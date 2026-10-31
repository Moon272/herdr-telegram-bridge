import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import RetryAfter

from herdr_bridge.client import CommandResult
from herdr_bridge.monitor import Monitor
from herdr_bridge.state import PendingAction
from tests.test_app import context, runtime, update
from tests.test_controls import control
from tests.test_state import agent


@pytest.mark.parametrize("replacement", ["new", "", None])
def test_plain_pane_replacement_never_receives_text_or_enter(replacement):
    async def scenario():
        rt = runtime()
        rt.agents.return_value = []
        rt.panes = AsyncMock(return_value=[{"pane_id": "w1:p2", "terminal_id": "old"}])
        target = await rt.find("w1:p2")
        rt.panes.return_value = (
            [] if replacement is None else [{"pane_id": "w1:p2", "terminal_id": replacement}]
        )
        with pytest.raises(ValueError):
            await rt.write(1, target, text="secret")
        rt.client.run.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", [{"text": "y"}, {"text": "task", "prompt": True}, {"key": "up"}])
def test_missing_terminal_id_blocks_all_terminal_writes_but_allows_rename(action):
    async def scenario():
        rt = runtime()
        target = replace(agent(), terminal_id="", cwd="C:/project")
        rt.agents.return_value = [target]
        with pytest.raises(ValueError, match="terminal_id"):
            await rt.write(1, target, **action)
        rt.client.run.assert_not_awaited()
        renamed = await rt.rename(1, target, "New name")
        assert renamed.alias == "New name"
        assert await rt.fresh(target, writing=False) == target

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "rows", [[], [{"pane_id": "w1:p1", "terminal_id": "new"}], [{"pane_id": "w1:p1", "terminal_id": ""}]]
)
def test_agent_and_pane_identity_must_agree_before_writing(rows):
    async def scenario():
        rt = runtime()
        rt.panes = AsyncMock(return_value=rows)
        with pytest.raises(ValueError):
            await rt.write(1, agent(), key="ctrl+c")
        rt.client.run.assert_not_awaited()

    asyncio.run(scenario())


def test_pane_read_failure_never_writes():
    async def scenario():
        rt = runtime()
        rt.panes = AsyncMock(side_effect=ValueError("CLI unavailable"))
        with pytest.raises(ValueError):
            await rt.write(1, agent(), text="task", prompt=True)
        rt.client.run.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("route", ["command", "callback", "reply", "selection"])
def test_missing_id_is_rejected_through_every_write_entrypoint(route):
    async def scenario():
        rt = runtime()
        target = replace(agent(), terminal_id="")
        rt.agents.return_value = [target]
        if route == "command":
            await rt.command(update("/prompt w1:p1 task"), context())
        elif route == "reply":
            meta = rt.registry.draft(1, target, "blocked")
            rt.registry.bind(meta, 10)
            upd = update("y", reply=SimpleNamespace(message_id=10, from_user=SimpleNamespace(id=99)))
            await rt.text(upd, context())
        else:
            meta, upd = control(rt, "choice" if route == "callback" else "select", "y", target)
            if route == "selection":
                meta.intent = PendingAction("prompt", "task")
            await rt.callback(upd, context())
        rt.client.run.assert_not_awaited()

    asyncio.run(scenario())


def test_unidentified_agent_identity_uses_fallback_context_but_ignores_display_names():
    target = replace(agent(), terminal_id="", cwd="C:/old")
    assert target.identity != replace(target, cwd="C:/new").identity
    assert target.identity != replace(target, workspace="w2").identity
    assert target.identity == replace(target, alias="New name", workspace_label="New label").identity


@pytest.mark.parametrize(
    "pane_rows",
    [
        [{"pane_id": "w1:p1", "terminal_id": "resolved"}],
        [{"pane_id": "w1:p1", "terminal_id": "first"}, {"pane_id": "w1:p1", "terminal_id": "second"}],
        [],
        None,
    ],
)
def test_agent_terminal_id_enrichment_is_unique_and_failure_keeps_read_only_agents(pane_rows):
    async def scenario():
        rt = runtime()
        del rt.agents
        rt.panes = AsyncMock(return_value=pane_rows)
        if pane_rows is None:
            rt.panes.side_effect = ValueError("CLI unavailable")
        rt.client.run.side_effect = [
            CommandResult(
                True,
                stdout=json.dumps(
                    {"result": {"agents": [{"agent": "agent", "pane_id": "w1:p1", "status": "idle"}]}}
                ),
            ),
            CommandResult(True, stdout='{"result":{"workspaces":[]}}'),
        ]
        agents = await rt.agents()
        assert len(agents) == 1
        expected = "resolved" if pane_rows and len(pane_rows) == 1 else ""
        assert agents[0].terminal_id == expected

    asyncio.run(scenario())


def test_enriched_agent_can_receive_a_prompt_after_pane_revalidation():
    async def scenario():
        rt = runtime()
        del rt.agents
        rt.panes = AsyncMock(return_value=[{"pane_id": "w1:p1", "terminal_id": "resolved"}])

        async def run(args):
            if args == ["agent", "list"]:
                return CommandResult(
                    True,
                    stdout=json.dumps(
                        {"result": {"agents": [{"agent": "agent", "pane_id": "w1:p1", "status": "idle"}]}}
                    ),
                )
            if args == ["workspace", "list"]:
                return CommandResult(True, stdout='{"result":{"workspaces":[]}}')
            return CommandResult(True)

        rt.client.run.side_effect = run
        target = await rt.find("w1:p1", agent_name=True)
        await rt.write(1, target, text="task", prompt=True)
        assert rt.client.run.call_args.args[0] == ["agent", "prompt", "w1:p1", "task"]

    asyncio.run(scenario())


@pytest.mark.parametrize("prefix", ["/reply w1:p1 ", "/reply w1 ", "/reply ", "/reply@bridge "])
def test_reply_keeps_multiline_payload_exactly(prefix):
    async def scenario():
        rt = runtime()
        payload = 'line1 "quoted" 😀\n  line2\n\tline3  \n'
        ctx = context()
        ctx.args = (prefix + payload).split()[1:]
        await rt.command(update(prefix + payload), ctx)
        assert rt.client.run.call_args_list[0].args[0] == ["pane", "send-text", "w1:p1", payload]
        assert rt.client.run.call_args_list[1].args[0] == ["pane", "send-keys", "w1:p1", "enter"]

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["/reply", "/reply \n\t", "/reply w1:p1", "/reply w1:p1 \n\t"])
def test_empty_reply_never_writes_or_falls_back(text):
    async def scenario():
        rt = runtime()
        ctx = context()
        ctx.args = text.split()[1:]
        upd = update(text)
        await rt.command(upd, ctx)
        rt.client.run.assert_not_awaited()
        assert "Cú pháp" in upd.effective_message.reply_text.call_args.args[0]

    asyncio.run(scenario())


@pytest.mark.parametrize("delay", [60, timedelta(seconds=60)])
def test_retry_after_defers_whole_chat_without_spending_other_deliveries_attempts(delay):
    async def scenario():
        rt = runtime()
        rt.config = replace(rt.config, notification_chats=frozenset({1, 2}))
        first, second = agent(), agent(pane="w1:p2")
        rt.agents.return_value = [first, second]
        sent = []

        async def send(bot, chat, current, kind):
            sent.append((chat, current.pane))
            if chat == 1 and len([c for c, _ in sent if c == 1]) == 1:
                raise RetryAfter(delay)

        rt.send_card = send
        now = [0]
        monitor = Monitor(rt, clock=lambda: now[0])
        ctx = SimpleNamespace(bot=object())
        await monitor.tick(ctx)
        assert sent == [(1, first.pane), (2, first.pane), (2, second.pane)]
        assert [d.attempts for d in monitor.pending] == [1, 0]
        now[0] = 59
        await monitor.tick(ctx)
        assert len(sent) == 3
        now[0] = 60
        await monitor.tick(ctx)
        assert sent[-2:] == [(1, first.pane), (1, second.pane)]
        assert not monitor.pending and not monitor.chat_due

    asyncio.run(scenario())


def test_chat_cooldown_covers_new_events_and_cancels_stale_deliveries():
    async def scenario():
        rt = runtime()
        rt.send_card = AsyncMock(side_effect=RetryAfter(60))
        now = [0]
        monitor = Monitor(rt, clock=lambda: now[0])
        ctx = SimpleNamespace(bot=object())
        await monitor.tick(ctx)
        now[0] = 10
        rt.agents.return_value = [agent("working", seq=2), agent(pane="w1:p2")]
        await monitor.tick(ctx)
        rt.send_card.assert_awaited_once()
        assert len(monitor.pending) == 1
        assert monitor.pending[0].agent.pane == "w1:p2"
        assert monitor.pending[0].attempts == 0
        now[0] = 60
        rt.send_card.side_effect = None
        await monitor.tick(ctx)
        assert rt.send_card.await_count == 2
        assert not monitor.pending and not monitor.chat_due

    asyncio.run(scenario())


def test_retry_after_exhaustion_still_holds_chat_until_deadline():
    async def scenario():
        rt = runtime()
        rt.send_card = AsyncMock(side_effect=RetryAfter(60))
        now = [0]
        monitor = Monitor(rt, clock=lambda: now[0])
        ctx = SimpleNamespace(bot=object())
        for instant in (0, 60, 120, 180):
            now[0] = instant
            await monitor.tick(ctx)
        assert rt.send_card.await_count == 4 and not monitor.pending
        assert monitor.chat_due == {1: 240}
        now[0] = 200
        rt.agents.return_value = [agent(pane="w1:p2")]
        await monitor.tick(ctx)
        assert rt.send_card.await_count == 4
        assert monitor.pending[0].attempts == 0
        now[0] = 240
        rt.send_card.side_effect = None
        await monitor.tick(ctx)
        assert rt.send_card.await_count == 5
        assert not monitor.pending and not monitor.chat_due

    asyncio.run(scenario())
