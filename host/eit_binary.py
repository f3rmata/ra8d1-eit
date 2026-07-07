#!/usr/bin/env python3
from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Any

MAGIC = b"EITB"
VERSION = 1
TYPE_SCANSTAT = 1
TYPE_RECONFAST = 2
HEADER_SIZE = 32
SCANSTAT_ROW_SIZE = 32
RECONFAST_SUMMARY_SIZE = 32
RECONFAST_NODE_STRIDE = 4

HEADER_STRUCT = struct.Struct("<4sBBHIIHHIHHHH")
ROW_STRUCT = struct.Struct("<HBBBBIIHHHHHHHB3x")
RECONFAST_SUMMARY_STRUCT = struct.Struct("<HHHHffff8x")


@dataclass(frozen=True)
class ScanStatBinRow:
    route_index: int
    src: int
    sink: int
    vp: int
    vn: int
    mean_milli: int
    rms_milli: int
    min_code: int
    max_code: int
    pp_code: int
    overrange_count: int
    valid_count: int
    flags: int
    raw_flags: int
    retry_count: int


@dataclass(frozen=True)
class ScanStatBinFrame:
    frame_id: int
    electrodes: int
    samples: int
    rate_hz: int
    rows: list[ScanStatBinRow]


@dataclass(frozen=True)
class ReconFastBinFrame:
    frame_id: int
    electrodes: int
    routes: int
    nodes: int
    valid: int
    invalid: int
    retry: int
    ds_min: float
    ds_max: float
    ds_abs_p98: float
    rel_l2: float
    ds_values: list[float]


def read_scanstat_frame(ser: Any, timeout: float) -> ScanStatBinFrame:
    deadline = time.monotonic() + timeout
    header = _read_header(ser, deadline)
    (
        magic,
        version,
        frame_type,
        header_len,
        payload_len,
        frame_id,
        electrodes,
        samples,
        rate_hz,
        route_count,
        row_stride,
        payload_crc,
        _reserved,
    ) = HEADER_STRUCT.unpack(header)

    if magic != MAGIC:
        raise RuntimeError("bad binary magic")
    if version != VERSION:
        raise RuntimeError(f"unsupported binary version {version}")
    if frame_type != TYPE_SCANSTAT:
        raise RuntimeError(f"unexpected binary frame type {frame_type}")
    if header_len != HEADER_SIZE:
        raise RuntimeError(f"unexpected binary header size {header_len}")
    if row_stride != SCANSTAT_ROW_SIZE:
        raise RuntimeError(f"unexpected scanstat row size {row_stride}")
    if payload_len != route_count * row_stride:
        raise RuntimeError(f"bad scanstat payload length {payload_len} for {route_count} rows")

    payload = _read_exact(ser, payload_len, deadline)
    actual_crc = crc16_ccitt(payload)
    if actual_crc != payload_crc:
        raise RuntimeError(f"scanstat CRC mismatch: got 0x{actual_crc:04x}, expected 0x{payload_crc:04x}")

    rows = [
        ScanStatBinRow(*ROW_STRUCT.unpack_from(payload, offset))
        for offset in range(0, payload_len, row_stride)
    ]
    return ScanStatBinFrame(frame_id, electrodes, samples, rate_hz, rows)


def read_reconfast_frame(ser: Any, timeout: float) -> ReconFastBinFrame:
    deadline = time.monotonic() + timeout
    header = _read_header(ser, deadline)
    (
        magic,
        version,
        frame_type,
        header_len,
        payload_len,
        frame_id,
        electrodes,
        nodes,
        routes,
        item_count,
        item_stride,
        payload_crc,
        _reserved,
    ) = HEADER_STRUCT.unpack(header)

    if magic != MAGIC:
        raise RuntimeError("bad binary magic")
    if version != VERSION:
        raise RuntimeError(f"unsupported binary version {version}")
    if frame_type != TYPE_RECONFAST:
        raise RuntimeError(f"unexpected binary frame type {frame_type}")
    if header_len != HEADER_SIZE:
        raise RuntimeError(f"unexpected binary header size {header_len}")
    if item_count != nodes:
        raise RuntimeError(f"reconfast item count {item_count} does not match node count {nodes}")
    if item_stride != RECONFAST_NODE_STRIDE:
        raise RuntimeError(f"unexpected reconfast node stride {item_stride}")
    expected_payload_len = RECONFAST_SUMMARY_SIZE + nodes * item_stride
    if payload_len != expected_payload_len:
        raise RuntimeError(f"bad reconfast payload length {payload_len}, expected {expected_payload_len}")

    payload = _read_exact(ser, payload_len, deadline)
    actual_crc = crc16_ccitt(payload)
    if actual_crc != payload_crc:
        raise RuntimeError(f"reconfast CRC mismatch: got 0x{actual_crc:04x}, expected 0x{payload_crc:04x}")

    valid, invalid, retry, _reserved0, ds_min, ds_max, ds_abs_p98, rel_l2 = RECONFAST_SUMMARY_STRUCT.unpack_from(
        payload,
        0,
    )
    ds_values = list(struct.unpack_from("<{}f".format(nodes), payload, RECONFAST_SUMMARY_SIZE))
    return ReconFastBinFrame(
        frame_id=frame_id,
        electrodes=electrodes,
        routes=routes,
        nodes=nodes,
        valid=valid,
        invalid=invalid,
        retry=retry,
        ds_min=ds_min,
        ds_max=ds_max,
        ds_abs_p98=ds_abs_p98,
        rel_l2=rel_l2,
        ds_values=ds_values,
    )


def scanstat_rows_as_dicts(frame: ScanStatBinFrame, vref: float) -> list[dict[str, float | int]]:
    scale = vref / 1023.0
    rows: list[dict[str, float | int]] = []
    for row in frame.rows:
        mean_code = row.mean_milli / 1000.0
        rms_code = row.rms_milli / 1000.0
        rms_v = rms_code * scale
        rows.append(
            {
                "frame": frame.frame_id,
                "route_index": row.route_index,
                "src": row.src,
                "sink": row.sink,
                "vp": row.vp,
                "vn": row.vn,
                "mean_code": mean_code,
                "min_code": row.min_code,
                "max_code": row.max_code,
                "pp_code": row.pp_code,
                "rms_code": rms_code,
                "dc_v": mean_code * scale,
                "amp_v": rms_v * 2.0**0.5,
                "phase_rad": 0.0,
                "rms_v": rms_v,
                "pp_v": row.pp_code * scale,
                "overrange_count": row.overrange_count,
                "valid_count": row.valid_count,
                "flags": row.flags,
                "retry_count": row.retry_count,
                "raw_flags": row.raw_flags,
            }
        )
    return rows


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _read_header(ser: Any, deadline: float) -> bytes:
    window = bytearray()
    while time.monotonic() < deadline:
        byte = ser.read(1)
        if not byte:
            continue
        window.extend(byte)
        if len(window) > len(MAGIC):
            del window[0 : len(window) - len(MAGIC)]
        if bytes(window) == MAGIC:
            rest = _read_exact(ser, HEADER_SIZE - len(MAGIC), deadline)
            return MAGIC + rest
    raise TimeoutError("timed out waiting for EIT binary frame magic")


def _read_exact(ser: Any, length: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < length and time.monotonic() < deadline:
        waiting = int(getattr(ser, "in_waiting", 0) or 0)
        chunk = ser.read(min(length - len(data), max(1, waiting)))
        if chunk:
            data.extend(chunk)
    if len(data) != length:
        raise TimeoutError(f"timed out reading binary payload: got {len(data)}/{length} bytes")
    return bytes(data)
