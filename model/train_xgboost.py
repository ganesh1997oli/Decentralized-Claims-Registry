"""Command-line entry point for the research XGBoost and SHAP workflow."""

from __future__ import annotations

import argparse
from pathlib import Path

from model.download_dataset import (
    DATASET_REVISION,
    DATASET_REPOSITORY,
    DEFAULT_DATASET_PATH,
    download_dataset,
    file_sha256,
)
from model.research_pipeline import (
    load_claims,
    save_training_run,
    train_research_models,
)


DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parent / "artifacts" / "xgboost-african-motor-v1"
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train the leakage-safe motor-claims research model"
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--download",
        action="store_true",
        help="download the pinned dataset when it is not already available",
    )
    arguments = parser.parse_args()

    if not arguments.dataset.exists():
        if not arguments.download:
            parser.error("Dataset not found. Re-run with --download.")
        download_dataset(arguments.dataset)

    claims = load_claims(arguments.dataset)
    run = train_research_models(claims)
    metadata = save_training_run(
        run,
        arguments.output_dir,
        dataset_reference=f"{DATASET_REPOSITORY}@{DATASET_REVISION}",
        dataset_sha256=file_sha256(arguments.dataset),
    )

    print(f"Dataset: {DATASET_REPOSITORY}@{DATASET_REVISION}")
    print(f"Rows: {len(claims):,}")
    print(
        "XGBoost test metrics: "
        f"PR-AUC={metadata['report']['xgboost']['pr_auc']:.3f}, "
        f"precision={metadata['report']['xgboost']['precision']:.3f}, "
        f"recall={metadata['report']['xgboost']['recall']:.3f}, "
        f"F1={metadata['report']['xgboost']['f1']:.3f}"
    )
    print(f"Artifacts: {arguments.output_dir}")


if __name__ == "__main__":
    main()
