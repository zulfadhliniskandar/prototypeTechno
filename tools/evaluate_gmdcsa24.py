"""Evaluate SafeWatch's 4-state YOLOv11-Pose Fall Detector on GMDCSA-24 videos.

Usage:
    python tools/evaluate_gmdcsa24.py                 # Download Subject 1 sample clips & evaluate
    python tools/evaluate_gmdcsa24.py --full-zenodo   # Download full 1.05 GB Zenodo v2.1 archive
    python tools/evaluate_gmdcsa24.py --sensitivity high
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from safewatch.config import DATA_DIR, GMDCSA24_DIR, GMDCSA24_DOI
from safewatch.gmdcsa24 import (
    discover_dataset_clips,
    download_full_zenodo_dataset,
    download_sample_clips,
    evaluate_dataset,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate SafeWatch on GMDCSA-24 dataset")
    parser.add_argument("--dataset-dir", type=Path, default=GMDCSA24_DIR)
    parser.add_argument("--sensitivity", choices=["low", "medium", "high"], default="medium")
    parser.add_argument("--full-zenodo", action="store_true", help="Download full Zenodo v2.1 archive (~1.05 GB)")
    parser.add_argument("--max-clips", type=int, default=None)
    args = parser.parse_args()

    print(f"GMDCSA-24 Reference DOI: {GMDCSA24_DOI}")
    if args.full_zenodo:
        print("Downloading full GMDCSA-24 dataset from Zenodo...")
        download_full_zenodo_dataset(args.dataset_dir)
    elif not discover_dataset_clips(args.dataset_dir):
        print("Downloading GMDCSA-24 Subject 1 sample clips (Fall + ADL)...")
        download_sample_clips(args.dataset_dir)

    report = evaluate_dataset(
        dataset_dir=args.dataset_dir,
        sensitivity=args.sensitivity,
        max_clips=args.max_clips,
    )
    out_path = DATA_DIR / "gmdcsa24_eval_report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n" + "=" * 66)
    print(f"  GMDCSA-24 Evaluation Summary (sensitivity={report['sensitivity']})")
    print("=" * 66)
    print(f"  Total Clips : {report['total_clips']}  |  TP={report['tp']}  FP={report['fp']}  TN={report['tn']}  FN={report['fn']}")
    print(f"  Accuracy    : {report['accuracy_pct']}%  |  Precision={report['precision_pct']}%  |  Recall={report['recall_pct']}%")
    print(f"  Elapsed     : {report['elapsed_sec']}s  |  Report: {out_path}")
    print("-" * 66)
    for c in report["clips"]:
        mark = "PASS" if c["correct"] else "MISS"
        print(
            f"  [{mark}] {c['subject']}/{c['category']}/{c['file']} -> "
            f"pred={'FALL' if c['predicted_fall'] else 'NO_FALL'} | "
            f"states={' -> '.join(c['states_sequence'])} | "
            f"torso={c['max_torso_angle']}deg | body={c['max_body_angle']}deg | "
            f"cancelled={c['cancelled_count']}"
        )


if __name__ == "__main__":
    main()
