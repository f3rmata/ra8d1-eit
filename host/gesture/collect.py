#!/usr/bin/env python3
"""Guided training data collection for EIT gesture recognition.

Connects to the RA8D1 MCU, captures reconfastbin frames while the user
performs specified gestures, extracts features, and saves labeled data.

Usage:
    python -m host.gesture.collect --port /dev/ttyACM0 \
        --gestures rest,fist,open,flex,ext \
        --reps 5 --frames-per-rep 10 \
        --out-dir gestures/session_001
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import serial

# Ensure host/ is on sys.path for eit_binary and serial_lines imports
_HOST_DIR = Path(__file__).resolve().parents[1]
if str(_HOST_DIR) not in sys.path:
    sys.path.insert(0, str(_HOST_DIR))

# Ensure host/gesture/ is importable
_GESTURE_DIR = Path(__file__).resolve().parent
if str(_GESTURE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_GESTURE_DIR.parent))

from eit_binary import read_reconfast_frame, ReconFastBinFrame  # noqa: E402
from serial_lines import SerialLineReader  # noqa: E402

from gesture.features import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    NUM_NODES,
    extract_features,
    get_node_xy,
    get_region_masks,
)

# ---------------------------------------------------------------------------
# Serial helpers (adapted from plot_recon_live.py for MCU compatibility)
# ---------------------------------------------------------------------------

def _write_command_mcu(ser: serial.Serial, command: str) -> None:
    """Send a CLI command to the MCU, character-by-character with pacing."""
    for ch in command.rstrip().encode():
        ser.write(bytes([ch]))
        ser.flush()
        time.sleep(0.003)
    time.sleep(0.05)
    for ch in b"\r\n\r":
        ser.write(bytes([ch]))
        ser.flush()
        time.sleep(0.02)


def _drain_idle(ser: serial.Serial, idle_s: float = 0.3, max_s: float = 3.0) -> None:
    """Drain serial buffer until idle."""
    deadline = time.monotonic() + max_s
    idle_deadline = time.monotonic() + idle_s
    reader = SerialLineReader(ser)
    while True:
        decoded = reader.read_line(min(deadline, idle_deadline))
        if decoded is None:
            return
        idle_deadline = time.monotonic() + idle_s


def _run_command(ser: serial.Serial, command: str, end_marker: str, timeout: float = 10.0) -> list[str]:
    """Send a command and collect lines until end_marker appears."""
    _drain_idle(ser)
    _write_command_mcu(ser, command)

    lines: list[str] = []
    deadline = time.monotonic() + timeout
    reader = SerialLineReader(ser)
    while True:
        decoded = reader.read_line(deadline)
        if decoded is None:
            raise TimeoutError(
                f"Timed out waiting for {end_marker!r} after {command!r}. "
                f"Recent:\n{reader.format_recent()}"
            )
        line = decoded.strip()
        lines.append(line)
        if line.startswith(end_marker) or line == end_marker:
            return lines
        if line.startswith("ERR:") or line.startswith("bad command"):
            raise RuntimeError(line)


def _init_board(ser: serial.Serial, drive_gain: int = 512, meas_gain: int = 6) -> None:
    """Initialize MCU: power on, set gains, verify model."""
    time.sleep(1.0)
    _drain_idle(ser)

    _run_command(ser, "p 1 0 0", "power ok")
    print("  [power] ok")

    _run_command(ser, f"g {drive_gain} {meas_gain}", "gain drive=")
    print(f"  [gain] drive={drive_gain} meas={meas_gain}")

    _run_command(ser, "recondump", "RECONDUMP,")
    print("  [recondump] ok")


def _capture_baseline(
    ser: serial.Serial,
    electrodes: int = 8,
    frames: int = 5,
    samples: int = 256,
    settle_ms: int = 20,
    rate_hz: int = 200000,
    pp_limit: int = 180,
    retries: int = 1,
    timeout: float = 120.0,
) -> None:
    """Capture a runtime baseline on the MCU."""
    command = f"reconbase {electrodes} {frames} {samples} {settle_ms} {rate_hz} {pp_limit} {retries}"
    print(f"  [baseline] {command}")

    _drain_idle(ser)
    _write_command_mcu(ser, command)

    deadline = time.monotonic() + timeout
    reader = SerialLineReader(ser)
    while True:
        decoded = reader.read_line(deadline)
        if decoded is None:
            raise TimeoutError(f"Timed out waiting for RECONBASE_DONE. Recent:\n{reader.format_recent()}")
        line = decoded.strip()
        if line.startswith("RECONBASE_FRAME,"):
            print(f"  {line}")
        if line.startswith("RECONBASE_DONE,"):
            print(f"  {line}")
            return
        if line.startswith("ERR:"):
            raise RuntimeError(line)


def _read_reconfastbin(
    ser: serial.Serial,
    electrodes: int,
    samples: int,
    settle_ms: int,
    rate_hz: int,
    pp_limit: int,
    retries: int,
    timeout: float,
) -> ReconFastBinFrame | None:
    """Send reconfastbin and read a single binary frame."""
    command = f"reconfastbin {electrodes} {samples} {settle_ms} {rate_hz} {pp_limit} {retries}"
    _drain_idle(ser)
    _write_command_mcu(ser, command)

    try:
        return read_reconfast_frame(ser, timeout)
    except (TimeoutError, RuntimeError) as exc:
        print(f"  [warn] frame read failed: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Collection logic
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Collect labeled EIT gesture data from RA8D1 MCU"
    )
    p.add_argument("--port", required=True, help="Serial port, e.g. /dev/ttyACM0")
    p.add_argument("--baud", type=int, default=460800)
    p.add_argument("--gestures", default="rest,fist,open,flex,ext",
                   help="Comma-separated gesture labels")
    p.add_argument("--reps", type=int, default=5,
                   help="Repetitions per gesture")
    p.add_argument("--frames-per-rep", type=int, default=10,
                   help="Frames to capture per repetition")
    p.add_argument("--electrodes", type=int, default=8)
    p.add_argument("--samples", type=int, default=128,
                   help="ADC samples per route")
    p.add_argument("--settle-ms", type=int, default=5,
                   help="Mux settling time (ms) — lower for faster frame rate")
    p.add_argument("--rate", type=int, default=200000,
                   help="ADC sample rate (Hz)")
    p.add_argument("--pp-limit", type=int, default=180)
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--baseline-frames", type=int, default=5,
                   help="Number of frames for reconbase baseline")
    p.add_argument("--drive-gain", type=int, default=512)
    p.add_argument("--meas-gain", type=int, default=6)
    p.add_argument("--timeout", type=float, default=60.0,
                   help="Per-frame timeout (seconds)")
    p.add_argument("--out-dir", type=Path, default=Path("gestures/session"),
                   help="Output directory for collected data")
    p.add_argument("--min-valid", type=int, default=36,
                   help="Minimum valid_count to keep a frame")
    p.add_argument("--no-extract-features", action="store_true",
                   help="Skip feature extraction, only save raw ds values")
    return p.parse_args()


def collect_session(args: argparse.Namespace) -> Path:
    """Run a full guided collection session, return output directory path."""
    gestures = [g.strip() for g in args.gestures.split(",") if g.strip()]
    if len(gestures) < 2:
        raise ValueError("Need at least 2 gesture labels")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(str(args.out_dir).format(timestamp=timestamp))
    if out_dir.exists():
        out_dir = Path(str(out_dir) + f"_{timestamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Gesture collection session: {out_dir}")
    print(f"Gestures ({len(gestures)}): {gestures}")
    print(f"Repetitions: {args.reps} × {args.frames_per_req} frames = "
          f"{args.reps * args.frames_per_req} per gesture")
    print()

    # Preload region masks for feature extraction
    node_xy = get_node_xy()
    regions = get_region_masks() if not args.no_extract_features else None

    # Open serial
    print(f"Opening {args.port} @ {args.baud}...")
    ser = serial.Serial(args.port, args.baud, timeout=0.1)
    print("Connected.\n")

    # Initialize
    print("=== Initializing MCU ===")
    _init_board(ser, drive_gain=args.drive_gain, meas_gain=args.meas_gain)
    _capture_baseline(
        ser,
        electrodes=args.electrodes,
        frames=args.baseline_frames,
        samples=args.samples,
        settle_ms=args.settle_ms,
        rate_hz=args.rate,
        pp_limit=args.pp_limit,
        retries=args.retries,
    )
    print("MCU ready.\n")

    # Prepare output files
    frames_csv_path = out_dir / "frames.csv"
    features_csv_path = out_dir / "features.csv"
    metadata_path = out_dir / "metadata.json"

    # Open CSV writers
    frames_fp = frames_csv_path.open("w", newline="")
    frames_writer = csv.writer(frames_fp)
    ds_cols = [f"ds_{i}" for i in range(NUM_NODES)]
    frames_writer.writerow(["frame_id", "gesture", "rep", "valid_count",
                             "invalid_count", "retry_count",
                             "ds_min", "ds_max", "ds_abs_p98", "rel_l2",
                             *ds_cols])

    features_fp = None
    features_writer = None
    feature_names: list[str] | None = None
    if not args.no_extract_features:
        features_fp = features_csv_path.open("w", newline="")
        features_writer = csv.writer(features_fp)
        # Write header later (after first feature extraction)

    total_frames = 0
    kept_frames = 0
    frame_id = 0

    try:
        for rep in range(args.reps):
            print(f"\n{'='*60}")
            print(f"Repetition {rep+1}/{args.reps}")
            print(f"{'='*60}")

            for gi, gesture in enumerate(gestures):
                print(f"\n--- Gesture: {gesture} ({gi+1}/{len(gestures)}) ---")

                # Prompt user
                input(f"  Press ENTER when ready to perform '{gesture}'...")

                # Countdown
                for sec in [3, 2, 1]:
                    print(f"  {sec}...", flush=True)
                    time.sleep(1.0)
                print(f"  *** HOLD '{gesture}' ***", flush=True)

                prev_ds = None
                for f in range(args.frames_per_req):
                    frame = _read_reconfastbin(
                        ser,
                        electrodes=args.electrodes,
                        samples=args.samples,
                        settle_ms=args.settle_ms,
                        rate_hz=args.rate,
                        pp_limit=args.pp_limit,
                        retries=args.retries,
                        timeout=args.timeout,
                    )

                    if frame is None:
                        print(f"    frame {f+1}/{args.frames_per_req}: MISSED", flush=True)
                        continue

                    summary_dict = {
                        "valid_count": frame.valid,
                        "invalid_count": frame.invalid,
                        "retry_count": frame.retry,
                        "ds_min": frame.ds_min,
                        "ds_max": frame.ds_max,
                        "ds_abs_p98": frame.ds_abs_p98,
                        "rel_l2": frame.rel_l2,
                    }

                    # Quality filter
                    if frame.valid < args.min_valid:
                        print(f"    frame {f+1}/{args.frames_per_req}: "
                              f"SKIP (valid={frame.valid} < {args.min_valid})", flush=True)
                        continue

                    # Write raw ds values
                    ds = np.array(frame.ds_values, dtype=np.float64)
                    frames_writer.writerow([
                        frame_id, gesture, rep,
                        frame.valid, frame.invalid, frame.retry,
                        frame.ds_min, frame.ds_max, frame.ds_abs_p98,
                        frame.rel_l2,
                        *[f"{v:.9e}" for v in ds],
                    ])
                    frames_fp.flush()

                    # Extract features
                    if not args.no_extract_features and regions is not None:
                        feat = extract_features(
                            ds, summary_dict,
                            prev_ds_node=prev_ds,
                            regions=regions,
                            node_xy=node_xy,
                        )
                        # Write feature header on first extraction
                        if features_writer is not None and feature_names is None:
                            feature_names = feat.names
                            features_writer.writerow(["frame_id", "gesture", "rep", *feature_names])

                        if features_writer is not None:
                            features_writer.writerow([
                                frame_id, gesture, rep,
                                *[f"{v:.9e}" for v in feat.values],
                            ])
                            assert features_fp is not None
                            features_fp.flush()

                    prev_ds = ds
                    frame_id += 1
                    kept_frames += 1
                    total_frames += 1

                    short_gesture = gesture[:6]
                    if not args.no_extract_features:
                        print(f"    frame {f+1}/{args.frames_per_req}: "
                              f"#{frame_id} [{short_gesture}] "
                              f"valid={frame.valid} p98={frame.ds_abs_p98:.4f}",
                              flush=True)
                    else:
                        print(f"    frame {f+1}/{args.frames_per_req}: "
                              f"#{frame_id} [{short_gesture}] "
                              f"valid={frame.valid}",
                              flush=True)

                print(f"  Relax...", flush=True)
                time.sleep(2.0)

    except KeyboardInterrupt:
        print("\n\nCollection interrupted by user.", file=sys.stderr)
    finally:
        frames_fp.close()
        if features_fp is not None:
            features_fp.close()
        ser.close()

    # Write metadata
    metadata = {
        "timestamp": timestamp,
        "gestures": gestures,
        "repetitions": args.reps,
        "frames_per_repetition": args.frames_per_req,
        "electrodes": args.electrodes,
        "samples": args.samples,
        "settle_ms": args.settle_ms,
        "rate_hz": args.rate,
        "pp_limit": args.pp_limit,
        "retries": args.retries,
        "baseline_frames": args.baseline_frames,
        "drive_gain": args.drive_gain,
        "meas_gain": args.meas_gain,
        "total_frames_collected": total_frames,
        "kept_frames": kept_frames,
        "model_version": "jac8-h0.12-kotre-p0.5-lambda0.01-v1",
    }
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")

    print(f"\n{'='*60}")
    print(f"Collection complete: {kept_frames}/{total_frames} frames kept")
    print(f"Data saved to: {out_dir}")
    print(f"  frames.csv  — raw ds_node values")
    if not args.no_extract_features and feature_names is not None:
        print(f"  features.csv — extracted features ({len(feature_names)} features)")
    print(f"  metadata.json")
    return out_dir


def main() -> None:
    args = parse_args()
    collect_session(args)


if __name__ == "__main__":
    main()
