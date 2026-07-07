#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


@dataclass(frozen=True)
class RawBlock:
    route: int
    src: int
    sink: int
    vp: int
    vn: int
    flags: int
    retry: int
    raw_flags: int
    samples: np.ndarray


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot route waveforms from a RA8D1 scanraw log")
    parser.add_argument("scanraw_log", type=Path)
    parser.add_argument("--summary-csv", type=Path, help="route_summary.csv from diagnose_invalid_routes.py")
    parser.add_argument("--routes", nargs="*", type=int, help="specific route indices to plot")
    parser.add_argument("--top-invalid", type=int, default=12, help="plot this many invalid routes when --routes is omitted")
    parser.add_argument("--include-valid", type=int, default=4, help="also plot this many valid reference routes")
    parser.add_argument("--out", type=Path, help="output PNG path")
    parser.add_argument("--cols", type=int, default=4)
    return parser.parse_args()


def clean_line(line: str) -> str:
    for marker in ("FRAME_BEGIN,", "ROUTE,", "ROUTE_STAT,", "END", "SCAN_DONE", "ERR:"):
        index = line.find(marker)
        if index >= 0:
            return line[index:]
    stripped = line.strip()
    if stripped and stripped[0].isdigit():
        return stripped
    return stripped


def parse_scanraw(path: Path) -> list[RawBlock]:
    blocks: list[RawBlock] = []
    current_route: tuple[int, int, int, int] | None = None
    current_stat = (0, 0, 0)
    samples: list[int] = []
    route_index = 0
    for raw_line in path.read_text(errors="replace").splitlines():
        line = clean_line(raw_line)
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
                        route=route_index,
                        src=current_route[0],
                        sink=current_route[1],
                        vp=current_route[2],
                        vn=current_route[3],
                        flags=current_stat[0],
                        retry=current_stat[1],
                        raw_flags=current_stat[2],
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


def read_summary(path: Path | None) -> dict[int, dict[str, str]]:
    if path is None or not path.exists():
        return {}
    with path.open(newline="") as fp:
        return {int(row["route"]): row for row in csv.DictReader(fp)}


def choose_routes(blocks: list[RawBlock], summary: dict[int, dict[str, str]], args: argparse.Namespace) -> list[int]:
    if args.routes:
        return args.routes
    if summary:
        invalid = [int(route) for route, row in summary.items() if row.get("valid") == "0"]
        invalid.sort(key=lambda route: float(summary[route].get("raw_rms") or summary[route].get("rms_code") or 0.0), reverse=True)
        valid = [int(route) for route, row in summary.items() if row.get("valid") == "1"]
        valid.sort(key=lambda route: float(summary[route].get("raw_rms") or summary[route].get("rms_code") or 0.0), reverse=True)
        return invalid[:args.top_invalid] + valid[:args.include_valid]
    invalid = [block.route for block in blocks if block.flags != 0]
    valid = [block.route for block in blocks if block.flags == 0]
    return invalid[:args.top_invalid] + valid[:args.include_valid]


def main() -> int:
    args = parse_args()
    blocks = parse_scanraw(args.scanraw_log)
    by_route = {block.route: block for block in blocks}
    summary = read_summary(args.summary_csv)
    routes = [route for route in choose_routes(blocks, summary, args) if route in by_route]
    if not routes:
        raise RuntimeError("no routes selected")

    cols = max(1, args.cols)
    rows = int(np.ceil(len(routes) / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 4.2, rows * 2.8), squeeze=False)
    for ax in axes.ravel():
        ax.set_visible(False)

    for ax, route in zip(axes.ravel(), routes):
        block = by_route[route]
        ax.set_visible(True)
        x = np.arange(len(block.samples))
        ax.plot(x, block.samples, lw=1.0)
        ax.axhline(2, color="red", lw=0.7, alpha=0.4)
        ax.axhline(1021, color="red", lw=0.7, alpha=0.4)
        mean = float(np.mean(block.samples)) if len(block.samples) else 0.0
        ax.axhline(mean, color="black", lw=0.7, alpha=0.4)
        pp = int(np.max(block.samples) - np.min(block.samples)) if len(block.samples) else 0
        rms = float(np.std(block.samples)) if len(block.samples) else 0.0
        extra = summary.get(route, {})
        valid = extra.get("valid", "raw")
        ax.set_title(
            "r{} {}-{} {}-{} valid={} flags={} retry={} pp={} rms={:.1f}".format(
                route, block.src, block.sink, block.vp, block.vn, valid, block.flags, block.retry, pp, rms
            ),
            fontsize=8,
        )
        ax.set_ylim(-20, 1043)
        ax.grid(True, alpha=0.25)

    fig.tight_layout()
    out = args.out
    if out is None:
        out = args.scanraw_log.with_name(args.scanraw_log.stem + "_waveforms.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=160)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
