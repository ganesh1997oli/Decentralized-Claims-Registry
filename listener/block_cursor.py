"""Small durable checkpoint used by the blockchain listener."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class BlockCursorError(RuntimeError):
    """Raised when a saved listener checkpoint cannot be trusted."""


@dataclass(frozen=True)
class BlockCursor:
    path: Path
    chain_id: int
    contract_address: str

    def load(self, *, default: int) -> int:
        """Load a matching checkpoint or use the first-run default."""

        if not self.path.exists():
            return default
        try:
            value = json.loads(self.path.read_text())
            if value["chain_id"] != self.chain_id:
                raise BlockCursorError("Checkpoint belongs to a different chain")
            if value["contract_address"].lower() != self.contract_address.lower():
                raise BlockCursorError("Checkpoint belongs to a different contract")
            block_number = value["last_processed_block"]
            if isinstance(block_number, bool) or not isinstance(block_number, int):
                raise BlockCursorError("Checkpoint block number is invalid")
            return block_number
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BlockCursorError(
                f"Could not read checkpoint {self.path}: {exc}"
            ) from exc

    def save(self, block_number: int) -> None:
        """Replace the checkpoint atomically after a full block range succeeds."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "chain_id": self.chain_id,
                    "contract_address": self.contract_address,
                    "last_processed_block": block_number,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        temporary.replace(self.path)
