#!/usr/bin/env python3
from __future__ import annotations

import time
from collections import deque
from typing import Any, Iterable

try:
    import serial
except ImportError:  # pragma: no cover - callers already check pyserial availability.
    serial = None


class SerialReceiveError(RuntimeError):
    """Raised when the serial port fails while receiving protocol lines."""


class SerialLineReader:
    """Read text lines from a pyserial port using an explicit byte buffer."""

    def __init__(self, ser: Any, *, recent_limit: int = 32, max_buffer: int = 65536) -> None:
        self.ser = ser
        self.max_buffer = max_buffer
        self._buffer = bytearray()
        self._recent: deque[str] = deque(maxlen=recent_limit)

    @property
    def recent_lines(self) -> list[str]:
        return list(self._recent)

    def pending_text(self) -> str:
        if not self._buffer:
            return ""
        return self._buffer.decode("utf-8", errors="replace")

    def read_line(self, deadline: float) -> str | None:
        """Return one decoded line before deadline, or None on timeout."""
        while time.monotonic() < deadline:
            line = self._pop_line()
            if line is not None:
                return line

            try:
                waiting = int(getattr(self.ser, "in_waiting", 0) or 0)
                chunk = self.ser.read(max(1, waiting))
            except Exception as exc:
                if _is_serial_exception(exc):
                    raise SerialReceiveError(
                        "Serial receive failed. Recent serial lines:\n{}".format(self.format_recent())
                    ) from exc
                raise

            if not chunk:
                time.sleep(0.001)
                continue
            self._buffer.extend(chunk)
            if len(self._buffer) > self.max_buffer:
                del self._buffer[: len(self._buffer) - self.max_buffer]

        return None

    def format_recent(self, limit: int | None = None) -> str:
        lines = self.recent_lines if limit is None else self.recent_lines[-limit:]
        if self._buffer:
            lines = [*lines, "<partial> " + self.pending_text()]
        return "\n".join(lines)

    def _pop_line(self) -> str | None:
        try:
            newline_index = self._buffer.index(0x0A)
        except ValueError:
            return None

        raw = bytes(self._buffer[:newline_index])
        del self._buffer[: newline_index + 1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        line = raw.decode("utf-8", errors="replace")
        self._recent.append(line)
        return line


def write_command(ser: Any, command: str) -> None:
    ser.write((command.rstrip() + "\r\n").encode())
    ser.flush()


def drain_lines(
    reader: SerialLineReader,
    seconds: float,
    *,
    debug: bool = False,
    markers: Iterable[str] | None = None,
) -> list[str]:
    lines: list[str] = []
    deadline = time.monotonic() + seconds
    while True:
        line = reader.read_line(deadline)
        if line is None:
            return lines
        if debug:
            print("serial:", repr(line))
        lines.append(clean_protocol_line(line, markers) if markers is not None else line.strip())


def clean_protocol_line(line: str, markers: Iterable[str]) -> str:
    for marker in markers:
        index = line.find(marker)
        if index >= 0:
            return line[index:]
    return line.strip()


def _is_serial_exception(exc: Exception) -> bool:
    if isinstance(exc, OSError):
        return True
    if serial is None:
        return False
    serial_exception = getattr(serial, "SerialException", None)
    return serial_exception is not None and isinstance(exc, serial_exception)
