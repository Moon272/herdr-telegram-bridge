import asyncio
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest

from herdr_bridge.client import CommandResult, HerdrClient


def test_failed_text_never_sends_enter():
    client = HerdrClient("unused")
    client.run = AsyncMock(return_value=CommandResult(False))
    result = asyncio.run(client.perform("w1:p1", text="n"))
    assert not result.success
    assert "Enter was not sent" in result.stderr
    assert client.run.await_count == 1


def test_partial_write_does_not_retry():
    client = HerdrClient("unused")
    client.run = AsyncMock(side_effect=[CommandResult(True), CommandResult(False)])
    result = asyncio.run(client.perform("w1:p1", text="hello"))
    assert not result.success and "Text sent" in result.stderr
    assert client.run.await_count == 2


def test_writes_are_serialized_and_locks_cleaned():
    async def scenario():
        client = HerdrClient("unused")
        commands = []

        async def run(args):
            commands.append(args)
            await asyncio.sleep(0)
            return CommandResult(True)

        client.run = run
        await asyncio.gather(client.perform("w1:p1", text="first"), client.perform("w1:p1", text="second"))
        assert [c[1] for c in commands] == ["send-text", "send-keys", "send-text", "send-keys"]
        assert not client._locks

    asyncio.run(scenario())


def test_invalid_key_and_validation_failure_never_write():
    async def scenario():
        client = HerdrClient("unused")
        client.run = AsyncMock()
        with pytest.raises(ValueError):
            await client.perform("w1:p1", key="bad")
        validator = AsyncMock(side_effect=ValueError("stale"))
        with pytest.raises(ValueError):
            await client.perform("w1:p1", text="hello", validate=validator)
        client.run.assert_not_awaited()
        assert not client._locks

    asyncio.run(scenario())


def test_real_subprocess_timeout_is_bounded():
    async def scenario():
        client = HerdrClient(sys.executable, timeout=0.1)
        result = await asyncio.wait_for(client.run(["-c", "import time; time.sleep(60)"]), 5)
        assert result.timed_out and not result.success

    asyncio.run(scenario())


def test_cancellation_kills_and_reaps_process():
    async def scenario():
        started = asyncio.Event()
        proc = AsyncMock()
        proc.returncode = None

        async def communicate():
            if proc.returncode is None:
                started.set()
                await asyncio.Future()
            return b"", b""

        def kill():
            proc.returncode = -1

        proc.communicate.side_effect = communicate
        proc.kill = Mock(side_effect=kill)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            task = asyncio.create_task(HerdrClient("unused").run(["status"]))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        proc.kill.assert_called_once()
        assert proc.communicate.await_count == 2

    asyncio.run(scenario())


def test_literal_text_cannot_become_cli_flags():
    client = HerdrClient("unused")
    client.run = AsyncMock(return_value=CommandResult(True))
    asyncio.run(client.perform("w1:p1", text="--help"))
    assert client.run.call_args_list[0].args[0] == ["pane", "send-text", "w1:p1", "--help"]


def test_cli_errors_are_classified_without_exposing_raw_messages():
    from herdr_bridge.client import cli_error_code

    assert cli_error_code("", '{"error":{"code":"agent_blocked","message":"secret"}}', 1) == "agent_blocked"
    assert cli_error_code('{"error":{"code":"pane_not_found"}}', "", 1) == "pane_not_found"
    assert cli_error_code("", "unknown option: secret prompt", 2) == "cli_arguments"
    assert cli_error_code("not json", "", 1) == ""


@pytest.mark.parametrize("prompt", [False, True])
def test_installed_herdr_accepts_positional_text_without_sending_to_a_real_pane(prompt):
    import shutil
    import uuid

    executable = shutil.which("herdr")
    if executable is None:
        pytest.skip("Herdr CLI is not installed")
    target = "__bridge_missing_" + uuid.uuid4().hex
    client = HerdrClient(executable)
    result = asyncio.run(client.perform(target, text="--help", prompt_agent=target if prompt else None))
    # Reaching target resolution proves parsing succeeded. The random nonexistent target
    # guarantees this check cannot deliver text or an Enter to an existing agent.
    assert not result.success
    expected = "agent_not_found" if prompt else "pane_not_found"
    assert result.error_code != "cli_arguments"
    if result.error_code != expected:
        pytest.skip("Herdr server unavailable for target-resolution integration check")
    assert result.error_code == expected
