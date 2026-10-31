"""Bounded CLI calls and serialized terminal writes."""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Awaitable, Callable

KEYS = frozenset({"enter", "esc", "up", "down", "space", "tab", "ctrl+c"})


@dataclass(frozen=True)
class CommandResult:
    success: bool
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error_code: str = ""


def cli_error_code(stdout, stderr, exit_code):
    for output in (stdout, stderr):
        try:
            code = json.loads(output)["error"]["code"]
            if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code):
                return code
        except (ValueError, KeyError, TypeError):
            pass
    return "cli_arguments" if exit_code == 2 else ""


class HerdrClient:
    def __init__(self, executable, timeout=15):
        self.executable = executable
        self.timeout = timeout
        self._locks = {}

    async def run(self, args):
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self.executable, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, err = await asyncio.wait_for(proc.communicate(), self.timeout)
            stdout = out.decode("utf-8", errors="replace").strip()
            stderr = err.decode("utf-8", errors="replace").strip()
            return CommandResult(
                proc.returncode == 0,
                proc.returncode,
                stdout,
                stderr,
                error_code=cli_error_code(stdout, stderr, proc.returncode) if proc.returncode else "",
            )
        except asyncio.TimeoutError:
            return CommandResult(False, stderr="Herdr command timed out", timed_out=True)
        except OSError as exc:
            return CommandResult(False, stderr=f"Unable to start Herdr: {type(exc).__name__}")
        finally:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.communicate()

    @asynccontextmanager
    async def pane_lock(self, pane):
        # Count waiters too so a lock cannot disappear while another write waits.
        lock, count = self._locks.get(pane, (asyncio.Lock(), 0))
        self._locks[pane] = (lock, count + 1)
        try:
            async with lock:
                yield
        finally:
            _, count = self._locks[pane]
            if count == 1:
                del self._locks[pane]
            else:
                self._locks[pane] = (lock, count - 1)

    async def perform(
        self,
        pane,
        *,
        text=None,
        key=None,
        prompt_agent=None,
        validate: Callable[[], Awaitable[None]] | None = None,
    ):
        if (text is None) == (key is None) or (prompt_agent is not None and text is None):
            raise ValueError("Specify exactly one text or key action")
        if key is not None and key not in KEYS:
            raise ValueError("Unsupported key")
        async with self.pane_lock(pane):
            if validate:
                await validate()
            # Herdr consumes TEXT positionally (even when it starts with '-').
            # Inserting '--' here makes agent prompt treat the actual text as an option.
            if prompt_agent is not None:
                return await self.run(["agent", "prompt", prompt_agent, text])
            if text is not None:
                result = await self.run(["pane", "send-text", pane, text])
                if not result.success:
                    return CommandResult(
                        False,
                        result.exit_code,
                        stderr="send-text failed; Enter was not sent",
                        timed_out=result.timed_out,
                        error_code=result.error_code,
                    )
                result = await self.run(["pane", "send-keys", pane, "enter"])
                if not result.success:
                    return CommandResult(
                        False,
                        result.exit_code,
                        stderr="Text sent, but Enter failed; inspect the pane before retrying",
                        timed_out=result.timed_out,
                        error_code=result.error_code,
                    )
                return result
            return await self.run(["pane", "send-keys", pane, key])
