#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from serial_lines import SerialLineReader, clean_protocol_line

try:
    import serial
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("pyserial is required. Use the project .venv or install pyserial.") from exc

FLAG_NAMES = {
    0x01: "overrange",
    0x02: "low_valid",
    0x04: "pp_abs",
    0x08: "pp_frame",
    0x10: "rms_ratio",
    0x20: "rms_frame",
}


@dataclass(frozen=True)
class StatRow:
    route: int
    src: int
    sink: int
    vp: int
    vn: int
    mean_code: float
    min_code: int
    max_code: int
    pp_code: int
    rms_code: float
    overrange_count: int
    valid_count: int
    flags: int
    retry_count: int
    raw_flags: int


@dataclass(frozen=True)
class RawBlock:
    route_index: int
    src: int
    sink: int
    vp: int
    vn: int
    route_flags: int
    route_retry: int
    route_raw_flags: int
    samples: np.ndarray


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture scanstat/scanraw and diagnose invalid EIT routes")
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=460800)
    parser.add_argument("--electrodes", type=int, default=8)
    parser.add_argument("--samples", type=int, default=128)
    parser.add_argument("--settle-ms", type=int, default=5)
    parser.add_argument("--rate", type=int, default=200000)
    parser.add_argument("--pp-limit", type=int, default=180)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--gain", nargs=2, type=int, metavar=("DRIVE", "MEAS"), default=(512, 6))
    parser.add_argument("--out-dir", type=Path, default=Path("diagnostics/recon_invalid"))
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--skip-raw", action="store_true")
    parser.add_argument("--reset", action="store_true", help="reset the MCU with pyOCD before opening serial")
    parser.add_argument("--pyocd", default="/home/fermata/.local/share/pipx/venvs/pyocd/bin/pyocd")
    parser.add_argument("--target", default="r7fa8d1bh")
    parser.add_argument("--uid", default="0F7A117605A6")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def clean_line(line: str) -> str:
    markers = (
        "STAT_BEGIN,",
        "STAT_DONE",
        "FRAME_BEGIN,",
        "ROUTE,",
        "ROUTE_STAT,",
        "END",
        "SCAN_DONE",
        "ERR:",
        "bad command",
    )
    cleaned = clean_protocol_line(line, markers)
    if cleaned != line.strip():
        return cleaned
    stripped = line.strip()
    if stripped and stripped[0].isdigit():
        return stripped
    return stripped


def run_until(
    ser: "serial.Serial",
    command: str,
    end_marker: str,
    timeout: float,
    debug: bool,
    attempts: int = 2,
    start_marker: str | None = None,
    start_timeout: float = 8.0,
) -> list[str]:
    for attempt in range(attempts):
        drain_idle(ser, idle_s=0.15, max_s=1.0, debug=debug)
        write_command(ser, command)
        lines: list[str] = []
        started = start_marker is None
        deadline = time.monotonic() + timeout
        start_deadline = time.monotonic() + min(start_timeout, timeout)
        reader = SerialLineReader(ser)
        while True:
            decoded = reader.read_line(min(deadline, start_deadline) if not started else deadline)
            if decoded is None:
                if (not started) and time.monotonic() >= start_deadline:
                    break
                break
            line = clean_line(decoded)
            if debug:
                print("serial:", repr(decoded))
            if line:
                if not started:
                    if line.startswith(start_marker):
                        started = True
                    else:
                        continue
                lines.append(line)
            if line == end_marker or line.startswith(end_marker):
                return lines
            if line.startswith("ERR:") or line.startswith("bad command"):
                return lines
        if debug:
            print("retrying command after missing marker:", command)
    raise TimeoutError("Timed out waiting for {} from command {!r}".format(end_marker, command))


def drain_idle(ser: "serial.Serial", idle_s: float, max_s: float, debug: bool) -> None:
    deadline = time.monotonic() + max_s
    idle_deadline = time.monotonic() + idle_s
    reader = SerialLineReader(ser)
    while True:
        decoded = reader.read_line(min(deadline, idle_deadline))
        if decoded is None:
            return
        idle_deadline = time.monotonic() + idle_s
        if debug:
            print("drain:", repr(decoded))


def write_command(ser: "serial.Serial", command: str) -> None:
    for ch in command.rstrip().encode():
        ser.write(bytes([ch]))
        ser.flush()
        time.sleep(0.003)
    time.sleep(0.05)
    for ch in b"\r\n\r":
        ser.write(bytes([ch]))
        ser.flush()
        time.sleep(0.02)


def wait_for_idle(ser: "serial.Serial", timeout: float, idle_s: float, debug: bool) -> None:
    deadline = time.monotonic() + timeout
    idle_deadline = time.monotonic() + idle_s
    reader = SerialLineReader(ser)
    while True:
        decoded = reader.read_line(min(deadline, idle_deadline))
        if decoded is None:
            if time.monotonic() >= deadline:
                break
            return
        idle_deadline = time.monotonic() + idle_s
        if debug:
            print("drain:", repr(decoded))
    raise TimeoutError("Serial did not become idle before initialization")


def wait_for_prompt(ser: "serial.Serial", timeout: float, debug: bool) -> None:
    deadline = time.monotonic() + timeout
    data = b""
    saw_ready = False
    first_prompt_time: float | None = None
    while time.monotonic() < deadline:
        chunk = ser.read(256)
        if chunk:
            data += chunk
            data = data[-1024:]
            if debug:
                print("prompt-drain:", repr(chunk.decode("utf-8", errors="replace")))
            if b"ready" in data:
                saw_ready = True
            if b"eit>" in data and saw_ready:
                return
            if b"eit>" in data and first_prompt_time is None:
                first_prompt_time = time.monotonic()
            if first_prompt_time is not None and (time.monotonic() - first_prompt_time) > 3.0:
                return
            continue
        if first_prompt_time is not None and (time.monotonic() - first_prompt_time) > 3.0:
            return
        time.sleep(0.02)
    raise TimeoutError("Timed out waiting for eit> prompt; last bytes:\n{}".format(
        data.decode("utf-8", errors="replace")
    ))


def init_board(ser: "serial.Serial", args: argparse.Namespace) -> None:
    time.sleep(1.0)
    drain_idle(ser, idle_s=0.3, max_s=3.0, debug=args.debug)
    run_until(ser, "p 1 0 0", "power ok", 5.0, args.debug, attempts=3)
    run_until(ser, "g {} {}".format(args.gain[0], args.gain[1]), "gain drive=", 5.0, args.debug, attempts=3)


def pyocd_reset(args: argparse.Namespace) -> None:
    cmd = [args.pyocd, "reset", "--target", args.target, "--uid", args.uid]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10.0)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print("warning: pyOCD reset failed ({}); continuing with serial sync".format(exc), flush=True)


def parse_scanstat(lines: list[str]) -> list[StatRow]:
    rows: list[StatRow] = []
    for line in lines:
        parts = line.split(",")
        if len(parts) >= 15 and parts[0].isdigit():
            rows.append(
                StatRow(
                    route=int(parts[0]),
                    src=int(parts[1]),
                    sink=int(parts[2]),
                    vp=int(parts[3]),
                    vn=int(parts[4]),
                    mean_code=float(parts[5]),
                    min_code=int(parts[6]),
                    max_code=int(parts[7]),
                    pp_code=int(parts[8]),
                    rms_code=float(parts[9]),
                    overrange_count=int(parts[10]),
                    valid_count=int(parts[11]),
                    flags=int(parts[12]),
                    retry_count=int(parts[13]),
                    raw_flags=int(parts[14]),
                )
            )
    return rows


def parse_scanraw(lines: list[str]) -> list[RawBlock]:
    blocks: list[RawBlock] = []
    current_route: tuple[int, int, int, int] | None = None
    current_stat = (0, 0, 0)
    samples: list[int] = []
    route_index = 0
    for line in lines:
        if line.startswith("ROUTE,"):
            parts = line.split(",")
            current_route = (int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]))
            samples = []
            continue
        if line.startswith("ROUTE_STAT,"):
            parts = line.split(",")
            current_stat = (int(parts[1]), int(parts[2]), int(parts[3]))
            continue
        if line == "END":
            if current_route is not None:
                blocks.append(
                    RawBlock(
                        route_index=route_index,
                        src=current_route[0],
                        sink=current_route[1],
                        vp=current_route[2],
                        vn=current_route[3],
                        route_flags=current_stat[0],
                        route_retry=current_stat[1],
                        route_raw_flags=current_stat[2],
                        samples=np.asarray(samples, dtype=np.float64),
                    )
                )
                route_index += 1
            current_route = None
            samples = []
            continue
        parts = line.split(",")
        if current_route is not None and len(parts) == 3 and parts[0].isdigit():
            samples.append(int(parts[1]))
    return blocks


def flag_text(flags: int) -> str:
    if flags == 0:
        return "ok"
    names = [name for bit, name in FLAG_NAMES.items() if flags & bit]
    unknown = flags & ~sum(FLAG_NAMES)
    if unknown:
        names.append("unknown_0x{:x}".format(unknown))
    return "|".join(names)


def raw_metrics(block: RawBlock) -> dict[str, float | int]:
    samples = block.samples
    if len(samples) == 0:
        return {"route": block.route_index, "raw_n": 0}
    valid = samples[(samples > 2.0) & (samples < 1021.0)]
    if len(valid) == 0:
        valid = samples
    mean = float(np.mean(valid))
    centered = valid - mean
    diffs = np.abs(np.diff(samples)) if len(samples) > 1 else np.asarray([0.0])
    return {
        "route": block.route_index,
        "src": block.src,
        "sink": block.sink,
        "vp": block.vp,
        "vn": block.vn,
        "raw_flags": block.route_flags,
        "raw_retry": block.route_retry,
        "raw_initial_flags": block.route_raw_flags,
        "raw_n": int(len(samples)),
        "rail_count": int(np.count_nonzero((samples <= 2.0) | (samples >= 1021.0))),
        "mean": mean,
        "min": int(np.min(samples)),
        "max": int(np.max(samples)),
        "pp": int(np.max(samples) - np.min(samples)),
        "rms": float(math.sqrt(float(np.mean(centered * centered)))),
        "max_step": int(np.max(diffs)),
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    if args.reset:
        pyocd_reset(args)
    out_dir = args.out_dir / time.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    stat_cmd = "scanstat {} {} {} {} {} {} 0".format(
        args.electrodes, args.samples, args.settle_ms, args.rate, args.pp_limit, args.retries
    )
    raw_cmd = "scanraw {} {} {} {} {} {}".format(
        args.electrodes, args.samples, args.settle_ms, args.rate, args.pp_limit, args.retries
    )

    with serial.Serial(args.port, args.baud, timeout=1.0) as ser:
        time.sleep(0.3)
        init_board(ser, args)
        print("running", stat_cmd, flush=True)
        stat_lines = run_until(
            ser, stat_cmd, "STAT_DONE", args.timeout, args.debug, start_marker="STAT_BEGIN,"
        )
        (out_dir / "scanstat.log").write_text("\n".join(stat_lines) + "\n")
        raw_lines: list[str] = []
        if not args.skip_raw:
            print("running", raw_cmd, flush=True)
            raw_lines = run_until(
                ser, raw_cmd, "SCAN_DONE", args.timeout, args.debug, start_marker="FRAME_BEGIN,"
            )
            (out_dir / "scanraw.log").write_text("\n".join(raw_lines) + "\n")

    stat_rows = parse_scanstat(stat_lines)
    raw_blocks = parse_scanraw(raw_lines)
    raw_by_route = {block.route_index: raw_metrics(block) for block in raw_blocks}

    summary_rows: list[dict[str, object]] = []
    for row in stat_rows:
        raw = raw_by_route.get(row.route, {})
        summary_rows.append(
            {
                "route": row.route,
                "src": row.src,
                "sink": row.sink,
                "vp": row.vp,
                "vn": row.vn,
                "valid": int(row.flags == 0),
                "flags": row.flags,
                "flag_text": flag_text(row.flags),
                "raw_flags": row.raw_flags,
                "raw_flag_text": flag_text(row.raw_flags),
                "retry": row.retry_count,
                "overrange_count": row.overrange_count,
                "valid_count": row.valid_count,
                "pp_code": row.pp_code,
                "rms_code": "{:.3f}".format(row.rms_code),
                "raw_rail_count": raw.get("rail_count", ""),
                "raw_pp": raw.get("pp", ""),
                "raw_rms": "{:.3f}".format(raw["rms"]) if "rms" in raw else "",
                "raw_max_step": raw.get("max_step", ""),
            }
        )
    write_csv(out_dir / "route_summary.csv", summary_rows)
    write_csv(out_dir / "raw_metrics.csv", [raw_metrics(block) for block in raw_blocks])

    invalid = [row for row in stat_rows if row.flags != 0]
    retried = [row for row in stat_rows if row.retry_count > 0 or row.raw_flags != 0]
    print("out_dir", out_dir)
    print("routes={} invalid={} retried={}".format(len(stat_rows), len(invalid), len(retried)))
    if stat_rows:
        pp = np.asarray([row.pp_code for row in stat_rows], dtype=np.float64)
        rms = np.asarray([row.rms_code for row in stat_rows], dtype=np.float64)
        print("pp median={:.1f} max={:.1f}; rms median={:.3f} max={:.3f}".format(
            float(np.median(pp)), float(np.max(pp)), float(np.median(rms)), float(np.max(rms))
        ))
    for row in invalid[:20]:
        print(
            "invalid route {:02d} {}-{} {}-{} flags={} retry={} raw_flags={} pp={} rms={:.3f}".format(
                row.route,
                row.src,
                row.sink,
                row.vp,
                row.vn,
                flag_text(row.flags),
                row.retry_count,
                flag_text(row.raw_flags),
                row.pp_code,
                row.rms_code,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
