"""HTML rendering: dynamic values are escaped, raw output is chunked before escaping."""

import html
import re
from collections import Counter

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .choices import detect_terminal_choices

ANSI = re.compile(r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def clean(text):
    return ANSI.sub("", text or "")


def e(text):
    return html.escape(str(text))


def chunks(text, limit=3000):
    # UTF-16 code units: Telegram counts astral emoji as two units.
    chunk, units = [], 0
    for char in clean(text):
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit:
            yield "".join(chunk)
            chunk, units = [], 0
        chunk.append(char)
        units += size
    if chunk:
        yield "".join(chunk)


def bounded(text, limit):
    return next(chunks(str(text), limit), "")


def status_label(agent):
    return {"blocked": "🚨 Đang chờ phản hồi", "idle": "🔵 Rảnh rỗi", "working": "⚙️ Đang làm việc"}.get(
        agent.status, "❔ Chưa xác định"
    )


def agent_summary(agents):
    def single_line(value, limit):
        return " ".join(bounded(value, limit).split())

    names = [single_line(a.alias or a.label or a.project or a.name, 100) for a in agents]
    counts = Counter(names)
    lines = []
    for agent, name in zip(agents, names):
        if counts[name] > 1:
            name += f" ({single_line(agent.pane, 60)})"
        lines.append(f"{name} — {single_line(agent.name, 40)} — {status_label(agent)}")
    return "\n".join(lines) or "Không có agent."


def card(agent, snippet=""):
    label = status_label(agent)
    text = (
        f"<b>{e(bounded(agent.display_name, 160))}</b>\n{label}\n"
        f"Loại agent: <code>{e(bounded(agent.name, 100))}</code>\n"
        f"Dự án: {e(bounded(agent.project or '—', 100))}\n"
        f"Workspace: {e(bounded(agent.workspace_label or agent.workspace, 100))} | "
        f"Pane: <code>{e(bounded(agent.pane, 100))}</code>"
    )
    if agent.title:
        text += f"\n{e(bounded(agent.title, 160))}"
    if snippet:
        text += f"\n<pre>{e(bounded(clean(snippet)[-1800:], 1800))}</pre>"
    return text + "\nReply tin nhắn này để gửi nội dung tới pane."


def keyboard(meta, snippet="", mode="main"):
    # Stable action IDs across menu edits: a delayed click cannot become a different action.
    def button(label, action, value=""):
        pair = (action, value)
        ident = next((k for k, v in meta.actions.items() if v == pair), None)
        if ident is None:
            ident = str(len(meta.actions))
            meta.actions[ident] = pair
        return InlineKeyboardButton(label, callback_data=f"v1:{meta.token}:{ident}")

    if mode == "tui":
        rows = [
            [button("⬆️", "key", "up"), button("⏎ Enter", "key", "enter"), button("⬇️", "key", "down")],
            [button("Space", "key", "space"), button("Esc", "key", "esc"), button("Tab", "key", "tab")],
            [button("🔄 Cập nhật", "mode", "tui"), button("🔙 Menu", "mode", "main")],
        ]
    elif mode == "num":
        rows = [[button(str(i), "choice", str(i)) for i in range(1, 6)], [button("🔙 Menu", "mode", "main")]]
    else:
        rows = []
        if meta.agent.status == "blocked":
            choices, _ = detect_terminal_choices(snippet)
            rows.extend([[button(c["label"], "choice", c["key"])] for c in choices])
            rows.append(
                [button("✅ Gửi y + Enter", "choice", "y"), button("❌ Gửi n + Enter", "choice", "n")]
            )
        rows.extend(
            [
                [button("🎮 Bàn phím", "mode", "tui"), button("🔢 1–5", "mode", "num")],
                [button("📜 Log", "logs"), button("🛑 Ctrl+C", "key", "ctrl+c")],
                [button("💬 Giao việc", "hint"), button("✏️ Đổi tên", "rename")],
                [InlineKeyboardButton("🔄 Agents", callback_data="agents")],
            ]
        )
    markup = InlineKeyboardMarkup(rows)
    # Reuse a menu rather than growing token actions with every terminal refresh.
    meta.modes[mode] = markup
    return markup
