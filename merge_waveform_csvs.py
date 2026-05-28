#!/usr/bin/env python3
"""Merge GUI waveform CSV captures while preserving training compatibility."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


PROVENANCE_COLUMNS = [
    "source_dataset",
    "source_file",
    "source_row",
    "confirmed_good",
]


def sample_column_names(header: list[str]) -> list[str]:
    indexed: list[tuple[int, str]] = []
    for name in header:
        if not name.startswith("s_"):
            continue
        try:
            indexed.append((int(name[2:]), name))
        except ValueError:
            continue
    if not indexed:
        raise ValueError("No sample columns named s_0, s_1, ... were found.")
    return [name for _, name in sorted(indexed)]


def parse_input_spec(raw: str) -> tuple[Path, str]:
    if ":" in raw:
        path_raw, source = raw.rsplit(":", 1)
        path = Path(path_raw)
        source = source.strip() or path.stem
    else:
        path = Path(raw)
        source = path.stem
    return path, source


def read_header(path: Path) -> list[str]:
    with path.open(newline="") as f:
        reader = csv.reader(f)
        try:
            return next(reader)
        except StopIteration as exc:
            raise ValueError(f"CSV is empty: {path}") from exc


def ordered_non_sample_columns(headers: list[list[str]], force_label: str) -> list[str]:
    preferred = ["timestamp", "serial", "sq", "sq_age_ms", "label", "n_samples"]
    seen: set[str] = set()
    out: list[str] = []

    def add(name: str) -> None:
        if name in seen or name.startswith("s_") or name in PROVENANCE_COLUMNS:
            return
        seen.add(name)
        out.append(name)

    for name in preferred:
        if force_label and name == "label":
            add(name)
        elif any(name in header for header in headers):
            add(name)
    for header in headers:
        for name in header:
            add(name)
    return out


def merge_csvs(
    inputs: list[tuple[Path, str]],
    output: Path,
    *,
    force_label: str,
    confirmed_good: bool,
) -> dict[str, Any]:
    if not inputs:
        raise ValueError("At least one --input is required.")

    headers = [read_header(path) for path, _ in inputs]
    sample_names = sample_column_names(headers[0])
    for (path, _), header in zip(inputs[1:], headers[1:]):
        if sample_column_names(header) != sample_names:
            raise ValueError(f"Sample columns do not match the first CSV: {path}")

    non_sample = ordered_non_sample_columns(headers, force_label)
    out_header = non_sample + PROVENANCE_COLUMNS + sample_names
    output.parent.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "output": str(output),
        "sample_columns": len(sample_names),
        "inputs": [],
        "rows": 0,
    }

    with output.open("w", newline="") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=out_header, extrasaction="ignore")
        writer.writeheader()

        for path, source in inputs:
            source_rows = 0
            labels: dict[str, int] = {}
            with path.open(newline="") as in_f:
                reader = csv.DictReader(in_f)
                for source_row, row in enumerate(reader, start=1):
                    out = {name: row.get(name, "") for name in non_sample}
                    if force_label:
                        out["label"] = force_label
                    label = str(out.get("label") or row.get("label") or "").strip().lower()
                    labels[label or ""] = labels.get(label or "", 0) + 1
                    out["source_dataset"] = source
                    out["source_file"] = str(path)
                    out["source_row"] = source_row
                    out["confirmed_good"] = "true" if confirmed_good or label == "good" else "false"
                    for name in sample_names:
                        out[name] = row.get(name, "")
                    writer.writerow(out)
                    source_rows += 1

            summary["inputs"].append(
                {
                    "path": str(path),
                    "source_dataset": source,
                    "rows": source_rows,
                    "labels": labels,
                }
            )
            summary["rows"] += source_rows

    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        help="Input CSV path, optionally suffixed as path:source_dataset.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--force-label",
        default="",
        help="If set, overwrite/add the label column with this value for every row.",
    )
    parser.add_argument(
        "--confirmed-good",
        action="store_true",
        help="Mark all merged rows as confirmed_good=true.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    inputs = [parse_input_spec(raw) for raw in args.input]
    summary = merge_csvs(
        inputs,
        args.output,
        force_label=args.force_label.strip().lower(),
        confirmed_good=args.confirmed_good,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
