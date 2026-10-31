"""Configuration is loaded explicitly at startup; importing is side effect free."""

import logging
import math
import os
import shutil
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    allowed_users: frozenset[int]
    notification_chats: frozenset[int]
    executable: str
    command_timeout: float = 15
    rate_calls: int = 15
    rate_period: float = 60
    notify_blocked: bool = True
    notify_idle: bool = True
    poll_interval: float = 3
    connect_timeout: float = 30
    read_timeout: float = 30
    write_timeout: float = 30
    pool_timeout: float = 15
    proxy: str | None = None

    @classmethod
    def load(cls):
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")

        def positive(name, default, cast=float):
            try:
                value = cast(os.getenv(name, str(default)))
                if value <= 0 or not math.isfinite(value):
                    raise ValueError()
                return value
            except ValueError:
                raise ValueError(f"{name} must be a positive number") from None

        def ids(name, positive_only=False):
            result = set()
            for item in os.getenv(name, "").split(","):
                if not item.strip():
                    continue
                try:
                    value = int(item.strip())
                    if value == 0 or (positive_only and value < 0):
                        raise ValueError()
                except ValueError:
                    raise ValueError(f"{name} contains an invalid ID") from None
                result.add(value)
            return frozenset(result)

        def boolean(name):
            value = os.getenv(name, "true").lower().strip()
            if value not in {"true", "false", "1", "0", "yes", "no"}:
                raise ValueError(f"{name} must be true or false")
            return value in {"true", "1", "yes"}

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        users = ids("ALLOWED_TELEGRAM_USER_IDS", True)
        if not token or not users:
            raise ValueError("TELEGRAM_BOT_TOKEN and ALLOWED_TELEGRAM_USER_IDS are required")
        executable = shutil.which(os.getenv("HERDR_EXECUTABLE", "herdr"))
        if not executable:
            raise ValueError("Herdr CLI not found; set HERDR_EXECUTABLE or update PATH")
        return cls(
            token,
            users,
            users | ids("NOTIFICATION_CHAT_IDS"),
            executable,
            positive("HERDR_COMMAND_TIMEOUT_SEC", 15),
            positive("RATE_LIMIT_MAX_CALLS", 15, int),
            positive("RATE_LIMIT_PERIOD_SEC", 60),
            boolean("AUTO_NOTIFY_BLOCKED"),
            boolean("AUTO_NOTIFY_IDLE"),
            positive("POLL_INTERVAL_SEC", 3),
            positive("TELEGRAM_CONNECT_TIMEOUT", 30),
            positive("TELEGRAM_READ_TIMEOUT", 30),
            positive("TELEGRAM_WRITE_TIMEOUT", 30),
            positive("TELEGRAM_POOL_TIMEOUT", 15),
            os.getenv("TELEGRAM_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"),
        )


def configure_logging():
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                log_dir / "audit_bridge.log", maxBytes=10 * 1024 * 1024, backupCount=4, encoding="utf-8"
            ),
        ],
    )
    # HTTP request URLs contain the Telegram token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
