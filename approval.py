"""Persistent, exact-operation approvals for the AstrBot authorization plugin."""

import json
import secrets
import sqlite3
import time
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

APPROVAL_SECONDS = 600
RETENTION_SECONDS = 86400


def display_json(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return "".join(
        json.dumps(char, ensure_ascii=True)[1:-1]
        if unicodedata.category(char)[0] == "C" or char in "\u2028\u2029"
        else char
        for char in text
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("JSON 值中有重复字段。")
    return result


@dataclass(frozen=True)
class Change:
    operation: str
    target: str
    value: str

    @classmethod
    def parse(cls, operation: str, target: str, value_json: str):
        if not isinstance(operation, str) or not operation.strip() or len(operation) > 60:
            raise ValueError("operation 需要具体填写，最多 60 字。")
        if not isinstance(target, str) or not target.strip() or len(target) > 220:
            raise ValueError("target 需要具体填写，最多 220 字。")
        if any(unicodedata.category(char) == "Cs" for char in operation + target):
            raise ValueError("operation 和 target 必须是有效 Unicode 文本。")
        if not isinstance(value_json, str) or not 1 <= len(value_json) <= 4000:
            raise ValueError("value_json 必须是 JSON 文本。")
        try:
            value = json.loads(value_json, object_pairs_hook=_unique_object)
            canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (ValueError, RecursionError) as exc:
            raise ValueError("value_json 必须是有效 JSON，且字段不得重复。") from exc
        if len(display_json([operation, target, value])) > 1000:
            raise ValueError("操作、目标和取值合计过长，请拆分审批范围。")
        return cls(operation, target, canonical)


@dataclass(frozen=True)
class Approval:
    request_id: str
    status: str
    expires_at: int
    change: Change
    origin: str
    approver: str

    def payload(self) -> dict:
        status = self.status if self.expires_at > int(time.time()) else "expired"
        return {
            "status": status,
            "request_id": self.request_id,
            "expires_at": self.expires_at,
            "operation": self.change.operation,
            "target": self.change.target,
            "value": json.loads(self.change.value),
        }


class ApprovalStore:
    def __init__(self, path: Path):
        self.path = path
        with closing(self._connect()) as db, db:
            db.executescript(
                """CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                    operation TEXT NOT NULL, target TEXT NOT NULL, value TEXT NOT NULL,
                    origin TEXT NOT NULL, approver TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS approvals_recent ON approvals (created_at);
                CREATE INDEX IF NOT EXISTS approvals_origin ON approvals (origin, expires_at);"""
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _approval(row: sqlite3.Row) -> Approval:
        return Approval(
            row["id"], row["status"], row["expires_at"],
            Change(row["operation"], row["target"], row["value"]),
            row["origin"], row["approver"],
        )

    def request(self, change: Change, origin: str, approver: str) -> tuple[Approval | None, bool]:
        now = int(time.time())
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM approvals WHERE created_at <= ?", (now - RETENTION_SECONDS,))
            parameters = (origin, approver, now)
            previous = db.execute(
                """SELECT * FROM approvals WHERE origin=? AND approver=? AND expires_at>?
                   AND operation=? AND target=? AND value=? AND status!='delivery_failed'
                   ORDER BY created_at DESC LIMIT 1""",
                (*parameters, change.operation, change.target, change.value),
            ).fetchone()
            if previous is not None:
                return self._approval(previous), False

            mine = db.execute(
                "SELECT count(*) FROM approvals WHERE origin=? AND created_at>?",
                (origin, now - 60),
            ).fetchone()[0]
            total = db.execute(
                "SELECT count(*) FROM approvals WHERE approver=? AND created_at>?",
                (approver, now - 60),
            ).fetchone()[0]
            pending = db.execute(
                """SELECT count(*) FROM approvals WHERE approver=? AND expires_at>?
                   AND status IN ('sending', 'pending')""",
                (approver, now),
            ).fetchone()[0]
            if mine >= 2 or total >= 6 or pending >= 20:
                return None, False

            approval = Approval(secrets.token_hex(12), "sending", now + APPROVAL_SECONDS, change, origin, approver)
            db.execute(
                """INSERT INTO approvals
                   (id,status,created_at,expires_at,operation,target,value,origin,approver)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (approval.request_id, approval.status, now, approval.expires_at,
                 change.operation, change.target, change.value, origin, approver),
            )
            return approval, True

    def delivered(self, request_id: str, ok: bool) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE approvals SET status=? WHERE id=? AND status='sending'",
                ("pending" if ok else "delivery_failed", request_id),
            )

    def status(self, request_id: str, origin: str, approver: str) -> Approval | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM approvals WHERE id=? AND origin=? AND approver=?",
                (request_id, origin, approver),
            ).fetchone()
            return self._approval(row) if row is not None else None

    def decide(self, request_id: str, approver: str, approve: bool) -> bool:
        with closing(self._connect()) as db, db:
            result = db.execute(
                """UPDATE approvals SET status=? WHERE id=? AND approver=? AND expires_at>?
                   AND status IN ('sending', 'pending')""",
                ("approved" if approve else "rejected", request_id, approver, int(time.time())),
            )
            return result.rowcount == 1

    def consume(self, request_id: str, change: Change, origin: str, approver: str) -> bool:
        with closing(self._connect()) as db, db:
            result = db.execute(
                """UPDATE approvals SET status='used' WHERE id=? AND status='approved'
                   AND origin=? AND approver=? AND expires_at>? AND operation=?
                   AND target=? AND value=?""",
                (request_id, origin, approver, int(time.time()),
                 change.operation, change.target, change.value),
            )
            return result.rowcount == 1
