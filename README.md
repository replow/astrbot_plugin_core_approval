# AstrBot 核心增强 · 管理员确认

为主模型与子代理提供 `astrbot_admin_approval`。一次审批对应一项具体操作，包含操作名称、目标资源和 JSON 取值。

## 配置

1. 在 AstrBot WebUI 上传插件 ZIP 并启用。版本要求为 AstrBot 4.27.3 或更高。
2. 打开插件配置。将 `approver_platform_id` 填为 OneBot v11（`aiocqhttp`）消息平台的实例 ID，将 `approver_qq` 填为审批人的 QQ 号。SnowLuna 或 NapCat 可在接入这个 AstrBot 平台实例后使用。
3. 把该 QQ 号添加到 AstrBot 基础配置的管理员 ID 列表。在同一平台实例下确认机器人能主动给该 QQ 发私聊。若配置了自定义命令前缀，回复审批时使用实际前缀。
4. 在函数工具管理中启用 `astrbot_admin_approval`。本安装包中的代理女仆会在主模型与管家的可用工具中保留它。使用其他原生子代理时，如果配置了工具白名单，将该工具加到对应列表。

## 申请与回复

例如，申请把 `demo.cache.ttl` 改为数值 900：

```json
{
  "action": "request",
  "operation": "update_config",
  "target": "demo.cache.ttl",
  "value_json": "900"
}
```

`value_json` 是 JSON 文本。数值是 `"900"`，字符串 900 是 `"\"900\""`，布尔值示例是 `"false"`，清空或删除可使用 `"null"`，具体含义由执行工具定义。请使用资源全名和具体键，敏感字段应通过执行端既有的密钥引用传递。

审批人私聊收到操作、目标、取值、来源和编号，可用 `/astr_approve <编号>` 同意，或用 `/astr_reject <编号>` 拒绝。回复需要来自指定平台实例上已配置的 AstrBot 管理员 QQ 私聊。申请有效期为 10 分钟。同一来源每分钟最多发出 2 条申请，同一审批人每分钟最多接收 6 条。有效期内重复申请同一项操作会返回已有编号。

发起方在同一账号、同一来源会话中用 `{"action":"status","request_id":"<编号>"}` 查询。返回值包括 `pending`、`approved`、`rejected`、`expired`、`used`、`delivery_failed` 等状态。收到 `pending` 时可以先结束本轮，管理员处理后在原会话继续并查询编号。

## 接入执行工具

审批工具负责记录管理员决定。需要强制执行审批的插件，在真正写入前调用 `consume_approval`。用执行代码实际解析后的操作、目标和取值核对。为同一请求并发执行时，SQLite 只允许一次消费成功。

```python
approval_plugin = context.get_registered_star("astrbot_plugin_core_approval")
if not approval_plugin or not approval_plugin.activated or not approval_plugin.star_cls:
    return "管理员审批工具未启用"

if not approval_plugin.star_cls.consume_approval(
    event,
    request_id,
    operation="update_config",
    target=actual_config_key,
    value_json=json.dumps(actual_new_value, allow_nan=False),
):
    return "这项操作缺少有效审批"

# 此处调用现有写入逻辑，继续执行自身的权限检查。
```

普通插件和外部 MCP 工具需要各自接入核验。若已有授权系统，保留它并在需要额外审批的操作前增加这一校验。一次性审批在写入前消费。写入失败后，可用执行端已有的幂等恢复机制。需要重新申请同一项操作时，请等原申请过期。
