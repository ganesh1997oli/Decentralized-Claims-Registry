"""Explicit deployment mode for anonymous, read-only dissertation demos.

Production remains credential-gated by default. Enabling this mode authorizes
only requests that omit a credential entirely; a supplied credential is still
validated normally. Write routes never consult this module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

PUBLIC_DEMO_ASSESSOR_REFERENCE = "public-demo-read-only"


class PublicDemoConfigurationError(ValueError):
    """Raised when the deployment mode is not an explicit boolean."""


@dataclass(frozen=True)
class PublicDemoAccess:
    """Decide whether one credential-free read may use public demo access."""

    public_read_only: bool

    @classmethod
    def from_settings(cls, settings: Mapping[str, str]) -> PublicDemoAccess:
        """Parse a secure-by-default, explicit true/false setting."""

        configured = settings.get("PUBLIC_DEMO_READ_ONLY", "false").strip().lower()
        if configured not in {"true", "false"}:
            raise PublicDemoConfigurationError(
                "PUBLIC_DEMO_READ_ONLY must be true or false"
            )
        return cls(public_read_only=configured == "true")

    @classmethod
    def from_env(cls) -> PublicDemoAccess:
        """Load the deployment mode from process configuration."""

        return cls.from_settings(os.environ)

    def allows_anonymous_read(self, supplied_credential: str | None) -> bool:
        """Allow only a missing credential when public read-only mode is on."""

        return self.public_read_only and not (supplied_credential or "").strip()
