"""Typed agent snapshots, bounded message metadata and event detection."""

import ntpath
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field

STATUS = {
    "blocked": "blocked",
    "waiting": "blocked",
    "wait": "blocked",
    "question": "blocked",
    "approval": "blocked",
    "waiting_for_approval": "blocked",
    "working": "working",
    "running": "working",
    "active": "working",
    "busy": "working",
    "idle": "idle",
    "ready": "idle",
    "done": "idle",
    "completed": "idle",
}


@dataclass(frozen=True)
class Agent:
    name: str
    pane: str
    workspace: str
    status: str
    raw_status: str
    seq: object = None
    completion_seq: object = None
    cwd: str = ""
    title: str = ""
    label: str = ""
    alias: str = ""
    terminal_id: str = ""
    workspace_label: str = ""

    @classmethod
    def parse(cls, row):
        raw = str(
            row.get("agent_status")
            or row.get("status")
            or row.get("lifecycle_state")
            or row.get("state")
            or "unknown"
        )
        return cls(
            str(row.get("agent") or row.get("name") or row.get("agent_id") or row.get("id") or "Unknown"),
            str(row.get("pane_id") or ""),
            str(row.get("workspace_id") or ""),
            STATUS.get(raw.lower(), "unknown"),
            raw,
            row.get("state_change_seq"),
            row.get("completion_seq"),
            str(row.get("cwd") or ""),
            str(row.get("terminal_title_stripped") or row.get("terminal_title") or ""),
            str(row.get("agent_name") or row.get("custom_name") or row.get("name") or ""),
            "",
            row["terminal_id"].strip() if isinstance(row.get("terminal_id"), str) else "",
            str(row.get("workspace_label") or row.get("workspace_name") or ""),
        )

    @property
    def identity(self):
        return self.pane, self.terminal_id or self.alias_key, self.name

    @property
    def alias_key(self):
        if self.terminal_id:
            return "terminal:" + self.terminal_id
        return "fallback:" + repr((self.workspace, self.pane, self.name, self.cwd))

    @property
    def project(self):
        return ntpath.basename(self.cwd.rstrip("/\\")) if self.cwd else ""

    @property
    def display_name(self):
        return self.alias or self.label or (f"{self.project} · {self.name}" if self.project else self.name)

    @property
    def version(self):
        return self.seq, self.completion_seq, self.raw_status


@dataclass(frozen=True)
class PendingAction:
    kind: str
    content: str = ""


@dataclass
class MessageMeta:
    chat_id: int
    agent: Agent
    kind: str
    actions: dict[str, tuple[str, str]]
    created: float
    token: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    message_id: int | None = None
    consumed: bool = False
    modes: dict = field(default_factory=dict)
    intent: PendingAction | None = None
    selection_group: str = ""


class StateChanged(ValueError):
    """A contextual terminal confirmation no longer matches the displayed snapshot."""


class Registry:
    def __init__(self, ttl=86400, limit=10000, clock=time.monotonic):
        self.ttl, self.limit, self.clock = ttl, limit, clock
        self.messages = OrderedDict()
        self.tokens = {}

    def draft(self, chat, agent, kind):
        return MessageMeta(chat, agent, kind, {}, self.clock())

    def bind(self, meta, message_id):
        meta.message_id = message_id
        key = (meta.chat_id, message_id)
        old = self.messages.pop(key, None)
        if old:
            self.tokens.pop(old.token, None)
        self.messages[key] = meta
        self.tokens[meta.token] = meta
        self.prune()

    def prune(self, active=None):
        now = self.clock()
        for key, meta in list(self.messages.items()):
            if now - meta.created >= self.ttl or (active is not None and meta.agent.identity not in active):
                self.messages.pop(key)
                self.tokens.pop(meta.token, None)
        while len(self.messages) > self.limit:
            _, meta = self.messages.popitem(last=False)
            self.tokens.pop(meta.token, None)

    def reply(self, chat, message_id):
        self.prune()
        return self.messages.get((chat, message_id))

    def callback(self, data, chat, message_id):
        self.prune()
        parts = data.split(":") if isinstance(data, str) else []
        if len(parts) != 3 or parts[0] != "v1":
            raise ValueError("Nút cũ hoặc không hợp lệ. Dùng /agents để làm mới.")
        meta = self.tokens.get(parts[1])
        if not meta or meta.chat_id != chat or meta.message_id != message_id or parts[2] not in meta.actions:
            raise ValueError("Nút đã hết hạn hoặc sai tin nhắn. Dùng /agents để làm mới.")
        return meta, meta.actions[parts[2]]

    def consume_selection(self, meta):
        meta.consumed = True
        if meta.selection_group:
            for sibling in self.messages.values():
                if sibling.selection_group == meta.selection_group:
                    sibling.consumed = True

    def consume(self, meta):
        # A confirmation delivered to several chats is still one terminal request.
        self.consume_selection(meta)
        for sibling in self.messages.values():
            if sibling.agent.identity == meta.agent.identity and sibling.agent.version == meta.agent.version:
                sibling.consumed = True

    def validate(self, meta, current, writing=False, require_version=None):
        if self.clock() - meta.created >= self.ttl:
            raise ValueError("Thông báo đã hết hạn. Dùng /agents để làm mới.")
        if not current or current.identity != meta.agent.identity:
            raise ValueError("Pane/agent không còn tồn tại. Dùng /agents để làm mới.")
        if require_version is None:
            require_version = writing
        if require_version and current.version != meta.agent.version:
            raise StateChanged(
                f"Trạng thái agent đã thay đổi. Dùng /agents {current.pane} để mở điều khiển mới."
            )
        if writing and meta.consumed:
            raise ValueError("Yêu cầu này đã được xử lý. Dùng /agents để làm mới.")
        if writing and require_version and current.seq is None and self.clock() - meta.created >= 300:
            raise ValueError("Nút điều khiển đã hết hạn sau 5 phút. Dùng /agents để làm mới.")


class RateLimiter:
    def __init__(self, calls, period, clock=time.monotonic):
        self.calls, self.period, self.clock = calls, period, clock
        self.history = {}

    def limited(self, uid):
        now = self.clock()
        history = self.history.setdefault(uid, deque())
        while history and now - history[0] >= self.period:
            history.popleft()
        if len(history) >= self.calls:
            return True
        history.append(now)
        return False


def transitions(previous, current):
    """First snapshot emits only blocked; completion counter is independent of state seq."""
    if current.status == "blocked":
        if previous is None or previous.status != "blocked" or previous.version != current.version:
            return "blocked"
    elif previous is not None and current.status == "idle":
        completion = current.completion_seq is not None and current.completion_seq != previous.completion_seq
        if previous.status in {"working", "blocked"} or completion:
            return "idle"
    return None
