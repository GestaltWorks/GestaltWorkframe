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

"""Canonical environment-variable names for the Gestalt LLM provider contract.

Both the GestaltWorkframe platform and the GestaltWorkframeEDU middleware
resolve their LLM provider from the same set of environment variables, in the
same precedence (OpenRouter, then a local OpenAI-compatible endpoint, with an
optional Anthropic fallback). These constants are the single source of truth so
the two layers cannot drift apart on the names or defaults.
"""

from __future__ import annotations

# --- variable names ---------------------------------------------------------
OPENROUTER_API_KEY = "OPENROUTER_API_KEY"
OPENROUTER_BASE_URL = "OPENROUTER_BASE_URL"
OPENROUTER_MODEL = "OPENROUTER_MODEL"

ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
ANTHROPIC_MODEL = "ANTHROPIC_MODEL"
# Documented alias for ANTHROPIC_MODEL. The platform's provider registry read
# `CLAUDE_MODEL` while this contract declared `ANTHROPIC_MODEL`, so an operator
# who set one of them silently had no effect on the other layer. Both names are
# now read, `ANTHROPIC_MODEL` wins when both are set, and the alias is listed
# here so neither layer can drift away from it again.
CLAUDE_MODEL_ALIAS = "CLAUDE_MODEL"
# Optional Anthropic-compatible gateway (e.g. a LiteLLM key broker); unset
# keeps the SDK's default api.anthropic.com host.
ANTHROPIC_BASE_URL = "ANTHROPIC_BASE_URL"

LOCAL_LLM_BASE_URL = "LOCAL_LLM_BASE_URL"
LOCAL_LLM_MODEL = "LOCAL_LLM_MODEL"

ENABLE_CLAUDE_FALLBACK = "ENABLE_CLAUDE_FALLBACK"

# --- defaults ---------------------------------------------------------------
# Endpoints have defaults. MODELS DO NOT, and this package ships none.
#
# A model id written into a shared package is the worst version of a stale
# constant: it propagates to every consumer at once and can only be corrected by
# releasing the package. Both of the previous defaults proved the point.
#
#   DEFAULT_OPENROUTER_MODEL was "openrouter/auto", a router pseudo-model.
#   docs/standards/model-routing-policy.md excludes those outright: they are not
#   selectable models, they delegate the routing decision away from the lane,
#   and they publish sentinel prices (0 and -1 per token) that win any cost
#   ranking. Every consumer that did not set OPENROUTER_MODEL was handing model
#   choice to the aggregator while a lane resolver sat unused in the same repo.
#
#   DEFAULT_ANTHROPIC_MODEL was "claude-3-5-sonnet-latest". Claude 3.5 Sonnet
#   was retired 2025-10-28 and both dated snapshots are gone, so the `-latest`
#   alias resolves to nothing and every request on that default returns HTTP 404
#   not_found_error. It was not merely stale, it was broken, in a package that
#   propagates to every consumer simultaneously.
#
# The replacement is no default at all. On the OpenRouter path the model is
# resolved at runtime from the live catalog by the lane system
# (gestaltworkframe/core/model_resolver.py). On the Anthropic fallback path the
# operator names the model explicitly via ANTHROPIC_MODEL (or its CLAUDE_MODEL
# alias); when nothing is named the fallback reports itself as not enabled
# rather than dispatching to a guess.
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_LOCAL_BASE_URL = "http://localhost:8080/v1"

# Strings treated as truthy for boolean env vars.
TRUTHY = frozenset({"1", "true", "yes", "on"})
