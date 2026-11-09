"""A bounded, read-only view of this process's standard output and error."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import re
import sys
import threading
import time


def redact(text):
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/\-=]+", r"\1<redacted>", text)
    text = re.sub(r"""(?ix)(["']?(?:token|api[_-]?key|password|secret|authorization)["']?\s*[:=]\s*)
                     (?:"[^"]*"|'[^']*'|[^&\s,]+)""", r"\1<redacted>", text)
    return re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1<redacted>@", text)


class TerminalLogs:
    capacity = 1000
    retention_seconds = 3600

    def __init__(self):
        self.rows = deque(maxlen=self.capacity)
        self.pending = {"out": "", "err": ""}
        self.lock = threading.Lock()
        self.sequence = 0
        self.capturing = False

    def write(self, stream, value):
        with self.lock:
            value = self.pending[stream] + value
            parts = value.splitlines(keepends=True)
            self.pending[stream] = ""
            for line in parts:
                if not line.endswith(("\n", "\r")) and len(line) < 4096:
                    self.pending[stream] = line
                else:
                    self._append(stream, line.rstrip("\r\n")[:4096])

    def flush(self, stream):
        with self.lock:
            if self.pending[stream]:
                self._append(stream, self.pending[stream][:4096])
                self.pending[stream] = ""

    def _append(self, stream, text):
        self.sequence += 1
        self.rows.append({
            "id": self.sequence, "at": datetime.now(timezone.utc).isoformat(),
            "stream": stream, "text": redact(text), "_time": time.monotonic(),
        })

    def snapshot(self, limit=300, stream="all"):
        with self.lock:
            cutoff = time.monotonic() - self.retention_seconds
            while self.rows and self.rows[0]["_time"] < cutoff:
                self.rows.popleft()
            selected = [row for row in self.rows if stream == "all" or row["stream"] == stream]
            return {
                "capturing": self.capturing, "capacity": self.capacity,
                "retention_seconds": self.retention_seconds, "retained": len(self.rows),
                "stdout": sum(row["stream"] == "out" for row in self.rows),
                "stderr": sum(row["stream"] == "err" for row in self.rows),
                "items": [{key: value for key, value in row.items() if key != "_time"}
                          for row in selected[-limit:]],
            }


class Tee:
    def __init__(self, original, logs, stream):
        self.original, self.logs, self.stream = original, logs, stream

    def write(self, value):
        result = self.original.write(value)
        self.logs.write(self.stream, value)
        return result

    def flush(self):
        self.original.flush()
        self.logs.flush(self.stream)

    def __getattr__(self, name):
        return getattr(self.original, name)


terminal_logs = TerminalLogs()


def install_terminal_capture():
    if not terminal_logs.capturing:
        sys.stdout = Tee(sys.stdout, terminal_logs, "out")
        sys.stderr = Tee(sys.stderr, terminal_logs, "err")
        terminal_logs.capturing = True
