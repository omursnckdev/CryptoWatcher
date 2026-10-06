"""Crash-safe JSON state: open trades, cooldowns, daily PnL, dedupe markers."""
from pathlib import Path
import json
import os
import tempfile

HISTORY_LIMIT = 50


def default_state() -> dict:
    return {"trades": {}, "cooldowns": {}, "daily": {}, "history": [], "signaled": {}, "paused": False,
            "tg_offset": 0, "alerts": {}}


class StateStore:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.data = default_state()
        if self.path and self.path.is_file():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.update(loaded)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def record_close(self, record: dict):
        self.data["history"] = (self.data["history"] + [record])[-HISTORY_LIMIT:]
