import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError

from herdr_bridge.app import Runtime, build_application
from herdr_bridge.client import CommandResult, HerdrClient
from herdr_bridge.config import Config
from herdr_bridge.names import NameStore
from tests.test_state import agent


def runtime():
    config = Config("123:ABC", frozenset({1}), frozenset({1}), "unused")
    client = HerdrClient("unused")
    client.run = AsyncMock(return_value=CommandResult(True))
    rt = Runtime(config, client, names=NameStore())
    rt.agents = AsyncMock(return_value=[agent()])
    return rt


def update(text="hello", reply=None, uid=1):
    message = SimpleNamespace(text=text, reply_to_message=reply, reply_text=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid),
        effective_chat=SimpleNamespace(id=1),
        effective_message=message,
        callback_query=None,
    )


def context():
    return SimpleNamespace(bot=SimpleNamespace(id=99), args=[])


def test_no_fallback_from_unknown_reply_or_idle_text():
    async def scenario():
        rt = runtime()
        for reply in [
            SimpleNamespace(message_id=123, from_user=SimpleNamespace(id=99)),
            SimpleNamespace(message_id=123, from_user=SimpleNamespace(id=20)),
        ]:
            upd = update(reply=reply)
            await rt.text(upd, context())
            rt.client.run.assert_not_awaited()
        rt.agents.return_value = [agent("idle")]
        await rt.text(update(), context())
        rt.client.run.assert_not_awaited()

    asyncio.run(scenario())


def test_reply_requires_fresh_state_and_never_falls_back():
    async def scenario():
        rt = runtime()
        meta = rt.registry.draft(1, agent(), "blocked")
        rt.registry.bind(meta, 10)
        rt.agents.return_value = [agent(seq=2)]
        upd = update(reply=SimpleNamespace(message_id=10, from_user=SimpleNamespace(id=99)))
        await rt.text(upd, context())
        rt.client.run.assert_not_awaited()

    asyncio.run(scenario())


def test_unauthorized_and_rate_limited_never_write():
    async def scenario():
        rt = runtime()
        upd = update(uid=2)
        await rt.text(upd, context())
        rt.client.run.assert_not_awaited()
        rt.limiter.calls = 1
        rt.limiter.limited(1)
        await rt.text(update(), context())
        rt.client.run.assert_not_awaited()

    asyncio.run(scenario())


def test_duplicate_callback_does_not_write_twice():
    async def scenario():
        rt = runtime()
        meta = rt.registry.draft(1, agent(), "blocked")
        meta.actions["0"] = ("choice", "n")
        rt.registry.bind(meta, 10)
        upd = update()
        query = SimpleNamespace(
            id="same",
            data=f"v1:{meta.token}:0",
            message=SimpleNamespace(message_id=10, reply_text=AsyncMock()),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        upd.callback_query = query
        await rt.callback(upd, context())
        await rt.callback(upd, context())
        assert rt.client.run.await_count == 2
        assert meta.consumed

    asyncio.run(scenario())


def test_confirmation_network_failure_never_replays_terminal_write():
    async def scenario():
        rt = runtime()
        upd = update()
        upd.effective_message.reply_text.side_effect = NetworkError("offline")
        with pytest.raises(Exception):
            await rt.text(upd, context())
        assert rt.client.run.await_count == 2
        await rt.error(upd, SimpleNamespace(error=RuntimeError()))
        assert rt.client.run.await_count == 2

    asyncio.run(scenario())


def test_workspace_ambiguity_and_plain_terminal_support():
    async def scenario():
        rt = runtime()
        rt.panes = AsyncMock(return_value=[{"pane_id": "w1:p1"}, {"pane_id": "w1:p2"}])
        with pytest.raises(ValueError):
            await rt.find("w1")
        plain = await rt.find("w1:p2")
        assert plain.name == ""
        await rt.write(1, plain, text="hello")
        assert rt.client.run.await_count == 2

    asyncio.run(scenario())


def test_same_pane_callbacks_are_revalidated_inside_lock():
    async def scenario():
        rt = runtime()
        meta = rt.registry.draft(1, agent(), "blocked")
        results = await asyncio.gather(
            rt.write(1, agent(), text="y", meta=meta, consume=True),
            rt.write(1, agent(), text="y", meta=meta, consume=True),
            return_exceptions=True,
        )
        assert sum(isinstance(r, ValueError) for r in results) == 1
        assert rt.client.run.await_count == 2

    asyncio.run(scenario())


def test_invalid_agent_response_is_unavailable_not_empty():
    async def scenario():
        rt = runtime()
        del rt.agents
        for payload in ["{}", "not-json", json.dumps({"result": {"agents": [1]}})]:
            rt.client.run.return_value = CommandResult(True, stdout=payload)
            assert await rt.agents() is None

    asyncio.run(scenario())


def test_application_builds_without_network():
    async def scenario():
        app = build_application(runtime().config)
        assert "runtime" in app.bot_data
        assert len(app.job_queue.jobs()) == 1
        # Not initialized: constructing the application must not contact Telegram.
        await app.bot.request.shutdown()
        await app.bot._request[0].shutdown()

    asyncio.run(scenario())


def test_explicit_pane_without_content_never_becomes_auto_reply():
    async def scenario():
        rt = runtime()
        ctx = context()
        ctx.args = ["w2:p1"]
        await rt.command(update("/reply w2:p1"), ctx)
        rt.client.run.assert_not_awaited()

    asyncio.run(scenario())


def test_successful_prompt_log_and_tui_commands():
    async def scenario():
        rt = runtime()
        ctx = context()
        ctx.args = ["agent", "--wait"]
        await rt.command(update("/prompt agent --wait"), ctx)
        assert rt.client.run.call_args.args[0] == ["agent", "prompt", "w1:p1", "--wait"]
        ctx.args = ["w1:p1"]
        rt.client.run.return_value = CommandResult(True, stdout="<unsafe>\U0001f600" * 2000)
        upd = update("/logs w1:p1")
        await rt.command(upd, ctx)
        assert upd.effective_message.reply_text.await_count > 1
        meta = rt.registry.draft(1, agent(), "blocked")
        meta.actions["0"] = ("key", "up")
        rt.registry.bind(meta, 10)
        upd = update()
        upd.callback_query = SimpleNamespace(
            id="up",
            data=f"v1:{meta.token}:0",
            message=SimpleNamespace(message_id=10, reply_text=AsyncMock()),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        await rt.callback(upd, ctx)
        assert not meta.consumed
        upd.callback_query.edit_message_text.assert_awaited_once()

    asyncio.run(scenario())


def test_confirmation_consumes_sibling_notifications_across_chats():
    async def scenario():
        rt = runtime()
        first = rt.registry.draft(1, agent(), "blocked")
        second = rt.registry.draft(2, agent(), "blocked")
        rt.registry.bind(first, 10)
        rt.registry.bind(second, 20)
        results = await asyncio.gather(
            rt.write(1, agent(), text="y", meta=first, consume=True),
            rt.write(1, agent(), text="y", meta=second, consume=True),
            return_exceptions=True,
        )
        assert sum(isinstance(r, ValueError) for r in results) == 1
        assert rt.client.run.await_count == 2
        assert first.consumed and second.consumed

    asyncio.run(scenario())


def test_agents_button_and_command_only_return_compact_status():
    from dataclasses import replace

    async def scenario():
        rt = runtime()
        rt.agents.return_value = [
            replace(agent(), name="codex", alias="Backend API"),
            replace(agent("idle"), name="codex", alias="Frontend", pane="w1:p2"),
        ]
        upd = update("/agents")
        await rt.command(upd, context())
        assert upd.effective_message.reply_text.await_count == 1
        text = upd.effective_message.reply_text.call_args.args[0]
        assert text == "Backend API — codex — 🚨 Đang chờ phản hồi\nFrontend — codex — 🔵 Rảnh rỗi"
        assert "<pre>" not in text and "Pane:" not in text
        rt.client.run.assert_not_awaited()
        upd.callback_query = SimpleNamespace(
            id="summary", data="agents", message=SimpleNamespace(reply_text=AsyncMock()), answer=AsyncMock()
        )
        await rt.callback(upd, context())
        assert upd.callback_query.message.reply_text.call_args.args[0] == text
        rt.client.run.assert_not_awaited()

    asyncio.run(scenario())


def test_legacy_agents_button_ignores_changed_source_status():
    async def scenario():
        rt = runtime()
        meta = rt.registry.draft(1, agent(), "agents")
        meta.actions["0"] = ("agents", "")
        rt.registry.bind(meta, 10)
        rt.agents.return_value = [agent("working", seq=2)]
        upd = update()
        upd.callback_query = SimpleNamespace(
            id="legacy-summary",
            data=f"v1:{meta.token}:0",
            message=SimpleNamespace(message_id=10, reply_text=AsyncMock()),
            answer=AsyncMock(),
        )
        await rt.callback(upd, context())
        assert "Đang làm việc" in upd.callback_query.message.reply_text.call_args.args[0]
        rt.client.run.assert_not_awaited()

    asyncio.run(scenario())


def test_explicit_agents_target_opens_only_that_card():
    async def scenario():
        rt = runtime()
        ctx = context()
        ctx.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=12))
        rt.client.run.return_value = CommandResult(True, stdout="terminal output")
        await rt.command(update("/agents w1:p1"), ctx)
        ctx.bot.send_message.assert_awaited_once()
        assert "<pre>" in ctx.bot.send_message.call_args.kwargs["text"]
        assert rt.registry.reply(1, 12) is not None

    asyncio.run(scenario())


def test_idle_notification_reply_preserves_exact_terminal_text():
    async def scenario():
        rt = runtime()
        snapshot = agent("idle")
        rt.agents.return_value = [snapshot]
        meta = rt.registry.draft(1, snapshot, "idle_notification")
        rt.registry.bind(meta, 10)
        text = '--check this "quoted" task\nand next line'
        upd = update(text, reply=SimpleNamespace(message_id=10, from_user=SimpleNamespace(id=99)))
        await rt.text(upd, context())
        assert rt.client.run.call_args_list[0].args[0] == ["pane", "send-text", "w1:p1", text]
        assert rt.client.run.call_args_list[1].args[0] == ["pane", "send-keys", "w1:p1", "enter"]

    asyncio.run(scenario())


def test_prompt_refusal_reports_known_reason_without_exposing_payload():
    async def scenario():
        rt = runtime()
        rt.client.run.return_value = CommandResult(False, error_code="agent_blocked", stderr="private-prompt")
        upd = update("/prompt w1:p1 secret task")
        await rt.command(upd, context())
        reply = upd.effective_message.reply_text.call_args.args[0]
        assert "Agent đang chờ phản hồi" in reply
        assert "private-prompt" not in reply
        assert rt.client.run.await_count == 1

    asyncio.run(scenario())


def test_agents_button_from_before_restart_still_returns_summary():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    async def scenario():
        rt = runtime()
        upd = update()
        data = "v1:expired-token:7"
        upd.callback_query = SimpleNamespace(
            id="before-restart",
            data=data,
            message=SimpleNamespace(
                reply_text=AsyncMock(),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Agents", callback_data=data)]]),
            ),
            answer=AsyncMock(),
        )
        await rt.callback(upd, context())
        text = upd.callback_query.message.reply_text.call_args.args[0]
        assert "Đang chờ phản hồi" in text and "hết hạn" not in text
        rt.client.run.assert_not_awaited()

    asyncio.run(scenario())
