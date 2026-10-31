import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from herdr_bridge.monitor import Monitor
from herdr_bridge.state import Registry
from tests.test_state import agent


def runtime():
    return SimpleNamespace(
        config=SimpleNamespace(notify_blocked=True, notify_idle=True, notification_chats={1, 2}),
        agents=AsyncMock(return_value=[agent()]),
        registry=Registry(),
        send_card=AsyncMock(),
        snapshots={},
    )


def test_retries_are_per_chat_with_exhaustion():
    async def scenario():
        rt = runtime()
        now = [0]
        monitor = Monitor(rt, clock=lambda: now[0])
        sent = []

        async def send(bot, chat, snapshot, kind):
            sent.append(chat)
            if chat == 2:
                raise NetworkError("offline")

        rt.send_card.side_effect = send
        context = SimpleNamespace(bot=object())
        await monitor.tick(context)
        assert sent == [1, 2] and len(monitor.pending) == 1
        for due in [3, 13, 43]:
            now[0] = due
            await monitor.tick(context)
        assert sent.count(1) == 1 and sent.count(2) == 4
        assert not monitor.pending

    asyncio.run(scenario())


def test_retry_after_and_stale_event_cancellation():
    async def scenario():
        rt = runtime()
        rt.send_card.side_effect = RetryAfter(20)
        now = [0]
        monitor = Monitor(rt, clock=lambda: now[0])
        context = SimpleNamespace(bot=object())
        await monitor.tick(context)
        assert all(d.due == 20 for d in monitor.pending)
        now[0] = 19
        await monitor.tick(context)
        assert rt.send_card.await_count == 2
        rt.agents.return_value = [agent("working", seq=2)]
        await monitor.tick(context)
        assert not monitor.pending

    asyncio.run(scenario())


def test_cli_failure_is_not_empty_snapshot_and_monitor_does_not_overlap():
    async def scenario():
        rt = runtime()
        monitor = Monitor(rt)
        context = SimpleNamespace(bot=object())
        await monitor.tick(context)
        before = monitor.previous.copy()
        rt.agents.return_value = None
        await monitor.tick(context)
        assert monitor.previous == before
        async with monitor.lock:
            await monitor.tick(context)
        assert rt.agents.await_count == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [Forbidden("blocked"), BadRequest("invalid message")])
def test_permanent_error_does_not_retry(error):
    async def scenario():
        rt = runtime()
        rt.send_card.side_effect = error
        monitor = Monitor(rt)
        context = SimpleNamespace(bot=object())
        await monitor.tick(context)
        await monitor.tick(context)
        assert rt.send_card.await_count == 2 and not monitor.pending

    asyncio.run(scenario())
