#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import queue
import math
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import serial

HOST_DIR = Path(__file__).resolve().parents[1]
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

from eit_binary import (
    HEADER_SIZE,
    HEADER_STRUCT,
    MAGIC,
    RECONFAST_SUMMARY_SIZE,
    RECONFAST_SUMMARY_STRUCT,
    TYPE_RECONFAST,
    VERSION,
    crc16_ccitt,
)


class SerialBridge:
    def __init__(
        self,
        port: str,
        baud: int,
        *,
        solver: str,
        mcu_fast: str,
        vref: float,
        timeout: float,
        mesh_h0: float,
        temporal_median: int,
        route_step_limit: float,
        route_guard_history: int,
        route_guard_max_routes: int,
        gesture_model: str | None = None,
    ) -> None:
        self.port = port
        self.baud = baud
        self.solver = solver
        self.mcu_fast = mcu_fast
        self.vref = vref
        self.timeout = timeout
        self.mesh_h0 = mesh_h0
        self.temporal_median = temporal_median
        self.route_step_limit = route_step_limit
        self.route_guard_history = route_guard_history
        self.route_guard_max_routes = route_guard_max_routes
        self._serial: serial.Serial | None = None
        self._lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._stop = threading.Event()
        self._clients: list[queue.Queue[dict[str, Any]]] = []
        self._clients_lock = threading.Lock()
        self._rx_lines: queue.Queue[str] = queue.Queue(maxsize=4096)
        self._suppress_rx_broadcast = False
        self.host_solver = HostPyEITSolver(self) if solver == "host" else None
        self._thread = threading.Thread(target=self._run, name="serial-reader", daemon=True)

        # Gesture classifier (optional)
        self._gesture_clf: Any = None
        self._gesture_regions: Any = None
        self._gesture_node_xy: Any = None
        self._gesture_prev_ds: Any = None
        if gesture_model:
            self._init_gesture(gesture_model)

    def _init_gesture(self, model_path: str) -> None:
        """Lazy-load the gesture classifier and feature extraction dependencies."""
        try:
            from gesture.model import GestureClassifier  # pyright: ignore[reportMissingImports]
            from gesture.features import (  # pyright: ignore[reportMissingImports]
                extract_features,
                get_node_xy,
                get_region_masks,
            )
            self._gesture_clf = GestureClassifier.load(model_path)
            self._gesture_node_xy = get_node_xy()
            self._gesture_regions = get_region_masks()
            self._extract_features = extract_features
            print(f"Gesture model loaded: {model_path}")
            print(f"  Gestures: {list(self._gesture_clf.label_encoder.classes_)}")
        except Exception as exc:
            print(f"Warning: gesture model not loaded: {exc}", file=sys.stderr)
            self._gesture_clf = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._serial is not None:
                self._serial.close()
                self._serial = None

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=4096)
        with self._clients_lock:
            self._clients.append(q)
        q.put({"type": "status", "text": f"bridge subscribed: {self.port} @ {self.baud}"})
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._clients_lock:
            if q in self._clients:
                self._clients.remove(q)

    def write_command(self, command: str) -> None:
        if not self._command_lock.acquire(blocking=False):
            raise RuntimeError("another serial command is already running")
        try:
            data = command.strip()
            if not data:
                return
            if self.host_solver is not None and self.host_solver.can_handle(data):
                self._broadcast({"type": "tx", "line": data})
                self.host_solver.handle_command(data)
                return
            serial_command = self._map_serial_command(data)

            with self._lock:
                if self._serial is None or not self._serial.is_open:
                    raise RuntimeError("serial port is not open")
                ser = self._serial
                for ch in serial_command.encode("utf-8"):
                    ser.write(bytes([ch]))
                    ser.flush()
                    time.sleep(0.003)
                time.sleep(0.05)
                for ch in b"\r\n\r":
                    ser.write(bytes([ch]))
                    ser.flush()
                    time.sleep(0.02)

            self._broadcast({"type": "tx", "line": data})
        finally:
            self._command_lock.release()

    def _map_serial_command(self, command: str) -> str:
        if self.solver == "mcu" and self.mcu_fast == "bin" and command.startswith("reconfast "):
            return "reconfastbin " + command[len("reconfast "):]
        return command

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
            self._process_rx_buffer(buffer)
            if len(buffer) > 65536:
                del buffer[:-65536]

    def _process_rx_buffer(self, buffer: bytearray) -> None:
        while buffer:
            magic_index = buffer.find(MAGIC)
            newline_index = buffer.find(b"\n")

            if magic_index == 0:
                if not self._try_consume_binary_frame(buffer):
                    return
                continue

            if newline_index >= 0 and (magic_index < 0 or newline_index < magic_index):
                raw = bytes(buffer[:newline_index])
                del buffer[: newline_index + 1]
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                line = raw.decode("utf-8", errors="replace")
                self._handle_rx_line(line)
                continue

            if magic_index > 0:
                raw = bytes(buffer[:magic_index]).strip(b"\r\n")
                del buffer[:magic_index]
                if raw:
                    self._handle_rx_line(raw.decode("utf-8", errors="replace"))
                continue

            return

    def _try_consume_binary_frame(self, buffer: bytearray) -> bool:
        if len(buffer) < HEADER_SIZE:
            return False

        header = bytes(buffer[:HEADER_SIZE])
        (
            magic,
            version,
            frame_type,
            header_len,
            payload_len,
            frame_id,
            electrodes,
            nodes_or_samples,
            routes_or_rate,
            item_count,
            item_stride,
            payload_crc,
            _reserved,
        ) = HEADER_STRUCT.unpack(header)
        if magic != MAGIC or version != VERSION or header_len != HEADER_SIZE:
            self._broadcast({"type": "error", "text": "bad EIT binary header"})
            del buffer[:4]
            return True

        total_len = header_len + payload_len
        if len(buffer) < total_len:
            return False

        payload = bytes(buffer[header_len:total_len])
        del buffer[:total_len]
        actual_crc = crc16_ccitt(payload)
        if actual_crc != payload_crc:
            self._broadcast({
                "type": "error",
                "text": f"EIT binary CRC mismatch: got 0x{actual_crc:04x}, expected 0x{payload_crc:04x}",
            })
            return True

        if frame_type == TYPE_RECONFAST:
            self._emit_reconfast_binary(
                frame_id,
                electrodes,
                nodes_or_samples,
                routes_or_rate,
                item_count,
                item_stride,
                payload,
            )
        else:
            self._broadcast({"type": "error", "text": f"unsupported EIT binary frame type {frame_type}"})
        return True

    def clear_rx_lines(self) -> None:
        while True:
            try:
                self._rx_lines.get_nowait()
            except queue.Empty:
                return

    def read_rx_line(self, timeout: float) -> str | None:
        try:
            return self._rx_lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def send_serial_command(self, command: str) -> None:
        with self._lock:
            if self._serial is None or not self._serial.is_open:
                raise RuntimeError("serial port is not open")
            ser = self._serial
            for ch in command.strip().encode("utf-8"):
                ser.write(bytes([ch]))
                ser.flush()
                time.sleep(0.003)
            time.sleep(0.05)
            for ch in b"\r\n\r":
                ser.write(bytes([ch]))
                ser.flush()
                time.sleep(0.02)

    def emit_protocol_line(self, line: str) -> None:
        self._broadcast({"type": "rx", "line": line})

    def begin_internal_capture(self) -> None:
        self._suppress_rx_broadcast = True

    def end_internal_capture(self) -> None:
        self._suppress_rx_broadcast = False

    def _handle_rx_line(self, line: str) -> None:
        try:
            self._rx_lines.put_nowait(line)
        except queue.Full:
            try:
                self._rx_lines.get_nowait()
                self._rx_lines.put_nowait(line)
            except queue.Empty:
                pass
        if not self._suppress_rx_broadcast:
            self._broadcast({"type": "rx", "line": line})

    def _emit_reconfast_binary(
        self,
        frame_id: int,
        electrodes: int,
        nodes: int,
        routes: int,
        item_count: int,
        item_stride: int,
        payload: bytes,
    ) -> None:
        if item_count != nodes or item_stride != 4:
            self._broadcast({"type": "error", "text": "bad reconfastbin item metadata"})
            return
        expected_payload_len = RECONFAST_SUMMARY_SIZE + nodes * item_stride
        if len(payload) != expected_payload_len:
            self._broadcast({"type": "error", "text": "bad reconfastbin payload length"})
            return

        valid, invalid, retry, _reserved0, ds_min, ds_max, ds_abs_p98, rel_l2 = RECONFAST_SUMMARY_STRUCT.unpack_from(
            payload,
            0,
        )
        import struct

        ds_values = struct.unpack_from(f"<{nodes}f", payload, RECONFAST_SUMMARY_SIZE)
        self._broadcast({"type": "rx", "line": f"RECONFAST_BEGIN,{frame_id},{electrodes},{routes},{nodes}"})
        self._broadcast({
            "type": "rx",
            "line": "RECON_SUMMARY,{},{},{},{:.9e},{:.9e},{:.9e},{:.9e}".format(
                valid,
                invalid,
                retry,
                ds_min,
                ds_max,
                ds_abs_p98,
                rel_l2,
            ),
        })
        self._broadcast({
            "type": "rx",
            "line": "RECONFAST_DS," + ",".join("{:.9e}".format(value) for value in ds_values),
        })
        self._broadcast({"type": "rx", "line": "RECONFAST_DONE"})

        # Gesture classification side-channel
        if self._gesture_clf is not None:
            try:
                import numpy as np  # noqa: F811
                ds_arr = np.array(list(ds_values), dtype=np.float64)
                summary = {
                    "valid_count": valid,
                    "invalid_count": invalid,
                    "retry_count": retry,
                    "ds_min": ds_min,
                    "ds_max": ds_max,
                    "ds_abs_p98": ds_abs_p98,
                    "rel_l2": rel_l2,
                }
                feat = self._extract_features(
                    ds_arr,
                    summary,
                    prev_ds_node=self._gesture_prev_ds,
                    regions=self._gesture_regions,
                    node_xy=self._gesture_node_xy,
                )
                label, confidence, all_probas = self._gesture_clf.predict(feat.values)
                self._broadcast({
                    "type": "gesture",
                    "label": label,
                    "confidence": round(confidence, 4),
                    "all_probas": {k: round(v, 4) for k, v in all_probas.items()},
                })
                self._gesture_prev_ds = ds_arr
            except Exception as exc:
                self._broadcast({"type": "error", "text": f"gesture classification failed: {exc}"})

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


class HostPyEITSolver:
    def __init__(self, bridge: SerialBridge) -> None:
        self.bridge = bridge
        host_dir = Path(__file__).resolve().parents[1]
        if str(host_dir) not in sys.path:
            sys.path.insert(0, str(host_dir))
        try:
            import numpy as np
            from pyeit.eit.interp2d import sim2pts
            from pyeit.eit.protocol import PyEITProtocol
            from reconstruct_eit_live import (
                StatFrame,
                StatRow,
                build_solver,
                filter_vector,
                relative_l2,
                rotate_plot_points_s1_up,
                route_key,
                row_is_valid,
                rows_to_vector,
            )
        except Exception as exc:
            raise RuntimeError(
                "host solver requires the project virtualenv with pyEIT; "
                "start with .venv/bin/python3 host/web-eit/serial_bridge.py"
            ) from exc

        self.np = np
        self.sim2pts = sim2pts
        self.PyEITProtocol = PyEITProtocol
        self.StatFrame = StatFrame
        self.StatRow = StatRow
        self.build_solver = build_solver
        self.filter_vector = filter_vector
        self.relative_l2 = relative_l2
        self.rotate_plot_points_s1_up = rotate_plot_points_s1_up
        self.route_key = route_key
        self.row_is_valid = row_is_valid
        self.rows_to_vector = rows_to_vector

        self.signature: list[tuple[int, int, int, int]] | None = None
        self.baseline: dict[tuple[int, int, int, int], float] | None = None
        self.baseline_vector = None
        self.mesh_obj = None
        self.solver = None
        self.vector_history: list[Any] = []

    def can_handle(self, command: str) -> bool:
        return (
            command.startswith("recon ")
            or command.startswith("reconfast ")
            or command.startswith("reconbase ")
        )

    def handle_command(self, command: str) -> None:
        try:
            if command.startswith("reconbase "):
                self._handle_reconbase(command)
            elif command.startswith("reconfast "):
                self._handle_recon(command, fast=True)
            elif command.startswith("recon "):
                self._handle_recon(command, fast=False)
        except Exception as exc:
            self.bridge.emit_protocol_line(f"ERR: host solver failed: {exc}")
            raise

    def _handle_reconbase(self, command: str) -> None:
        parts = command.split()
        electrodes = self._part_int(parts, 1, 8)
        frames = self._part_int(parts, 2, 5)
        samples = self._part_int(parts, 3, 256)
        settle_ms = self._part_int(parts, 4, 20)
        rate = self._part_int(parts, 5, 200000)
        pp_limit = self._part_int(parts, 6, 180)
        retries = self._part_int(parts, 7, 1)

        expected_routes = electrodes * max(0, electrodes - 3)
        self.bridge.emit_protocol_line(f"RECONBASE_BEGIN,{frames},{expected_routes}")

        signature = None
        samples_by_route: dict[tuple[int, int, int, int], list[float]] = {}
        protocol = None
        for index in range(frames):
            frame = self._capture_scanstat(electrodes, samples, settle_ms, rate, pp_limit, retries)
            frame_signature = [self.route_key(row) for row in frame.rows]
            if signature is None:
                signature = frame_signature
                protocol = self._build_protocol_from_rows(frame.rows)
            elif frame_signature != signature:
                raise RuntimeError("route order changed during host baseline")

            valid_count = 0
            invalid_count = 0
            retry_count = 0
            for row in frame.rows:
                retry_count += row.retry_count
                if self.row_is_valid(row):
                    samples_by_route.setdefault(self.route_key(row), []).append(row.amp_v)
                    valid_count += 1
                else:
                    invalid_count += 1
            self.bridge.emit_protocol_line(
                f"RECONBASE_FRAME,{frame.frame_id},{valid_count},{invalid_count},{retry_count}"
            )

        if signature is None or protocol is None:
            raise RuntimeError("no baseline frames captured")
        missing = [key for key in signature if key not in samples_by_route]
        if missing:
            raise RuntimeError(f"baseline missing {len(missing)} route(s); first missing {missing[:3]}")

        self.signature = signature
        self.baseline = {key: float(self.np.median(samples_by_route[key])) for key in signature}
        self.baseline_vector = self.np.asarray([self.baseline[key] for key in signature], dtype=self.np.float64)
        self.mesh_obj, self.solver = self.build_solver(protocol, self.bridge.mesh_h0)
        self.vector_history = []
        self.bridge.emit_protocol_line(f"RECONBASE_DONE,{frames},host,{len(signature)},0")

    def _handle_recon(self, command: str, *, fast: bool) -> None:
        if self.signature is None or self.baseline is None or self.baseline_vector is None:
            raise RuntimeError("host baseline missing; run reconbase first")
        if self.mesh_obj is None or self.solver is None:
            raise RuntimeError("host solver is not initialized")

        parts = command.split()
        electrodes = self._part_int(parts, 1, 8)
        samples = self._part_int(parts, 2, 256)
        settle_ms = self._part_int(parts, 3, 20)
        rate = self._part_int(parts, 4, 200000)
        pp_limit = self._part_int(parts, 5, 180)
        retries = self._part_int(parts, 6, 1)

        frame = self._capture_scanstat(electrodes, samples, settle_ms, rate, pp_limit, retries)
        frame_signature = [self.route_key(row) for row in frame.rows]
        if frame_signature != self.signature:
            raise RuntimeError(f"route order changed in frame {frame.frame_id}")

        raw_vector, valid_count, invalid_count, retry_count = self.rows_to_vector(
            frame.rows,
            self.signature,
            self.baseline,
        )
        _guarded, solve_vector, _replaced, _max_step, _indices = self.filter_vector(
            self.vector_history,
            raw_vector,
            max(1, self.bridge.temporal_median),
            max(0.0, self.bridge.route_step_limit),
            max(0, self.bridge.route_guard_history),
            max(0, self.bridge.route_guard_max_routes),
        )
        ds = self.solver.solve(solve_vector, self.baseline_vector, normalize=True)
        ds_node = self.sim2pts(self.mesh_obj.node, self.mesh_obj.element, self.np.real(ds))
        points = self.rotate_plot_points_s1_up(self.mesh_obj.node)
        ds_min = float(self.np.nanmin(ds_node))
        ds_max = float(self.np.nanmax(ds_node))
        ds_abs_p98 = float(self.np.nanpercentile(self.np.abs(ds_node), 98))
        rel_l2 = float(self.relative_l2(solve_vector, self.baseline_vector))
        node_count = int(len(ds_node))
        route_count = int(len(self.signature))

        if fast:
            self.bridge.emit_protocol_line(f"RECONFAST_BEGIN,{frame.frame_id},{electrodes},{route_count},{node_count}")
        else:
            self.bridge.emit_protocol_line(f"RECON_BEGIN,{frame.frame_id},{electrodes},{route_count},{node_count}")
        self.bridge.emit_protocol_line(
            "RECON_SUMMARY,{},{},{},{:.9e},{:.9e},{:.9e},{:.9e}".format(
                valid_count,
                invalid_count,
                retry_count,
                ds_min,
                ds_max,
                ds_abs_p98,
                rel_l2,
            )
        )

        if fast:
            self.bridge.emit_protocol_line(
                "RECONFAST_DS," + ",".join("{:.9e}".format(float(value)) for value in ds_node)
            )
            self.bridge.emit_protocol_line("RECONFAST_DONE")
        else:
            self.bridge.emit_protocol_line("node,x,y,ds")
            for index, (point, value) in enumerate(zip(points, ds_node)):
                self.bridge.emit_protocol_line(
                    "{},{:.9e},{:.9e},{:.9e}".format(index, float(point[0]), float(point[1]), float(value))
                )
            self.bridge.emit_protocol_line("RECON_DONE")

    def _capture_scanstat(
        self,
        electrodes: int,
        samples: int,
        settle_ms: int,
        rate: int,
        pp_limit: int,
        retries: int,
    ):
        command = f"scanstat {electrodes} {samples} {settle_ms} {rate} {pp_limit} {retries}"
        self.bridge.clear_rx_lines()
        self.bridge.begin_internal_capture()
        rows = []
        frame_id = None
        frame_electrodes = electrodes
        malformed: list[str] = []
        deadline = time.monotonic() + self.bridge.timeout
        scale = self.bridge.vref / 1023.0

        try:
            self.bridge.send_serial_command(command)
            while time.monotonic() < deadline:
                line = self.bridge.read_rx_line(max(0.001, deadline - time.monotonic()))
                if line is None:
                    break
                cleaned = self._clean_scanstat_line(line)
                if not cleaned:
                    continue
                if cleaned.startswith("ERR:") or cleaned.startswith("bad command"):
                    raise RuntimeError(cleaned)
                if cleaned.startswith("STAT_BEGIN,"):
                    parts = cleaned.split(",")
                    frame_id = int(parts[1])
                    if len(parts) >= 3:
                        frame_electrodes = int(parts[2])
                    deadline = time.monotonic() + self.bridge.timeout
                    continue
                if cleaned == "STAT_DONE":
                    if frame_id is None:
                        raise RuntimeError("STAT_DONE before STAT_BEGIN")
                    expected = frame_electrodes * max(0, frame_electrodes - 3)
                    if expected > 0 and len(rows) != expected:
                        raise RuntimeError(
                            f"incomplete STAT frame {frame_id}: got {len(rows)}, expected {expected}; malformed={malformed[-3:]}"
                        )
                    return self.StatFrame(frame_id=frame_id, electrodes=frame_electrodes, rows=rows)
                if cleaned.startswith("route,") or cleaned.startswith("STAT_ROUTE_BEGIN,"):
                    continue

                parts = cleaned.split(",")
                if len(parts) >= 15 and parts[0].isdigit() and frame_id is not None:
                    try:
                        mean_code = float(parts[5])
                        pp_code = float(parts[8])
                        rms_code = float(parts[9])
                        route_index = int(parts[0])
                        src = int(parts[1])
                        sink = int(parts[2])
                        vp = int(parts[3])
                        vn = int(parts[4])
                        overrange_count = int(parts[10])
                        valid_count = int(parts[11])
                        flags = int(parts[12])
                        retry_count = int(parts[13])
                        raw_flags = int(parts[14])
                    except ValueError:
                        malformed.append(cleaned)
                        malformed = malformed[-10:]
                        continue
                    rows.append(
                        self.StatRow(
                            frame=frame_id,
                            route_index=route_index,
                            src=src,
                            sink=sink,
                            vp=vp,
                            vn=vn,
                            mean_code=mean_code,
                            pp_code=pp_code,
                            rms_code=rms_code,
                            amp_v=rms_code * scale * math.sqrt(2.0),
                            overrange_count=overrange_count,
                            valid_count=valid_count,
                            flags=flags,
                            retry_count=retry_count,
                            raw_flags=raw_flags,
                        )
                    )
        finally:
            self.bridge.end_internal_capture()

        raise TimeoutError("timed out waiting for STAT_DONE")

    def _build_protocol_from_rows(self, rows: list[Any]):
        ex_lookup: dict[tuple[int, int], int] = {}
        ex_order: list[tuple[int, int]] = []
        meas_by_exc: list[list[list[int]]] = []
        for row in rows:
            ex_key = (row.src, row.sink)
            if ex_key not in ex_lookup:
                ex_lookup[ex_key] = len(ex_order)
                ex_order.append(ex_key)
                meas_by_exc.append([])
            meas_by_exc[ex_lookup[ex_key]].append([row.vp, row.vn])

        lengths = {len(meas) for meas in meas_by_exc}
        if len(lengths) != 1:
            raise RuntimeError(f"scanstat routes are not rectangular by excitation: {sorted(lengths)}")

        meas_mat = self.np.asarray(meas_by_exc, dtype=int)
        keep_ba = self.np.ones(meas_mat.shape[0] * meas_mat.shape[1], dtype=bool)
        return self.PyEITProtocol(
            ex_mat=self.np.asarray(ex_order, dtype=int),
            meas_mat=meas_mat,
            keep_ba=keep_ba,
        )

    @staticmethod
    def _part_int(parts: list[str], index: int, default: int) -> int:
        if index >= len(parts):
            return default
        return int(parts[index])

    @staticmethod
    def _clean_scanstat_line(line: str) -> str:
        markers = ("STAT_BEGIN", "STAT_DONE", "route,", "STAT_ROUTE_BEGIN", "ERR:", "bad command")
        for marker in markers:
            index = line.find(marker)
            if index >= 0:
                return line[index:].strip()
        stripped = line.strip()
        if stripped and stripped[0].isdigit():
            return stripped
        return stripped


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
            payload = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            return

        payload = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

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
    parser.add_argument(
        "--solver",
        choices=("host", "mcu"),
        default="host",
        help="host uses pyEIT from scanstat frames; mcu passes through MCU recon commands",
    )
    parser.add_argument(
        "--mcu-fast",
        choices=("bin", "text"),
        default="bin",
        help="in --solver mcu mode, map reconfast to reconfastbin by default",
    )
    parser.add_argument("--vref", type=float, default=2.5)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--mesh-h0", type=float, default=0.12)
    parser.add_argument("--temporal-median", type=int, default=3)
    parser.add_argument("--route-step-limit", type=float, default=0.08)
    parser.add_argument("--route-guard-history", type=int, default=5)
    parser.add_argument("--route-guard-max-routes", type=int, default=3)
    parser.add_argument("--gesture-model", default=None,
                        help="Path to gesture model.joblib for real-time classification")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    static_root = Path(__file__).resolve().parent
    bridge = SerialBridge(
        args.serial_port,
        args.baud,
        solver=args.solver,
        mcu_fast=args.mcu_fast,
        vref=args.vref,
        timeout=args.timeout,
        mesh_h0=args.mesh_h0,
        temporal_median=args.temporal_median,
        route_step_limit=args.route_step_limit,
        route_guard_history=args.route_guard_history,
        route_guard_max_routes=args.route_guard_max_routes,
        gesture_model=args.gesture_model,
    )
    BridgeHandler.bridge = bridge
    BridgeHandler.static_root = static_root
    bridge.start()
    server = ThreadingHTTPServer((args.host, args.http_port), BridgeHandler)
    print(f"open http://{args.host}:{args.http_port}/?bridge=1")
    print(f"serial {args.serial_port} @ {args.baud}, solver={args.solver}, mcu_fast={args.mcu_fast}", flush=True)
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
