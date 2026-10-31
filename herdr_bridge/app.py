"""Telegram routing and dependency composition. No import-time configuration or I/O."""

import asyncio
import json
import logging
import re
import secrets
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, RetryAfter
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

from .client import KEYS, HerdrClient
from .config import Config, configure_logging
from .monitor import Monitor
from .names import NameStore
from .render import agent_summary, card, chunks, e, keyboard
from .state import Agent, PendingAction, RateLimiter, Registry, StateChanged

log = logging.getLogger(__name__)
HELP = """Herdr Telegram Bridge
/status — trạng thái hệ thống
/workspaces — danh sách workspace
/agents — danh sách ngắn: tên phiên — agent — trạng thái
/agents <tên hoặc pane_id> — mở thẻ điều khiển của một phiên
/logs <pane_id> — 30 dòng log
/prompt <tên hoặc pane_id> <nội dung> — giao nhiệm vụ
/rename <pane_id> <tên mới> — đặt tên phiên; --clear để xóa
/reply [pane_id] <nội dung> — gửi nội dung + Enter
/key [pane_id] <phím> — enter, esc, up, down, space, tab, ctrl+c
/stop <pane_id/agent> — gửi Ctrl+C
Reply thông báo còn hiệu lực để gửi nội dung vào đúng pane.
Lệnh rút gọn/tin nhắn tự do chỉ tự chọn khi có đúng một agent blocked.
Nút cũ sau khi bot khởi động lại cần được làm mới bằng /agents."""


class AmbiguousTarget(ValueError):
    def __init__(self, agents):
        self.agents = agents
        super().__init__("Tên khớp nhiều phiên; hãy chọn pane cụ thể.")


def target_and_content(text):
    """Only parse the target; preserve quotes and line breaks inside the prompt."""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return "", ""
    match = re.fullmatch(r'\s*(?:"([^"\r\n]+)"|((?!")\S+))(?:\s+([\s\S]*))?', parts[1])
    if not match:
        raise ValueError('Tên nhiều từ cần đặt trong dấu ngoặc kép, ví dụ /prompt "Backend API" nhiệm vụ.')
    return match.group(1) or match.group(2), (match.group(3) or "").strip()


class Runtime:
    def __init__(self, config, client=None, registry=None, names=None):
        self.config = config
        self.client = client or HerdrClient(config.executable, config.command_timeout)
        self.registry = registry or Registry()
        self.names = (
            names
            if names is not None
            else NameStore(Path(__file__).resolve().parent.parent / "data" / "agent_names.json")
        )
        self.limiter = RateLimiter(config.rate_calls, config.rate_period)
        self.snapshots = {}
        self.seen_callbacks = OrderedDict()
        self.monitor = Monitor(self)

    async def agents(self):
        result = await self.client.run(["agent", "list"])
        if not result.success:
            log.warning("Agent list unavailable timed_out=%s", result.timed_out)
            return None
        try:
            rows = json.loads(result.stdout)["result"]["agents"]
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError()
            agents = [Agent.parse(row) for row in rows]
            agents = [replace(a, alias=self.names.get(a)) for a in agents]
            # Workspace labels are display context; failure must not hide otherwise valid agents.
            workspaces = await self.client.run(["workspace", "list"])
            try:
                labels = {
                    row["workspace_id"]: str(row.get("label") or row["workspace_id"])
                    for row in json.loads(workspaces.stdout)["result"]["workspaces"]
                }
            except (ValueError, KeyError, TypeError):
                labels = {}
            return [replace(a, workspace_label=labels.get(a.workspace, a.workspace_label)) for a in agents]
        except (ValueError, KeyError, TypeError):
            log.warning("Invalid agent list response")
            return None

    async def require_agents(self):
        agents = await self.agents()
        if agents is None:
            raise ValueError("Không đọc được danh sách agent. Vui lòng thử lại.")
        return agents

    async def panes(self):
        result = await self.client.run(["pane", "list"])
        try:
            if not result.success:
                raise ValueError()
            rows = json.loads(result.stdout)["result"]["panes"]
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError()
            return rows
        except (ValueError, KeyError, TypeError):
            raise ValueError("Không đọc được danh sách pane. Vui lòng thử lại.") from None

    async def find(self, target=None, *, agent_name=False):
        agents = await self.require_agents()
        if target is None:
            matches = [a for a in agents if a.status == "blocked" and a.pane]
        elif agent_name:
            matches = [a for a in agents if a.pane == target]
            if not matches:
                matches = [
                    a
                    for a in agents
                    if a.pane
                    and target.casefold()
                    in {n.casefold() for n in (a.display_name, a.alias, a.label, a.name) if n}
                ]
            if len(matches) > 1:
                raise AmbiguousTarget(matches)
        else:
            matches = [a for a in agents if a.pane == target]
            if ":" not in target or not matches:
                rows = await self.panes()
                panes = [
                    r
                    for r in rows
                    if r.get("pane_id") == target or str(r.get("pane_id", "")).startswith(target + ":")
                ]
                if len(panes) != 1:
                    raise ValueError("Workspace không có một pane duy nhất. Chỉ định Pane ID đầy đủ.")
                pane = panes[0]["pane_id"]
                matches = [a for a in agents if a.pane == pane]
                if not matches:
                    matches = [Agent("", pane, pane.split(":")[0], "unknown", "unknown")]
        if len(matches) != 1:
            raise ValueError(
                "Không xác định được một pane duy nhất. Dùng /agents và chỉ định Pane ID đầy đủ."
            )
        return matches[0]

    async def fresh(self, agent, meta=None, writing=True, require_version=None):
        if agent.name:
            agents = await self.require_agents()
            matches = [a for a in agents if a.identity == agent.identity]
        else:
            rows = await self.panes()
            matches = [agent for row in rows if row.get("pane_id") == agent.pane]
        if len(matches) != 1:
            raise ValueError("Pane/agent không còn tồn tại. Dùng /agents để làm mới.")
        current = matches[0]
        if meta:
            self.registry.validate(meta, current, writing, require_version=require_version)
        return current

    async def authorize(self, update):
        user = update.effective_user
        if user is None or user.id not in self.config.allowed_users:
            log.warning("Denied user=%s", user.id if user else None)
            text = "⛔ Không có quyền truy cập."
        elif self.limiter.limited(user.id):
            text = "⚠️ Thao tác quá nhanh, vui lòng đợi."
        else:
            return True
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(text)
        return False

    async def reply_output(self, message, text):
        for part in chunks(text or "(Không có dữ liệu)"):
            await message.reply_text(f"<pre>{e(part)}</pre>", parse_mode="HTML")

    async def send_card(self, bot, chat, agent, kind="agents"):
        result = (
            await self.client.run(["pane", "read", agent.pane, "--lines", "15", "--format", "text", "--raw"])
            if agent.pane
            else None
        )
        snippet = result.stdout if result and result.success else "(Không đọc được terminal)"
        meta = self.registry.draft(chat, agent, kind)
        sent = await bot.send_message(
            chat_id=chat, text=card(agent, snippet), parse_mode="HTML", reply_markup=keyboard(meta, snippet)
        )
        self.registry.bind(meta, sent.message_id)

    async def write(self, user_id, agent, *, text=None, key=None, prompt=False, meta=None, consume=False):
        current = agent

        async def validate():
            nonlocal current
            # Typed new tasks and explicit navigation target the same live terminal.
            # Only text replies/choices and Enter depend on the displayed question.
            require_version = not prompt and key not in {"ctrl+c", "up", "down", "space", "tab", "esc"}
            current = await self.fresh(agent, meta, require_version=require_version)
            # Mark before writing: a timeout leaves the outcome uncertain, so old buttons cannot retry.
            if consume and meta:
                self.registry.consume(meta)

        result = await self.client.perform(
            agent.pane, text=text, key=key, prompt_agent=agent.pane if prompt else None, validate=validate
        )
        log.info(
            "AUDIT user=%s action=%s pane=%s length=%s success=%s timeout=%s",
            user_id,
            "prompt" if prompt else ("text" if text is not None else key),
            agent.pane,
            len(text) if text is not None else 0,
            result.success,
            result.timed_out,
        )
        if not result.success:
            log.warning(
                "Herdr write failed pane=%s exit_code=%s error_code=%s",
                agent.pane,
                result.exit_code,
                result.error_code or "unknown",
            )
            if result.stderr.startswith(("send-text", "Text sent")):
                detail = result.stderr
            elif result.timed_out:
                detail = "Herdr hết thời gian chờ; chưa xác định lệnh đã được nhận hay chưa."
            else:
                detail = {
                    "agent_blocked": "Agent đang chờ phản hồi; hãy xử lý yêu cầu hiện tại bằng Reply hoặc /reply trước khi giao việc mới.",
                    "agent_not_found": "Không tìm thấy agent tại pane này. Dùng /agents để kiểm tra lại.",
                    "pane_not_found": "Pane không còn tồn tại. Dùng /agents để kiểm tra lại.",
                    "cli_arguments": "Herdr từ chối đối số lệnh; nhiệm vụ chưa được gửi.",
                    "agent_prompt_stalled": "Herdr đã nhận prompt nhưng chưa thấy agent bắt đầu làm việc.",
                }.get(result.error_code, "Herdr không xác nhận thành công.")
            raise ValueError(detail + " Kiểm tra pane trước khi thử lại; lệnh không được tự gửi lại.")

        return current

    async def command(self, update, context):
        if not await self.authorize(update):
            return
        message = update.effective_message
        command = message.text.split()[0].split("@")[0].lower()
        args = context.args
        log.info(
            "AUDIT user=%s action=%s chat=%s", update.effective_user.id, command, update.effective_chat.id
        )
        try:
            if command in {"/start", "/help"}:
                await message.reply_text(HELP)
            elif command in {"/status", "/workspaces"}:
                cli = ["status"] if command == "/status" else ["workspace", "list"]
                result = await self.client.run(cli)
                await self.reply_output(
                    message, result.stdout if result.success else "Không đọc được trạng thái Herdr."
                )
            elif command == "/agents":
                target, content = target_and_content(message.text)
                if content:
                    raise ValueError('Cú pháp: /agents hoặc /agents "tên phiên" hoặc /agents <pane_id>')
                if not target:
                    await self.send_summary(message)
                else:
                    try:
                        agent = await self.find(target, agent_name=True)
                    except AmbiguousTarget as exc:
                        await self.choose(
                            message, update.effective_chat.id, exc.agents, PendingAction("view")
                        )
                        return
                    await self.send_card(context.bot, update.effective_chat.id, agent)
            elif command == "/logs":
                if len(args) != 1:
                    raise ValueError("Cú pháp: /logs <pane_id>")
                agent = await self.find(args[0])
                await self.logs(message, agent.pane)
            elif command in {"/prompt", "/stop", "/rename"}:
                target, content = target_and_content(message.text)
                kind = command[1:]
                if (
                    not target
                    or (kind in {"prompt", "rename"} and not content)
                    or (kind == "stop" and content)
                ):
                    raise ValueError(
                        f"Cú pháp: {command} <tên hoặc pane_id>"
                        + (" <nội dung/tên mới>" if kind != "stop" else "")
                    )
                intent = PendingAction(kind, content)
                try:
                    agent = await self.find(target, agent_name=True)
                except AmbiguousTarget as exc:
                    await self.choose(message, update.effective_chat.id, exc.agents, intent)
                    return
                await self.execute_target(message, update.effective_user.id, agent, intent)
            elif command in {"/reply", "/key"}:
                if not args:
                    raise ValueError(f"Cú pháp: {command} [pane_id] <nội dung/phím>")
                explicit = ":" in args[0] or args[0].startswith("w") and args[0][1:].isdigit()
                if explicit and len(args) < 2:
                    raise ValueError(f"Cú pháp: {command} <pane_id> <nội dung/phím>")
                agent = await self.find(args[0] if explicit else None)
                value = " ".join(args[1:] if explicit else args)
                if command == "/key":
                    if value.lower() not in KEYS:
                        raise ValueError("Phím hỗ trợ: " + ", ".join(sorted(KEYS)))
                    current = await self.write(update.effective_user.id, agent, key=value.lower())
                else:
                    current = await self.write(update.effective_user.id, agent, text=value)
                await message.reply_text(f"✅ Đã gửi tới {current.display_name} ({current.pane}).")
        except ValueError as exc:
            await message.reply_text(str(exc))

    async def send_summary(self, message):
        summary = agent_summary(await self.require_agents())
        for part in chunks(summary):
            await message.reply_text(part)

    async def choose(self, message, chat, agents, intent):
        await message.reply_text("Tên này khớp nhiều phiên. Chọn đúng phiên bên dưới; chưa gửi lệnh nào.")
        group = secrets.token_urlsafe(12)
        for agent in agents:
            meta = self.registry.draft(chat, agent, "selection")
            meta.intent = intent
            meta.selection_group = group
            meta.actions["0"] = ("select", "")
            button = InlineKeyboardButton(
                "Chọn " + agent.display_name[:40], callback_data=f"v1:{meta.token}:0"
            )
            sent = await message.reply_text(
                card(agent), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[button]])
            )
            self.registry.bind(meta, sent.message_id)

    async def rename(self, user_id, agent, label, meta=None):
        async with self.client.pane_lock(agent.pane):
            current = await self.fresh(agent, meta, require_version=False)
            self.names.set(current, "" if label == "--clear" else label)
            if meta:
                self.registry.consume_selection(meta)
        log.info("AUDIT user=%s action=rename pane=%s", user_id, agent.pane)
        return replace(current, alias=self.names.get(current))

    async def execute_target(self, message, user, agent, intent, meta=None):
        if intent.kind == "view":
            current = await self.fresh(agent, meta, require_version=False)
            if meta:
                self.registry.consume_selection(meta)
            # reply_text keeps the selected card in the same chat without requiring bot context.
            draft = self.registry.draft(meta.chat_id, current, "agents")
            sent = await message.reply_text(card(current), parse_mode="HTML", reply_markup=keyboard(draft))
            self.registry.bind(draft, sent.message_id)
        elif intent.kind == "rename":
            renamed = await self.rename(user, agent, intent.content, meta)
            await message.reply_text(
                f"✅ Tên phiên: {renamed.display_name} ({renamed.pane}). Dùng /agents để xem thẻ mới."
            )
        elif intent.kind == "prompt":
            current = await self.write(user, agent, text=intent.content, prompt=True, meta=meta, consume=True)
            await message.reply_text(f"✅ Đã gửi prompt tới {current.display_name} ({current.pane}).")
        elif intent.kind == "stop":
            current = await self.write(user, agent, key="ctrl+c", meta=meta, consume=True)
            await message.reply_text(f"🛑 Đã gửi Ctrl+C tới {current.display_name} ({current.pane}).")

    async def request_input(self, query, meta, kind):
        current = await self.fresh(meta.agent, meta, writing=False)
        draft = self.registry.draft(meta.chat_id, current, kind)
        text = (
            f"Đặt tên mới cho {current.display_name} ({current.pane}). Reply tin này với tên mới; "
            "gửi --clear để xóa tên riêng."
            if kind == "rename_input"
            else f"Reply tin này với nhiệm vụ cho {current.display_name} ({current.pane})."
        )
        sent = await query.message.reply_text(text, reply_markup=ForceReply(selective=True))
        self.registry.bind(draft, sent.message_id)

    async def logs(self, message, pane):
        result = await self.client.run(["pane", "read", pane, "--lines", "30", "--format", "text", "--raw"])
        await self.reply_output(message, result.stdout if result.success else "Không đọc được log.")

    async def text(self, update, context):
        if not await self.authorize(update):
            return
        message = update.effective_message
        log.info("AUDIT user=%s action=reply chat=%s", update.effective_user.id, update.effective_chat.id)
        value = message.text.strip()
        if not value:
            return
        try:
            meta = None
            if message.reply_to_message:
                replied = message.reply_to_message
                if not replied.from_user or replied.from_user.id != context.bot.id:
                    raise ValueError("Chỉ reply thông báo của bot, hoặc dùng /reply <pane_id> <nội dung>.")
                meta = self.registry.reply(update.effective_chat.id, replied.message_id)
                if not meta:
                    raise ValueError("Thông báo đã hết hạn. Dùng /agents hoặc /reply <pane_id> <nội dung>.")
                agent = meta.agent
                if meta.kind == "prompt_input":
                    await self.execute_target(
                        message, update.effective_user.id, agent, PendingAction("prompt", value), meta
                    )
                    return
                if meta.kind == "rename_input":
                    await self.execute_target(
                        message, update.effective_user.id, agent, PendingAction("rename", value), meta
                    )
                    return
            else:
                agent = await self.find()
            current = await self.write(update.effective_user.id, agent, text=value, meta=meta, consume=True)
            await message.reply_text(
                f"✅ Đã gửi nội dung + Enter tới {current.display_name} ({current.pane})."
            )
        except ValueError as exc:
            await message.reply_text(str(exc))

    async def callback(self, update, context):
        if not await self.authorize(update):
            return
        query = update.callback_query
        log.info("AUDIT user=%s action=callback chat=%s", update.effective_user.id, update.effective_chat.id)
        await query.answer()
        if query.id in self.seen_callbacks:
            return
        self.seen_callbacks[query.id] = None
        while len(self.seen_callbacks) > 10000:
            self.seen_callbacks.popitem(last=False)
        try:
            markup = getattr(query.message, "reply_markup", None)
            legacy_summary = markup is not None and any(
                button.text == "🔄 Agents" and button.callback_data == query.data
                for row in markup.inline_keyboard
                for button in row
            )
            # Global status is read-only and does not need the source pane's metadata.
            # Recognize old bot keyboards too, including messages sent before a restart.
            if query.data == "agents" or legacy_summary:
                await self.send_summary(query.message)
                return
            meta, (action, value) = self.registry.callback(
                query.data, update.effective_chat.id, query.message.message_id
            )
            if action == "agents":
                # Older keyboards also show current global status even if their source state changed.
                await self.send_summary(query.message)
            elif action == "select" and meta.intent is not None:
                await self.execute_target(
                    query.message, update.effective_user.id, meta.agent, meta.intent, meta
                )
                await query.edit_message_reply_markup(reply_markup=None)
            elif action in {"choice", "key"}:
                terminal = action == "choice" or value in {"enter", "esc", "ctrl+c"}
                current = await self.write(
                    update.effective_user.id,
                    meta.agent,
                    text=value if action == "choice" else None,
                    key=value if action == "key" else None,
                    meta=meta,
                    consume=terminal,
                )
                if terminal:
                    await query.edit_message_text(f"✅ Đã gửi tới {current.display_name} ({current.pane}).")
                    return
                await asyncio.sleep(0.25)
                await self.refresh(query, meta, "tui")
            else:
                await self.fresh(meta.agent, meta, writing=False)
                if action == "logs":
                    await self.logs(query.message, meta.agent.pane)
                elif action == "mode":
                    await self.refresh(query, meta, value)
                elif action in {"hint", "rename"}:
                    await self.request_input(
                        query, meta, "rename_input" if action == "rename" else "prompt_input"
                    )
        except StateChanged:
            # Never apply an old choice/Enter to a new question. Refresh the UI instead
            # of leaving every button stuck behind a stale snapshot.
            try:
                await self.refresh(query, meta, "tui" if action == "key" else "main")
                await query.message.reply_text(
                    "Đã cập nhật trạng thái và bàn phím. Hãy bấm lại thao tác bạn muốn."
                )
            except ValueError as exc:
                await query.message.reply_text(str(exc))
        except ValueError as exc:
            await query.message.reply_text(str(exc))

    async def refresh(self, query, meta, mode):
        current = await self.fresh(meta.agent, meta, writing=False)
        result = await self.client.run(
            ["pane", "read", meta.agent.pane, "--lines", "15", "--format", "text", "--raw"]
        )
        snippet = result.stdout if result.success else "(Không đọc được terminal)"
        # A new token binds the rendered controls to this snapshot. Reusing a token
        # would let a delayed click on an old menu select an option from a new menu.
        updated = self.registry.draft(meta.chat_id, current, meta.kind)
        markup = keyboard(updated, snippet, mode)
        await query.edit_message_text(card(current, snippet), parse_mode="HTML", reply_markup=markup)
        self.registry.bind(updated, query.message.message_id)

    async def error(self, update, context):
        err = context.error
        # An update send failure never replays a terminal write.
        log.error("Handler failed type=%s; terminal actions will not be retried", type(err).__name__)
        if isinstance(err, (NetworkError, RetryAfter)):
            return


def build_application(config):
    runtime = Runtime(config)

    def request(read):
        return HTTPXRequest(
            connection_pool_size=16,
            connect_timeout=config.connect_timeout,
            read_timeout=read,
            write_timeout=config.write_timeout,
            pool_timeout=config.pool_timeout,
            proxy=config.proxy,
        )

    app = (
        Application.builder()
        .token(config.token)
        .request(request(config.read_timeout))
        .get_updates_request(request(config.read_timeout + 15))
        .build()
    )
    app.bot_data["runtime"] = runtime
    app.add_error_handler(runtime.error)
    commands = [
        "start",
        "help",
        "status",
        "workspaces",
        "agents",
        "logs",
        "prompt",
        "reply",
        "key",
        "stop",
        "rename",
    ]
    app.add_handler(CommandHandler(commands, runtime.command))
    app.add_handler(CallbackQueryHandler(runtime.callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, runtime.text))
    # Always schedule cleanup; notification flags only control event generation.
    if app.job_queue is None:
        raise ValueError("Install python-telegram-bot[job-queue]")
    app.job_queue.run_repeating(
        runtime.monitor.tick,
        interval=config.poll_interval,
        first=2,
        job_kwargs={"max_instances": 1, "coalesce": True},
    )
    return app


def main():
    try:
        config = Config.load()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    configure_logging()
    log.info("Starting bridge users=%s interval=%s", len(config.allowed_users), config.poll_interval)
    build_application(config).run_polling(drop_pending_updates=True, bootstrap_retries=10, timeout=15)
