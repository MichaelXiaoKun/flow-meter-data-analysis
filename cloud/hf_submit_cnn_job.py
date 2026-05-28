#!/usr/bin/env python3
"""Submit the CNN waveform autoencoder trainer to Hugging Face Jobs.

This local helper packages the existing trainer source into a self-contained
UV job, downloads the CSV from a Hugging Face Dataset repo inside the remote
job, trains the CNN autoencoder, and uploads artifacts back to a Hub repo.

Typical flow:

1. Authenticate locally: ``hf auth login`` or provide ``HF_TOKEN``.
2. Optionally upload the local CSV to a Dataset repo with ``--upload-csv``.
3. Submit a training job.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
TRAINER_FILES = [
    "train_oneclass_meter_model.py",
    "train_cnn_embedding_analyzer.py",
]


def b64_file(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def build_remote_script(config: dict[str, Any]) -> str:
    encoded_files = {
        rel: b64_file(REPO_ROOT / rel)
        for rel in TRAINER_FILES
    }
    config_json = json.dumps(config, sort_keys=True)
    config_literal = json.dumps(config_json)
    encoded_json = json.dumps(encoded_files, indent=2, sort_keys=True)

    return f'''\
# /// script
# dependencies = [
#   "huggingface-hub>=0.35.0",
#   "numpy>=1.26.0",
#   "torch>=2.0.0",
# ]
# ///

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download


CONFIG = json.loads({config_literal})
SOURCE_FILES = {encoded_json}


def main() -> None:
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN is required so the job can download/upload Hub artifacts.")

    work_dir = Path.cwd()
    for rel, data in SOURCE_FILES.items():
        target = work_dir / rel
        target.write_bytes(base64.b64decode(data))

    csv_path = hf_hub_download(
        repo_id=CONFIG["dataset_repo"],
        repo_type=CONFIG["dataset_repo_type"],
        filename=CONFIG["csv_path"],
        token=token,
    )

    out_dir = work_dir / "outputs"
    out_dir.mkdir(exist_ok=True)
    model_out = out_dir / CONFIG["model_filename"]
    report_out = out_dir / CONFIG["report_filename"]
    analysis_out = out_dir / CONFIG["analysis_filename"]
    manifest_out = out_dir / "job_manifest.json"

    cmd = [
        sys.executable,
        str(work_dir / "train_cnn_embedding_analyzer.py"),
        csv_path,
        "--input-mode", CONFIG["input_mode"],
        "--embedding-dim", str(CONFIG["embedding_dim"]),
        "--epochs", str(CONFIG["epochs"]),
        "--batch-size", str(CONFIG["batch_size"]),
        "--learning-rate", str(CONFIG["learning_rate"]),
        "--weight-decay", str(CONFIG["weight_decay"]),
        "--train-fraction", str(CONFIG["train_fraction"]),
        "--split-mode", CONFIG["split_mode"],
        "--train-labels", CONFIG["train_labels"],
        "--device", CONFIG["device"],
        "--model-out", str(model_out),
        "--report-out", str(report_out),
        "--analysis-out", str(analysis_out),
    ]
    if CONFIG.get("analyze_labels"):
        cmd += ["--analyze-labels", CONFIG["analyze_labels"]]
    if CONFIG.get("max_rows") is not None:
        cmd += ["--max-rows", str(CONFIG["max_rows"])]

    print("Running:", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)

    manifest = {{
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_repo": CONFIG["dataset_repo"],
        "dataset_repo_type": CONFIG["dataset_repo_type"],
        "csv_path": CONFIG["csv_path"],
        "output_repo": CONFIG["output_repo"],
        "output_repo_type": CONFIG["output_repo_type"],
        "output_prefix": CONFIG["output_prefix"],
        "trainer_command": cmd,
        "artifacts": [
            CONFIG["model_filename"],
            CONFIG["report_filename"],
            CONFIG["analysis_filename"],
        ],
    }}
    manifest_out.write_text(json.dumps(manifest, indent=2))

    api = HfApi(token=token)
    api.create_repo(
        repo_id=CONFIG["output_repo"],
        repo_type=CONFIG["output_repo_type"],
        exist_ok=True,
    )
    api.upload_folder(
        folder_path=str(out_dir),
        repo_id=CONFIG["output_repo"],
        repo_type=CONFIG["output_repo_type"],
        path_in_repo=CONFIG["output_prefix"],
        token=token,
    )
    print("Uploaded artifacts to:", CONFIG["output_repo"], CONFIG["output_prefix"], flush=True)


if __name__ == "__main__":
    main()
'''


def require_hub():
    try:
        from huggingface_hub import HfApi, get_token, run_uv_job
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing huggingface_hub. Install with: python3 -m pip install huggingface_hub"
        ) from exc
    return HfApi, get_token, run_uv_job


def upload_csv_if_requested(args: argparse.Namespace, token: str) -> None:
    if not args.upload_csv:
        return
    if args.local_csv is None:
        raise SystemExit("--upload-csv requires --local-csv")
    if not args.local_csv.exists():
        raise SystemExit(f"Local CSV not found: {args.local_csv}")

    HfApi, _, _ = require_hub()
    api = HfApi(token=token)
    api.create_repo(args.dataset_repo, repo_type=args.dataset_repo_type, exist_ok=True)
    print(
        f"Uploading {args.local_csv} -> {args.dataset_repo}/{args.csv_path} "
        f"({args.dataset_repo_type})"
    )
    api.upload_file(
        path_or_fileobj=str(args.local_csv),
        path_in_repo=args.csv_path,
        repo_id=args.dataset_repo,
        repo_type=args.dataset_repo_type,
        token=token,
    )


def default_output_prefix() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"runs/{stamp}"


def build_config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "dataset_repo": args.dataset_repo,
        "dataset_repo_type": args.dataset_repo_type,
        "csv_path": args.csv_path,
        "output_repo": args.output_repo,
        "output_repo_type": args.output_repo_type,
        "output_prefix": args.output_prefix or default_output_prefix(),
        "model_filename": args.model_filename,
        "report_filename": args.report_filename,
        "analysis_filename": args.analysis_filename,
        "input_mode": args.input_mode,
        "embedding_dim": args.embedding_dim,
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "learning_rate": args.learning_rate,
        "weight_decay": args.weight_decay,
        "train_fraction": args.train_fraction,
        "split_mode": args.split_mode,
        "train_labels": args.train_labels,
        "analyze_labels": args.analyze_labels,
        "max_rows": args.max_rows,
        "device": args.remote_device,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-repo", required=True,
                        help="HF Dataset repo containing the training CSV, e.g. user/bluebot-waveforms.")
    parser.add_argument("--dataset-repo-type", default="dataset", choices=["dataset", "model", "space"])
    parser.add_argument("--csv-path", default="BB8100017587.csv",
                        help="Path of the CSV inside --dataset-repo.")
    parser.add_argument("--local-csv", type=Path, default=None,
                        help="Local CSV to upload before submitting the job.")
    parser.add_argument("--upload-csv", action="store_true",
                        help="Upload --local-csv to --dataset-repo/--csv-path before submitting.")
    parser.add_argument("--output-repo", required=True,
                        help="HF repo where training artifacts will be uploaded.")
    parser.add_argument("--output-repo-type", default="model", choices=["model", "dataset", "space"])
    parser.add_argument("--output-prefix", default="",
                        help="Path inside output repo. Default: runs/<UTC timestamp>.")

    parser.add_argument("--flavor", default="cpu-upgrade",
                        help="HF Jobs hardware flavor, e.g. cpu-upgrade, t4-small, l4x1.")
    parser.add_argument("--timeout", default="2h")
    parser.add_argument("--namespace", default=None,
                        help="Optional HF namespace for the job.")

    parser.add_argument("--input-mode", choices=["full", "gate"], default="full")
    parser.add_argument("--embedding-dim", type=int, default=64)
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--weight-decay", type=float, default=1e-4)
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--split-mode", choices=["interleaved", "temporal"], default="interleaved")
    parser.add_argument("--train-labels", default="good")
    parser.add_argument("--analyze-labels", default="")
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--remote-device", choices=["auto", "cpu", "cuda"], default="auto",
                        help="Device requested inside the remote job. Use auto for GPU flavors.")

    parser.add_argument("--model-filename", default="cnn_autoencoder_model.pt")
    parser.add_argument("--report-filename", default="cnn_embedding_report.json")
    parser.add_argument("--analysis-filename", default="cnn_embedding_analysis.jsonl")

    parser.add_argument("--dry-run-script", type=Path, default=None,
                        help="Write the generated remote UV script here and do not submit.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = build_config(args)
    remote_script = build_remote_script(config)
    if args.dry_run_script is not None:
        args.dry_run_script.parent.mkdir(parents=True, exist_ok=True)
        args.dry_run_script.write_text(remote_script)
        print(f"Wrote remote UV script: {args.dry_run_script}")
        print("Remote trainer command will use config:")
        print(json.dumps(config, indent=2))
        return

    _, get_token, run_uv_job = require_hub()
    token = os.environ.get("HF_TOKEN") or get_token()
    if not token:
        raise SystemExit(
            "No Hugging Face token found. Run `hf auth login`, set HF_TOKEN, "
            "or use --dry-run-script to only generate the job script."
        )

    upload_csv_if_requested(args, token)
    secrets = {"HF_TOKEN": token}
    print("Submitting HF Job")
    print(f"  dataset: {args.dataset_repo}/{args.csv_path}")
    print(f"  output:  {config['output_repo']}/{config['output_prefix']}")
    print(f"  flavor:  {args.flavor}, timeout={args.timeout}")
    with tempfile.TemporaryDirectory(prefix="bluebot_hf_job_") as tmp_dir:
        script_path = Path(tmp_dir) / "hf_cnn_train_job.py"
        script_path.write_text(remote_script)
        job = run_uv_job(
            str(script_path),
            flavor=args.flavor,
            timeout=args.timeout,
            namespace=args.namespace,
            secrets=secrets,
            token=token,
        )
    print("Submitted")
    print(f"  id:     {job.id}")
    print(f"  status: {job.status}")
    print(f"  url:    {job.url}")


if __name__ == "__main__":
    main()
