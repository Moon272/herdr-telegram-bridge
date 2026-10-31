import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram import ForceReply

from herdr_bridge.app import AmbiguousTarget, target_and_content
from herdr_bridge.client import CommandResult
from herdr_bridge.names import NameStore
from herdr_bridge.render import card
from herdr_bridge.state import Agent
from tests.test_app import context, runtime, update
from tests.test_state import agent


def test_names_persist_by_terminal_not_pane_or_type(tmp_path):
    path = tmp_path / "data" / "agent_names.json"
    store = NameStore(path)
    first = replace(agent(), name="codex", terminal_id="terminal-one")
    second = replace(first, pane="w1:p2", terminal_id="terminal-two")
    store.set(first, "Backend API")
    store.set(second, "Frontend")
    restarted = NameStore(path)
    assert restarted.get(first) == "Backend API"
    assert restarted.get(second) == "Frontend"
    assert restarted.get(replace(first, pane="w9:p4")) == "Backend API"
    assert restarted.get(replace(first, terminal_id="replacement")) == ""
    restarted.set(first, "")
    assert NameStore(path).get(first) == ""


def test_name_validation_and_failed_save_preserve_previous_names(tmp_path):
    store = NameStore(tmp_path / "names.json")
    snapshot = agent()
    store.set(snapshot, "Old")
    for value in ["x" * 81, "bad\nname", "bad\x00name"]:
        with pytest.raises(ValueError):
            store.set(snapshot, value)
    with patch("herdr_bridge.names.os.replace", side_effect=OSError()):
        with pytest.raises(ValueError):
            store.set(snapshot, "New")
    assert store.get(snapshot) == "Old"
    assert NameStore(store.path).get(snapshot) == "Old"
    assert list(tmp_path.iterdir()) == [store.path]


def test_corrupt_names_file_is_not_overwritten(tmp_path):
    path = tmp_path / "names.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        NameStore(path)
    assert path.read_text() == "[]"


def test_display_name_separates_type_context_and_stable_identity():
    snapshot = Agent.parse(
        {
            "agent": "codex",
            "name": "Native label",
            "terminal_id": "t1",
            "pane_id": "w1:p1",
            "cwd": r"C:\code\shop-api",
            "status": "idle",
        }
    )
    assert snapshot.name == "codex" and snapshot.display_name == "Native label"
    renamed = replace(snapshot, alias="Backend API", workspace_label="Development")
    assert renamed.identity == snapshot.identity
    text = card(renamed)
    assert all(value in text for value in ["Backend API", "codex", "shop-api", "Development", "w1:p1"])
    assert replace(snapshot, label="").display_name == "shop-api · codex"
    assert replace(snapshot, terminal_id="new").identity != snapshot.identity


def test_agent_rows_are_enriched_with_persisted_alias_and_workspace_label():
    async def scenario():
        rt = runtime()
        del rt.agents
        snapshot = Agent.parse({"agent": "codex", "terminal_id": "t1", "pane_id": "w1:p1"})
        rt.names.set(snapshot, "Backend API")
        rt.client.run.side_effect = [
            CommandResult(
                True,
                stdout=json.dumps(
                    {
                        "result": {
                            "agents": [
                                {
                                    "agent": "codex",
                                    "terminal_id": "t1",
                                    "pane_id": "w1:p1",
                                    "workspace_id": "w1",
                                }
                            ]
                        }
                    }
                ),
            ),
            CommandResult(
                True,
                stdout=json.dumps(
                    {"result": {"workspaces": [{"workspace_id": "w1", "label": "Development"}]}}
                ),
            ),
        ]
        rows = await rt.agents()
        assert rows[0].display_name == "Backend API"
        assert rows[0].workspace_label == "Development"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            '/prompt "Backend API" fix "quoted text"\nnext line',
            ("Backend API", 'fix "quoted text"\nnext line'),
        ),
        ("/rename w1:p1 Backend API", ("w1:p1", "Backend API")),
        ('/stop "Backend API"', ("Backend API", "")),
    ],
)
def test_quoted_targets_preserve_prompt_content(text, expected):
    assert target_and_content(text) == expected


def test_unclosed_quote_is_rejected():
    with pytest.raises(ValueError):
        target_and_content('/prompt "Backend API fix')


def test_rename_command_is_not_terminal_input_and_clear_restores_default():
    async def scenario():
        rt = runtime()
        ctx = context()
        await rt.command(update("/rename w1:p1 Backend API"), ctx)
        assert rt.names.get(agent()) == "Backend API"
        rt.client.run.assert_not_awaited()
        await rt.command(update("/rename w1:p1 --clear"), ctx)
        assert rt.names.get(agent()) == ""
        rt.client.run.assert_not_awaited()
        await rt.command(update("/rename w1:p1 Denied", uid=22), ctx)
        assert rt.names.get(agent()) == ""

    asyncio.run(scenario())


def test_alias_and_pane_prompt_and_stop_target_only_the_selected_pane():
    async def scenario():
        rt = runtime()
        first = replace(agent(), name="codex", alias="Backend API")
        second = replace(first, pane="w1:p2", alias="Frontend")
        rt.agents.return_value = [first, second]
        await rt.command(update('/prompt "Backend API" fix "literal quotes"'), context())
        assert rt.client.run.call_args.args[0] == ["agent", "prompt", "w1:p1", 'fix "literal quotes"']
        await rt.command(update("/prompt w1:p2 another task"), context())
        assert rt.client.run.call_args.args[0] == ["agent", "prompt", "w1:p2", "another task"]
        await rt.command(update('/stop "Backend API"'), context())
        assert rt.client.run.call_args.args[0] == ["pane", "send-keys", "w1:p1", "ctrl+c"]

    asyncio.run(scenario())


def test_ambiguous_codex_shows_selection_before_any_command():
    async def scenario():
        rt = runtime()
        rt.agents.return_value = [
            replace(agent(), name="codex", alias="Backend"),
            replace(agent(), name="codex", alias="Frontend", pane="w1:p2"),
        ]
        upd = update("/prompt codex fix bug")
        upd.effective_message.reply_text.side_effect = [SimpleNamespace(message_id=i) for i in [9, 10, 11]]
        await rt.command(upd, context())
        rt.client.run.assert_not_awaited()
        assert upd.effective_message.reply_text.await_count == 3
        meta = rt.registry.reply(1, 11)
        query = SimpleNamespace(
            id="selection",
            data=f"v1:{meta.token}:0",
            message=SimpleNamespace(message_id=11, reply_text=AsyncMock()),
            answer=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )
        upd.callback_query = query
        await rt.callback(upd, context())
        assert rt.client.run.call_args.args[0] == ["agent", "prompt", "w1:p2", "fix bug"]
        assert meta.consumed
        other = rt.registry.reply(1, 10)
        assert other.consumed
        query.id = "second-selection"
        query.data = f"v1:{other.token}:0"
        query.message.message_id = 10
        await rt.callback(upd, context())
        assert rt.client.run.await_count == 1

    asyncio.run(scenario())


def test_duplicate_alias_is_also_ambiguous():
    async def scenario():
        rt = runtime()
        rt.agents.return_value = [
            replace(agent(), alias="Backend"),
            replace(agent(), alias="Backend", pane="w1:p2"),
        ]
        with pytest.raises(AmbiguousTarget):
            await rt.find("backend", agent_name=True)
        rt.client.run.assert_not_awaited()

    asyncio.run(scenario())


def test_rename_button_reply_changes_name_without_sending_keystrokes():
    async def scenario():
        rt = runtime()
        meta = rt.registry.draft(1, agent(), "agents")
        meta.actions["0"] = ("rename", "")
        rt.registry.bind(meta, 10)
        query = SimpleNamespace(
            id="rename",
            data=f"v1:{meta.token}:0",
            message=SimpleNamespace(
                message_id=10, reply_text=AsyncMock(return_value=SimpleNamespace(message_id=11))
            ),
            answer=AsyncMock(),
        )
        upd = update()
        upd.callback_query = query
        await rt.callback(upd, context())
        assert isinstance(query.message.reply_text.call_args.kwargs["reply_markup"], ForceReply)
        assert rt.registry.reply(1, 11).kind == "rename_input"
        response = update(
            "Backend API", reply=SimpleNamespace(message_id=11, from_user=SimpleNamespace(id=99))
        )
        await rt.text(response, context())
        assert rt.names.get(agent()) == "Backend API"
        rt.client.run.assert_not_awaited()
        # Old terminal controls remain bound to the same identity despite a display rename.
        rt.registry.validate(meta, replace(agent(), alias="Backend API"), writing=True)

    asyncio.run(scenario())


def test_stale_selection_does_not_execute_the_saved_prompt():
    async def scenario():
        rt = runtime()
        original = replace(agent(), name="codex", terminal_id="old")
        rt.agents.return_value = [replace(original, terminal_id="replacement")]
        meta = rt.registry.draft(1, original, "selection")
        from herdr_bridge.state import PendingAction

        meta.intent = PendingAction("prompt", "saved task")
        meta.actions["0"] = ("select", "")
        rt.registry.bind(meta, 10)
        upd = update()
        upd.callback_query = SimpleNamespace(
            id="stale",
            data=f"v1:{meta.token}:0",
            message=SimpleNamespace(message_id=10, reply_text=AsyncMock()),
            answer=AsyncMock(),
        )
        await rt.callback(upd, context())
        rt.client.run.assert_not_awaited()
        upd.callback_query.message.reply_text.assert_awaited_once()

    asyncio.run(scenario())


def test_assign_button_reply_submits_prompt_by_pane():
    async def scenario():
        rt = runtime()
        meta = rt.registry.draft(1, agent(), "agents")
        meta.actions["0"] = ("hint", "")
        rt.registry.bind(meta, 10)
        upd = update()
        upd.callback_query = SimpleNamespace(
            id="assign",
            data=f"v1:{meta.token}:0",
            message=SimpleNamespace(
                message_id=10, reply_text=AsyncMock(return_value=SimpleNamespace(message_id=11))
            ),
            answer=AsyncMock(),
        )
        await rt.callback(upd, context())
        response = update("new task", reply=SimpleNamespace(message_id=11, from_user=SimpleNamespace(id=99)))
        await rt.text(response, context())
        assert rt.client.run.call_args.args[0] == ["agent", "prompt", "w1:p1", "new task"]

    asyncio.run(scenario())


def test_compact_status_keeps_same_project_sessions_distinguishable():
    from herdr_bridge.render import agent_summary

    first = replace(agent(), name="codex", cwd=r"C:\code\shop-api")
    second = replace(first, pane="w1:p2")
    text = agent_summary([first, second])
    assert "shop-api (w1:p1) — codex" in text
    assert "shop-api (w1:p2) — codex" in text
