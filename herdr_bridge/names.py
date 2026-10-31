"""Persistent display aliases, keyed by terminal identity rather than agent type."""

import json
import os
import tempfile
from pathlib import Path


class NameStore:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.names: dict[str, str] = {}
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in data.items()
                ):
                    raise ValueError()
                self.names = data
            except (OSError, ValueError) as exc:
                raise ValueError(
                    "Không đọc được data/agent_names.json; hãy kiểm tra file tên phiên."
                ) from exc

    def get(self, agent):
        return self.names.get(agent.alias_key, "")

    def set(self, agent, label):
        label = label.strip()
        if len(label) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in label):
            raise ValueError("Tên phiên tối đa 80 ký tự, không chứa xuống dòng hoặc ký tự điều khiển.")
        updated = dict(self.names)
        if label:
            updated[agent.alias_key] = label
        else:
            updated.pop(agent.alias_key, None)
        if self.path is not None:
            temp = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent, delete=False
                ) as file:
                    temp = Path(file.name)
                    json.dump(updated, file, ensure_ascii=False, indent=2)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temp, self.path)
            except OSError as exc:
                raise ValueError("Không lưu được tên phiên; tên cũ được giữ nguyên.") from exc
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)
        self.names = updated
