"""Explicit, secure-by-default modes for supervised dissertation demos.

``PUBLIC_DEMO_READ_ONLY`` keeps the existing anonymous read-only presentation.
``PUBLIC_PROTOTYPE_ASSESSOR`` is a separate and deliberately conspicuous switch
that also permits anonymous off-chain assessor revisions. The latter is meant
only for fictional, supervised prototype sessions; it never grants a wallet,
changes Sepolia state, or represents an attributable production reviewer.

Both modes authorize only requests that omit a credential entirely. Supplying
any value opts the request back into the normal digest-backed authentication
boundary, so an invalid key is never silently accepted as anonymous access.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

PUBLIC_DEMO_ASSESSOR_REFERENCE = "public-demo-read-only"
PUBLIC_PROTOTYPE_ASSESSOR_REFERENCE = "public-prototype-assessor"


class PublicDemoConfigurationError(ValueError):
    """Raised when the deployment mode is not an explicit boolean."""


@dataclass(frozen=True)
class PublicDemoAccess:
    """Keep public presentation modes explicit and disabled by default."""

    public_read_only: bool
    public_prototype_assessor: bool = False

    @classmethod
    def from_settings(cls, settings: Mapping[str, str]) -> PublicDemoAccess:
        """Parse a secure-by-default, explicit true/false setting."""

        def parse_boolean(variable_name: str) -> bool:
            configured = settings.get(variable_name, "false").strip().lower()
            if configured not in {"true", "false"}:
                raise PublicDemoConfigurationError(
                    f"{variable_name} must be true or false"
                )
            return configured == "true"

        return cls(
            public_read_only=parse_boolean("PUBLIC_DEMO_READ_ONLY"),
            public_prototype_assessor=parse_boolean(
                "PUBLIC_PROTOTYPE_ASSESSOR"
            ),
        )

    @classmethod
    def from_env(cls) -> PublicDemoAccess:
        """Load the deployment mode from process configuration."""

        return cls.from_settings(os.environ)

    def allows_anonymous_read(self, supplied_credential: str | None) -> bool:
        """Allow general demo reads only under the existing read-only switch."""

        return self.public_read_only and not (supplied_credential or "").strip()

    def anonymous_assessor_reference(
        self, supplied_credential: str | None
    ) -> str | None:
        """Return the fixed audit identity for one anonymous assessor request.

        Prototype access takes precedence when both presentation switches are
        enabled. That makes every write visibly non-attributable while retaining
        the older read-only identity for deployments that do not enable writes.
        """

        if (supplied_credential or "").strip():
            return None
        if self.public_prototype_assessor:
            return PUBLIC_PROTOTYPE_ASSESSOR_REFERENCE
        if self.public_read_only:
            return PUBLIC_DEMO_ASSESSOR_REFERENCE
        return None

    def allows_anonymous_assessor_write(
        self, supplied_credential: str | None
    ) -> bool:
        """Permit an off-chain write only under the dedicated prototype flag."""

        return self.public_prototype_assessor and not (
            supplied_credential or ""
        ).strip()
