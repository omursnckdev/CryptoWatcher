"""Crash-safe JSON state: open trades, cooldowns, daily PnL, dedupe markers."""
from pathlib import Path
import json
import logging
import os
import tempfile
import time

log = logging.getLogger(__name__)
HISTORY_LIMIT = 50


def default_state() -> dict:
    return {"trades": {}, "cooldowns": {}, "daily": {}, "history": [], "signaled": {}, "paused": False,
            "tg_offset": 0, "alerts": {}, "last_error": None,
            "totals": {"trades": 0, "wins": 0, "realized": 0.0}}


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

    def check_writable(self):
        """Fail fast at startup: a bot that cannot persist its state forgets its trades after a restart."""
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".probe-", suffix=".tmp")
            os.close(fd)
            Path(tmp).unlink()
        except OSError as error:
            raise RuntimeError(f"State directory {self.path.parent} is not writable ({error}). "
                               "With Docker, use the named volume from docker-compose.yml.") from error

    def save(self) -> bool:
        """Never raises for I/O problems (a full disk must not kill a bot that guards open positions); returns success."""
        if self.path is None:
            return True
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            return True
        except OSError as error:
            if tmp:
                Path(tmp).unlink(missing_ok=True)
            if time.time() - getattr(self, "_save_warned", 0) > 60:
                self._save_warned = time.time()
                log.error("Could not save state to %s: %s (continuing with in-memory state)", self.path, error)
            return False
        except BaseException:
            if tmp:
                Path(tmp).unlink(missing_ok=True)
            raise

    def record_close(self, record: dict):
        self.data["history"] = (self.data["history"] + [record])[-HISTORY_LIMIT:]
