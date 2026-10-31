"""Event detection and per-chat delivery retries, independent of Telegram handlers."""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from .state import Agent, transitions

log = logging.getLogger(__name__)


@dataclass
class Delivery:
    agent: Agent
    kind: str
    chat: int
    attempts: int = 0
    due: float = 0


class Monitor:
    def __init__(self, runtime, clock=time.monotonic):
        self.runtime = runtime
        self.clock = clock
        self.previous = {}
        self.pending = []
        self.lock = asyncio.Lock()

    async def tick(self, context):
        if self.lock.locked():
            return
        async with self.lock:
            agents = await self.runtime.agents()
            if agents is None:
                return  # An unavailable CLI is not an empty snapshot.
            current = {a.identity: a for a in agents}
            self.runtime.registry.prune(set(current))
            for agent in agents:
                event = transitions(self.previous.get(agent.identity), agent)
                enabled = (
                    event == "blocked"
                    and self.runtime.config.notify_blocked
                    or event == "idle"
                    and self.runtime.config.notify_idle
                )
                if enabled:
                    for chat in sorted(self.runtime.config.notification_chats):
                        if len(self.pending) < 10000:
                            self.pending.append(Delivery(agent, event, chat))
                        else:
                            log.error("Notification queue full; delivery dropped chat=%s", chat)
            self.previous = current
            self.runtime.snapshots = current
            remaining = []
            for delivery in self.pending:
                now = self.clock()
                latest = current.get(delivery.agent.identity)
                # Never deliver stale controls, including an idle event superseded by work.
                if latest is None or latest.version != delivery.agent.version:
                    continue
                if delivery.due > now:
                    remaining.append(delivery)
                    continue
                try:
                    await self.runtime.send_card(context.bot, delivery.chat, latest, delivery.kind)
                except RetryAfter as exc:
                    seconds = exc.retry_after
                    seconds = seconds.total_seconds() if isinstance(seconds, timedelta) else float(seconds)
                    self.retry(delivery, max(seconds, 0), remaining)
                except (BadRequest, Forbidden):
                    # BadRequest subclasses NetworkError in PTB: permanent errors must come first.
                    log.error("Permanent notification failure chat=%s", delivery.chat)
                except NetworkError:
                    delay = (3, 10, 30)[min(delivery.attempts, 2)]
                    self.retry(delivery, delay, remaining)
                except Exception as exc:
                    # Do not leak raw API/CLI exception strings into logs.
                    log.error("Notification failed chat=%s type=%s", delivery.chat, type(exc).__name__)
            self.pending = remaining

    def retry(self, delivery, delay, remaining):
        delivery.attempts += 1
        if delivery.attempts > 3:
            log.error("Notification retries exhausted chat=%s pane=%s", delivery.chat, delivery.agent.pane)
            return
        delivery.due = self.clock() + delay
        remaining.append(delivery)
