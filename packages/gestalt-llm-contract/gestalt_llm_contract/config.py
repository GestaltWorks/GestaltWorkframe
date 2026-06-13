# Copyright 2026 Gestalt Works
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Resolved LLM provider configuration and the canonical resolution rule.

`resolve_provider_config` encodes the one piece of behaviour the platform and
the middleware genuinely share: given the canonical environment values, decide
which provider is primary and whether the Anthropic fallback is active. Each
layer builds its own concrete provider objects from the result, so this package
carries no transport, no SDK, and no runtime coupling.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from . import env

ProviderKind = Literal["openrouter", "local"]


@dataclass(frozen=True)
class ProviderConfig:
    """Normalized provider selection, independent of any transport.

    `kind` is the primary provider. `fallback_enabled` is True only when the
    Anthropic fallback is both opted in and has a key, so callers do not have to
    repeat that two-part check.
    """

    kind: ProviderKind
    base_url: str
    model: str
    api_key: str
    fallback_enabled: bool
    anthropic_api_key: str
    anthropic_model: str


def _text(values: Mapping[str, str], name: str, default: str = "") -> str:
    raw = values.get(name)
    return raw.strip() if isinstance(raw, str) else default


def _bool(values: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = values.get(name)
    if not isinstance(raw, str):
        return default
    return raw.strip().lower() in env.TRUTHY


def resolve_provider_config(values: Mapping[str, str]) -> ProviderConfig:
    """Resolve a `ProviderConfig` from a mapping of canonical env values.

    Precedence: an OpenRouter key selects OpenRouter; otherwise the local
    OpenAI-compatible endpoint is primary. The Anthropic fallback is enabled
    only when explicitly turned on and a key is present.
    """
    openrouter_key = _text(values, env.OPENROUTER_API_KEY)
    anthropic_key = _text(values, env.ANTHROPIC_API_KEY)
    anthropic_model = _text(values, env.ANTHROPIC_MODEL, env.DEFAULT_ANTHROPIC_MODEL)
    fallback = _bool(values, env.ENABLE_CLAUDE_FALLBACK) and bool(anthropic_key)

    if openrouter_key:
        return ProviderConfig(
            kind="openrouter",
            base_url=_text(values, env.OPENROUTER_BASE_URL, env.DEFAULT_OPENROUTER_BASE_URL),
            model=_text(values, env.OPENROUTER_MODEL, env.DEFAULT_OPENROUTER_MODEL),
            api_key=openrouter_key,
            fallback_enabled=fallback,
            anthropic_api_key=anthropic_key,
            anthropic_model=anthropic_model,
        )
    return ProviderConfig(
        kind="local",
        base_url=_text(values, env.LOCAL_LLM_BASE_URL, env.DEFAULT_LOCAL_BASE_URL),
        model=_text(values, env.LOCAL_LLM_MODEL),
        api_key="",
        fallback_enabled=fallback,
        anthropic_api_key=anthropic_key,
        anthropic_model=anthropic_model,
    )


def provider_config_from_env(environ: Mapping[str, str] | None = None) -> ProviderConfig:
    """Convenience wrapper that resolves directly from `os.environ`."""
    return resolve_provider_config(environ if environ is not None else os.environ)
