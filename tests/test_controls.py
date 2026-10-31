import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from herdr_bridge.client import CommandResult
from tests.test_app import context, runtime, update
from tests.test_state import agent


def control(rt, action, value="", snapshot=None):
    meta = rt.registry.draft(1, snapshot or agent(), "agents")
    meta.actions["0"] = (action, value)
    rt.registry.bind(meta, 10)
    upd = update()
    upd.callback_query = SimpleNamespace(
        id="control",
        data=f"v1:{meta.token}:0",
        answer=AsyncMock(),
        message=SimpleNamespace(
            message_id=10, reply_text=AsyncMock(return_value=SimpleNamespace(message_id=11))
        ),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    return meta, upd


@pytest.mark.parametrize(
    "action,value",
    [("logs", ""), ("mode", "tui"), ("mode", "num"), ("mode", "main"), ("hint", ""), ("rename", "")],
)
def test_nonterminal_controls_work_after_state_changes(action, value):
    async def scenario():
        rt = runtime()
        rt.agents.return_value = [agent("idle", seq=10, completion=3)]
        _, upd = control(rt, action, value)
        await rt.callback(upd, context())
        for call in rt.client.run.call_args_list:
            assert call.args[0][:2] == ["pane", "read"]
        if action == "mode":
            upd.callback_query.edit_message_text.assert_awaited_once()
            assert rt.registry.reply(1, 10).agent.seq == 10
        else:
            upd.callback_query.message.reply_text.assert_awaited_once()
            assert (
                "Trạng thái agent đã thay đổi" not in upd.callback_query.message.reply_text.call_args.args[0]
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["up", "down", "space", "tab", "esc", "ctrl+c"])
def test_explicit_navigation_and_stop_target_the_live_terminal(key):
    async def scenario():
        rt = runtime()
        rt.agents.return_value = [agent("working", seq=2)]
        old, upd = control(rt, "key", key)
        await rt.callback(upd, context())
        assert rt.client.run.call_args_list[0].args[0] == ["pane", "send-keys", "w1:p1", key]
        if key not in {"esc", "ctrl+c"}:
            refreshed = rt.registry.reply(1, 10)
            assert refreshed.agent.seq == 2 and refreshed.token != old.token

    asyncio.run(scenario())


@pytest.mark.parametrize("action,value", [("choice", "y"), ("key", "enter")])
def test_stale_confirmation_refreshes_menu_without_sending_then_accepts_new_click(action, value):
    async def scenario():
        rt = runtime()
        rt.agents.return_value = [agent(seq=2)]
        rt.client.run.return_value = CommandResult(True, stdout="[y/n]")
        old, upd = control(rt, action, value)
        await rt.callback(upd, context())
        assert rt.client.run.await_count == 1
        assert rt.client.run.call_args.args[0][:2] == ["pane", "read"]
        refreshed = rt.registry.reply(1, 10)
        assert refreshed.agent.seq == 2 and refreshed.token != old.token
        assert old.token not in rt.registry.tokens
        assert "Đã cập nhật" in upd.callback_query.message.reply_text.call_args.args[0]
        ident = next(k for k, v in refreshed.actions.items() if v == (action, value))
        upd.callback_query.id = "new-click"
        upd.callback_query.data = f"v1:{refreshed.token}:{ident}"
        await rt.callback(upd, context())
        assert rt.client.run.call_args.args[0] == ["pane", "send-keys", "w1:p1", "enter"]
        assert refreshed.consumed

    asyncio.run(scenario())


@pytest.mark.parametrize("action,value", [("logs", ""), ("mode", "tui"), ("key", "up"), ("hint", "")])
def test_reused_pane_with_different_terminal_is_always_rejected(action, value):
    async def scenario():
        rt = runtime()
        old = replace(agent(), terminal_id="old")
        rt.agents.return_value = [replace(old, terminal_id="new")]
        _, upd = control(rt, action, value, old)
        await rt.callback(upd, context())
        rt.client.run.assert_not_awaited()
        assert "không còn tồn tại" in upd.callback_query.message.reply_text.call_args.args[0]

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["rename_input", "prompt_input"])
def test_explicit_input_request_survives_normal_lifecycle_changes(kind):
    async def scenario():
        rt = runtime()
        rt.agents.return_value = [agent("idle", seq=5)]
        meta = rt.registry.draft(1, agent(), kind)
        rt.registry.bind(meta, 10)
        upd = update("Backend API", reply=SimpleNamespace(message_id=10, from_user=SimpleNamespace(id=99)))
        await rt.text(upd, context())
        if kind == "rename_input":
            assert rt.names.get(agent()) == "Backend API"
            rt.client.run.assert_not_awaited()
        else:
            assert rt.client.run.call_args.args[0] == ["agent", "prompt", "w1:p1", "Backend API"]

    asyncio.run(scenario())
