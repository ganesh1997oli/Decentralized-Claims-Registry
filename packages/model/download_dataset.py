"""Download the pinned synthetic motor-claims dataset used for research."""

from __future__ import annotations

import hashlib
import shutil
import ssl
from pathlib import Path
from urllib.request import urlopen

import certifi

DATASET_REPOSITORY = "electricsheepafrica/africa-synth-motor-insurance-claims-all"
DATASET_REVISION = "7b8b6e8526c28ea54ae8e6414666d48d108221a1"
DATASET_FILENAME = "african_motor_claims.csv"
DATASET_SHA256 = "c82a531db73cf72de8791978b29799f53e828ad5891f9a170868f3111c45b457"
DEFAULT_DATASET_PATH = Path(__file__).resolve().parent / "data" / DATASET_FILENAME
DATASET_URL = (
    f"https://huggingface.co/datasets/{DATASET_REPOSITORY}/resolve/"
    f"{DATASET_REVISION}/{DATASET_FILENAME}?download=true"
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_dataset(destination: Path = DEFAULT_DATASET_PATH) -> Path:
    """Download the exact reviewed dataset revision and verify its checksum."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.download")
    ssl_context = ssl.create_default_context(cafile=certifi.where())

    try:
        with (
            urlopen(
                DATASET_URL,
                timeout=120,
                context=ssl_context,
            ) as response,
            temporary.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)

        actual_hash = file_sha256(temporary)
        if actual_hash != DATASET_SHA256:
            raise RuntimeError(
                "The downloaded dataset checksum did not match the reviewed version"
            )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)

    return destination


def main() -> None:
    path = download_dataset()
    print(f"Saved the research dataset to {path}")


if __name__ == "__main__":
    main()
