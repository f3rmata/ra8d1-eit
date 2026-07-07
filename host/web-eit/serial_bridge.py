#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import queue
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import serial


class SerialBridge:
    def __init__(self, port: str, baud: int) -> None:
        self.port = port
        self.baud = baud
        self._serial: serial.Serial | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._clients: list[queue.Queue[dict[str, Any]]] = []
        self._clients_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="serial-reader", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._serial is not None:
                self._serial.close()
                self._serial = None

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
        with self._clients_lock:
            self._clients.append(q)
        q.put({"type": "status", "text": f"bridge subscribed: {self.port} @ {self.baud}"})
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._clients_lock:
            if q in self._clients:
                self._clients.remove(q)

    def write_command(self, command: str) -> None:
        data = command.strip()
        if not data:
            return

        with self._lock:
            if self._serial is None or not self._serial.is_open:
                raise RuntimeError("serial port is not open")
            ser = self._serial
            for ch in data.encode("utf-8"):
                ser.write(bytes([ch]))
                ser.flush()
                time.sleep(0.003)
            time.sleep(0.05)
            for ch in b"\r\n\r":
                ser.write(bytes([ch]))
                ser.flush()
                time.sleep(0.02)

        self._broadcast({"type": "tx", "line": data})

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._broadcast({"type": "status", "text": f"opening {self.port}"})
                ser = serial.Serial(self.port, self.baud, timeout=0.1)
                with self._lock:
                    self._serial = ser
                self._broadcast({"type": "status", "text": f"open {self.port} @ {self.baud}"})
                self._read_loop(ser)
            except Exception as exc:
                self._broadcast({"type": "error", "text": str(exc)})
                time.sleep(1.0)
            finally:
                with self._lock:
                    if self._serial is not None:
                        try:
                            self._serial.close()
                        except Exception:
                            pass
                        self._serial = None

    def _read_loop(self, ser: serial.Serial) -> None:
        buffer = bytearray()
        while not self._stop.is_set() and ser.is_open:
            try:
                chunk = ser.read(max(1, int(ser.in_waiting or 0)))
            except Exception as exc:
                self._broadcast({"type": "error", "text": f"serial read failed: {exc}"})
                return
            if not chunk:
                continue
            buffer.extend(chunk)
            while b"\n" in buffer:
                index = buffer.index(0x0A)
                raw = bytes(buffer[:index])
                del buffer[: index + 1]
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                line = raw.decode("utf-8", errors="replace")
                self._broadcast({"type": "rx", "line": line})
            if len(buffer) > 65536:
                del buffer[:-65536]

    def _broadcast(self, event: dict[str, Any]) -> None:
        with self._clients_lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except queue.Empty:
                    pass


class BridgeHandler(SimpleHTTPRequestHandler):
    bridge: SerialBridge
    static_root: Path

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(self.static_root), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/events":
            self._serve_events()
            return
        super().do_GET()

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/write":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            self.bridge.write_command(body)
        except Exception as exc:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(exc)}).encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def _serve_events(self) -> None:
        q = self.bridge.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                event = q.get()
                payload = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.bridge.unsubscribe(q)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local HTTP/SSE bridge for the RA8D1 EIT web console")
    parser.add_argument("--serial-port", default="/dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=460800)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, default=8765)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    static_root = Path(__file__).resolve().parent
    bridge = SerialBridge(args.serial_port, args.baud)
    BridgeHandler.bridge = bridge
    BridgeHandler.static_root = static_root
    bridge.start()
    server = ThreadingHTTPServer((args.host, args.http_port), BridgeHandler)
    print(f"open http://{args.host}:{args.http_port}/?bridge=1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
