"""AstrBot plugin for explicit administrator confirmation over OneBot v11."""

import asyncio
import json
import re
import sqlite3

from astrbot.api import logger
from astrbot.api.event import MessageChain, filter as event_filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.cron.events import CronMessageEvent

from .approval import APPROVAL_SECONDS, Approval, ApprovalStore, Change, display_json

PLUGIN_NAME = "astrbot_plugin_core_approval"
TOOL_NAME = "astrbot_admin_approval"
REQUEST_ID = re.compile(r"[a-f0-9]{24}")
QQ_ID = re.compile(r"[1-9][0-9]{4,14}")


def _origin(event) -> str | None:
    if isinstance(event, CronMessageEvent) or event.get_extra("agent_stop_requested"):
        return None
    if not getattr(getattr(event, "message_obj", None), "message_id", None):
        return None
    snapshot = getattr(event, "_maid_origin_identity", None)
    platform = str(event.get_platform_id() or "")
    sender = str(event.get_sender_id() or "")
    if snapshot is not None:
        is_active = getattr(event, "_maid_approval_is_active", None)
        if not isinstance(snapshot, dict) or not callable(is_active) or not is_active():
            return None
        if not snapshot.get("sourceMessageId") or snapshot.get("requestSource") != "chat":
            return None
        if snapshot.get("platformId") != platform or snapshot.get("senderId") != sender:
            return None
    umo = str(event.unified_msg_origin or "")
    if not platform or not sender or not umo.startswith(f"{platform}:"):
        return None
    return json.dumps([umo, sender, str(event.get_self_id() or "")], ensure_ascii=True)


class CoreApproval(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}

    async def initialize(self):
        self.store = ApprovalStore(StarTools.get_data_dir(PLUGIN_NAME) / "approvals.sqlite3")
        tool = self.context.get_llm_tool_manager().get_func(TOOL_NAME)
        if tool is not None:
            tool.parameters["required"] = ["action"]
            tool.parameters["properties"]["action"]["enum"] = ["request", "status"]

    def _approver(self) -> tuple[str, str] | None:
        platform_id = str(self.config.get("approver_platform_id", "")).strip()
        qq = str(self.config.get("approver_qq", "")).strip()
        if not platform_id or ":" in platform_id or not QQ_ID.fullmatch(qq):
            return None
        configured_admins = self.context.get_config().get("admins_id", ())
        if qq not in (configured_admins or ()):
            return None
        if not any(
            adapter.meta().id == platform_id and adapter.meta().name == "aiocqhttp"
            for adapter in self.context.platform_manager.platform_insts
        ):
            return None
        return platform_id, qq

    @staticmethod
    def _notice(approval: Approval, event) -> str:
        change = approval.change
        source = {
            "平台": str(event.get_platform_id() or ""),
            "发送者": str(event.get_sender_id() or ""),
            "群": str(event.get_group_id() or ""),
            "消息编号": str(event.message_obj.message_id or ""),
        }
        request_id = approval.request_id
        return (
            f"【AstrBot 权限确认】有效期 {APPROVAL_SECONDS // 60} 分钟\n"
            f"来源 {display_json(source)}\n"
            f"操作 {display_json(change.operation)}\n"
            f"目标 {display_json(change.target)}\n"
            f"取值 {display_json(json.loads(change.value))}\n\n"
            f"同意 /astr_approve {request_id}\n"
            f"拒绝 /astr_reject {request_id}"
        )

    @event_filter.llm_tool(name=TOOL_NAME)
    async def astrbot_admin_approval(
        self, event, action: str, operation: str = "", target: str = "",
        value_json: str = "", request_id: str = "",
    ) -> str:
        """Request approval for one exact operation from the configured AstrBot administrator.
        When pending or sending, report the ID and end the turn. Check with status on
        a subsequent turn in the same chat and account. Only approved records the
        administrator's explicit decision for the returned operation, target and value.

        Args:
            action(string): request to send an approval, or status to check a request_id.
            operation(string): Specific operation for request, such as update_config.
            target(string): Exact resource and setting key for request.
            value_json(string): Exact proposed value encoded as JSON text. Use null for deletion and quoted JSON for a string. Do not include secrets.
            request_id(string): ID returned by request, required for status.
        """
        try:
            result = await self._run_tool(event, action, operation, target, value_json, request_id)
        except sqlite3.Error as exc:
            logger.warning("[core-approval] 审批数据库不可用: %s", type(exc).__name__)
            result = {"status": "unavailable", "message": "审批数据库暂时不可用。"}
        return json.dumps(result, ensure_ascii=True)

    async def _run_tool(self, event, action, operation, target, value_json, request_id):
        if action not in ("request", "status"):
            return {"status": "invalid", "message": "action 只能填写 request 或 status。"}
        origin = _origin(event)
        if origin is None:
            return {"status": "unavailable", "message": "请从有发起者身份的 AstrBot 对话申请或查询。"}
        recipient = self._approver()
        if recipient is None:
            return {"status": "unavailable", "message": "请在插件配置里指定启用的 OneBot 实例及 AstrBot 管理员 QQ。"}
        platform, qq = recipient
        approver = f"{platform}:FriendMessage:{qq}"

        if action == "status":
            if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
                return {"status": "invalid", "message": "status 需要有效的 request_id。"}
            approval = self.store.status(request_id, origin, approver)
            return approval.payload() if approval else {"status": "not_found"}

        try:
            change = Change.parse(operation, target, value_json)
        except ValueError as exc:
            return {"status": "invalid", "message": str(exc)}
        approval, created = self.store.request(change, origin, approver)
        if approval is None:
            return {"status": "rate_limited", "message": "审批请求较多，请稍后再试。"}
        if not created:
            return approval.payload()

        # A maid's scoped context checks that its task is still active before sending.
        context = getattr(event, "_maid_context", None) or self.context
        sent = False
        try:
            sent = await asyncio.wait_for(
                context.send_message(approver, MessageChain().message(self._notice(approval, event))),
                timeout=10,
            )
        except Exception as exc:
            logger.warning("[core-approval] 私聊发送失败: %s", type(exc).__name__)
        finally:
            self.store.delivered(approval.request_id, ok=sent)
        updated = self.store.status(approval.request_id, origin, approver)
        return updated.payload() if updated else {"status": "unavailable"}

    def _decision(self, event, request_id: str, approve: bool) -> str | None:
        recipient = self._approver()
        if recipient is None:
            return None
        platform, qq = recipient
        approver = f"{platform}:FriendMessage:{qq}"
        if (
            event.get_platform_id() != platform or event.get_platform_name() != "aiocqhttp"
            or not event.is_private_chat() or str(event.get_sender_id()) != qq
            or event.unified_msg_origin != approver or not event.is_admin()
            or event.get_extra("_api_key_allow_admin_role") is False
        ):
            return None

        # Approve only an incoming OneBot message, not a cron, dashboard or child event.
        from aiocqhttp import Event

        raw = getattr(event.message_obj, "raw_message", None)
        if (
            not isinstance(raw, Event) or raw.get("post_type") != "message"
            or raw.get("message_type") != "private" or raw.get("group_id")
            or str(raw.get("user_id")) != qq
        ):
            return None
        if not REQUEST_ID.fullmatch(request_id):
            return "审批编号格式有误。"
        try:
            if self.store.decide(request_id, approver, approve):
                return "已批准这项操作。" if approve else "已拒绝这项操作。"
        except sqlite3.Error as exc:
            logger.warning("[core-approval] 审批数据库不可用: %s", type(exc).__name__)
            return "审批数据库暂时不可用，请稍后重试。"
        return "请求已过期、已处理或不存在。"

    @event_filter.command("astr_approve")
    async def approve(self, event, request_id: str = ""):
        event.stop_event()
        result = self._decision(event, request_id, True)
        if result is not None:
            yield event.plain_result(result)

    @event_filter.command("astr_reject")
    async def reject(self, event, request_id: str = ""):
        event.stop_event()
        result = self._decision(event, request_id, False)
        if result is not None:
            yield event.plain_result(result)

    def consume_approval(self, event, request_id: str, *, operation: str, target: str, value_json: str) -> bool:
        """Server-side API for trusted execution tools. Consume before performing a write."""
        recipient = self._approver()
        origin = _origin(event)
        if not recipient or not origin or not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            return False
        platform, qq = recipient
        try:
            change = Change.parse(operation, target, value_json)
            return self.store.consume(request_id, change, origin, f"{platform}:FriendMessage:{qq}")
        except (sqlite3.Error, ValueError):
            return False
