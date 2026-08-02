"""Transport mapping: which path carries a model, and in what order.

House doctrine is **OpenRouter primary, Anthropic backup**. Those are two
routes to the same model with different billing and different failure modes,
so the same catalog entry can be reachable more than one way:

    catalog id                      transports (in preference order)
    anthropic/claude-haiku-4.5  ->  openrouter/anthropic/claude-haiku-4.5   (aggregator)
                                    claude-haiku-4-5                        (direct)

This module owns that mapping and nothing else. Per
`docs/standards/model-routing-policy.md`, a table mapping a catalog slug to a
gateway alias is legitimate configuration; a table asserting which model is
*best* is not. Nothing here ranks models — the resolver does that, and both
transports of one model share its rank.

The aggregator alias is derived from the gateway prefix. Direct aliases cannot
be derived, because the gateway names them itself, so they are configured in
`deployments/<id>/transports.yaml`:

    direct:
      anthropic/claude-haiku-4.5: claude-haiku-4-5
      anthropic/claude-opus-5: claude-opus-5
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

GATEWAY_PREFIX_ENV = "MODEL_GATEWAY_PREFIX"
DEFAULT_GATEWAY_PREFIX = "openrouter/"

# Preference order. Lower sorts first: the aggregator is the primary path, the
# direct provider is the backup, and an unrecognized alias sorts last because
# we cannot say what it is.
AGGREGATOR = 0
DIRECT = 1
UNKNOWN = 2


@dataclass(frozen=True)
class TransportMap:
    """Alias <-> catalog id, plus which path an alias represents."""

    gateway_prefix: str = DEFAULT_GATEWAY_PREFIX
    direct_aliases: dict[str, str] = None  # catalog id -> gateway alias

    def __post_init__(self) -> None:
        if self.direct_aliases is None:
            object.__setattr__(self, "direct_aliases", {})

    @property
    def _alias_to_catalog(self) -> dict[str, str]:
        return {alias: catalog_id for catalog_id, alias in self.direct_aliases.items()}

    def catalog_id(self, alias: str) -> str:
        """The catalog id an alias refers to, whichever path it uses."""
        if self.gateway_prefix and alias.startswith(self.gateway_prefix):
            return alias[len(self.gateway_prefix):]
        return self._alias_to_catalog.get(alias, alias)

    def transport_kind(self, alias: str) -> int:
        if self.gateway_prefix and alias.startswith(self.gateway_prefix):
            return AGGREGATOR
        if alias in self._alias_to_catalog:
            return DIRECT
        return UNKNOWN


def load_transport_map(deployment_dir: Path | None = None) -> TransportMap:
    """Load the transport map from a deployment bundle.

    A malformed file is logged and ignored: losing the backup mapping degrades
    failover, and must not take the process down.
    """
    prefix = os.getenv(GATEWAY_PREFIX_ENV, DEFAULT_GATEWAY_PREFIX)
    direct: dict[str, str] = {}
    path = (deployment_dir / "transports.yaml") if deployment_dir else None
    if path and path.is_file():
        try:
            payload: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            declared = payload.get("direct", {}) if isinstance(payload, dict) else {}
            if not isinstance(declared, dict):
                raise ValueError("transports.yaml `direct` must map catalog id -> gateway alias")
            direct = {str(k): str(v) for k, v in declared.items() if k and v}
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.warning("transports.yaml at %s unusable (%s); no direct backup configured", path, exc)
    return TransportMap(gateway_prefix=prefix, direct_aliases=direct)
