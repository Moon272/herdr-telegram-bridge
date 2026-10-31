from dataclasses import replace

import pytest

from herdr_bridge.state import Agent, RateLimiter, Registry, transitions


def agent(status="blocked", seq=1, completion=0, pane="w1:p1"):
    return Agent("agent", pane, "w1", status, status, seq, completion)


def bound(registry, chat=1, message=10, snapshot=None):
    meta = registry.draft(chat, snapshot or agent(), "blocked")
    meta.actions["0"] = ("choice", "n")
    registry.bind(meta, message)
    return meta


def test_chat_message_pair_prevents_collisions():
    registry = Registry()
    first = bound(registry)
    second = bound(registry, chat=2, snapshot=agent(pane="w2:p1"))
    assert registry.reply(1, 10) is first
    assert registry.reply(2, 10) is second
    with pytest.raises(ValueError):
        registry.callback(f"v1:{first.token}:0", 2, 10)


def test_expiration_bounds_restart_and_closed_panes():
    now = [0]
    registry = Registry(limit=2, clock=lambda: now[0])
    first = bound(registry)
    bound(registry, message=11)
    last = bound(registry, message=12)
    assert registry.reply(1, 10) is None and first.token not in registry.tokens
    with pytest.raises(ValueError):
        Registry().callback(f"v1:{last.token}:0", 1, 12)
    registry.prune(set())
    assert not registry.messages and not registry.tokens
    bound(registry)
    now[0] = 86400
    assert registry.reply(1, 10) is None


def test_stale_consumed_and_unsequenced_controls():
    now = [0]
    registry = Registry(clock=lambda: now[0])
    meta = bound(registry)
    with pytest.raises(ValueError):
        registry.validate(meta, replace(meta.agent, seq=2), True)
    with pytest.raises(ValueError):
        registry.validate(meta, None, True)
    meta.consumed = True
    with pytest.raises(ValueError):
        registry.validate(meta, meta.agent, True)
    meta = bound(registry, message=11, snapshot=agent(seq=None))
    now[0] = 300
    with pytest.raises(ValueError):
        registry.validate(meta, meta.agent, True)
    registry.validate(meta, meta.agent, False)


@pytest.mark.parametrize(
    "previous,current,expected",
    [
        (None, agent(), "blocked"),
        (None, agent("idle"), None),
        (agent("working"), agent("idle"), "idle"),
        (agent(), agent("working"), None),
        (agent(), agent("idle"), "idle"),
        (agent("idle", completion=1), agent("idle", completion=2), "idle"),
        (agent(), agent(), None),
        (agent(), agent(seq=2), "blocked"),
        (agent("working"), agent("unknown"), None),
    ],
)
def test_state_transitions(previous, current, expected):
    assert transitions(previous, current) == expected


def test_status_is_exact_not_substring():
    assert Agent.parse({"status": "inactive"}).status == "unknown"
    assert Agent.parse({"status": "not_ready"}).status == "unknown"


def test_rate_limit_window():
    now = [0]
    limiter = RateLimiter(2, 60, clock=lambda: now[0])
    assert not limiter.limited(1)
    assert not limiter.limited(1)
    assert limiter.limited(1)
    now[0] = 60
    assert not limiter.limited(1)
