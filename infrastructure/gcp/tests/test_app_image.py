"""Lock the GCP application image to the gasless runtime it launches."""

from __future__ import annotations

import shlex
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = PROJECT_ROOT / "infrastructure" / "gcp" / "Dockerfile.app"
REQUIRED_GASLESS_PATHS = (
    Path("apps/relayer"),
    Path("apps/contracts/ignition/deployments/sepolia-gasless-v1"),
)


def _copy_sources(dockerfile: Path) -> tuple[Path, ...]:
    logical_lines = dockerfile.read_text(encoding="utf-8").replace("\\\n", " ").splitlines()
    sources: list[Path] = []

    for line in logical_lines:
        tokens = shlex.split(line, comments=True)
        if not tokens or tokens[0].upper() != "COPY":
            continue

        arguments = [token for token in tokens[1:] if not token.startswith("--")]
        sources.extend(Path(source) for source in arguments[:-1])

    return tuple(sources)


def _is_copied(required_path: Path, copy_sources: tuple[Path, ...]) -> bool:
    return any(source == required_path or source in required_path.parents for source in copy_sources)


def test_app_image_contains_gasless_runtime_and_deployment() -> None:
    copy_sources = _copy_sources(DOCKERFILE)
    missing = [
        str(required_path)
        for required_path in REQUIRED_GASLESS_PATHS
        if not _is_copied(required_path, copy_sources)
    ]

    assert not missing, f"Dockerfile.app omits gasless runtime paths: {', '.join(missing)}"
