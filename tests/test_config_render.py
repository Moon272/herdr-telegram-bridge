import html
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from herdr_bridge.config import Config
from herdr_bridge.render import card, chunks, keyboard
from herdr_bridge.state import Registry
from tests.test_state import agent


def configure(monkeypatch):
    monkeypatch.setattr("herdr_bridge.config.load_dotenv", lambda *args: None)
    for name in list(os.environ):
        if name.startswith(("TELEGRAM_", "HERDR_", "RATE_LIMIT_", "AUTO_NOTIFY_")) or name in {
            "POLL_INTERVAL_SEC",
            "ALLOWED_TELEGRAM_USER_IDS",
            "NOTIFICATION_CHAT_IDS",
        }:
            monkeypatch.delenv(name)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "1,2")
    monkeypatch.setenv("HERDR_EXECUTABLE", sys.executable)


def test_config_validation_and_defaults(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("NOTIFICATION_CHAT_IDS", "-123,3")
    cfg = Config.load()
    assert cfg.notification_chats == {1, 2, -123, 3}
    assert cfg.command_timeout == 15


@pytest.mark.parametrize(
    "name,value",
    [
        ("POLL_INTERVAL_SEC", "0"),
        ("TELEGRAM_READ_TIMEOUT", "nan"),
        ("RATE_LIMIT_MAX_CALLS", "-1"),
        ("HERDR_COMMAND_TIMEOUT_SEC", "bad"),
        ("ALLOWED_TELEGRAM_USER_IDS", "1,oops"),
        ("NOTIFICATION_CHAT_IDS", "0"),
        ("AUTO_NOTIFY_IDLE", "maybe"),
        ("HERDR_EXECUTABLE", "missing-unique-herdr-binary"),
        ("TELEGRAM_BOT_TOKEN", ""),
    ],
)
def test_invalid_config_fails_clearly(monkeypatch, name, value):
    configure(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Config.load()


def test_import_is_side_effect_free(tmp_path):
    root = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": root, "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; before=sys.stdout.encoding; import bridge; "
            "assert before == sys.stdout.encoding; "
            "import logging; assert not logging.getLogger().handlers",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    assert list(tmp_path.iterdir()) == []


def test_html_escaping_long_output_and_unicode_limits():
    dangerous = "<script>&`_*😀"
    snapshot = replace(agent(), name=dangerous, title=dangerous * 100)
    text = card(snapshot, dangerous * 1000)
    assert "<script>" not in text and "&lt;script&gt;" in text
    import re

    visible = html.unescape(re.sub("<[^>]+>", "", text))
    assert len(visible.encode("utf-16-le")) // 2 < 4096
    output = dangerous * 2000
    parts = list(chunks(output))
    assert "".join(parts) == output
    assert all(len(p.encode("utf-16-le")) // 2 <= 3000 for p in parts)


def test_callback_sizes_and_stable_action_ids():
    registry = Registry()
    meta = registry.draft(1, replace(agent(), name="😀" * 1000), "blocked")
    markup = keyboard(meta, "[y/n]")
    old = dict(meta.actions)
    keyboard(meta, "1. other choice", "tui")
    assert all(meta.actions[k] == v for k, v in old.items())
    assert all(len(b.callback_data.encode()) <= 64 for row in markup.inline_keyboard for b in row)
