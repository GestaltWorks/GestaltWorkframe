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
# Optional Anthropic-compatible gateway (e.g. a LiteLLM key broker); unset
# keeps the SDK's default api.anthropic.com host.
ANTHROPIC_BASE_URL = "ANTHROPIC_BASE_URL"

LOCAL_LLM_BASE_URL = "LOCAL_LLM_BASE_URL"
LOCAL_LLM_MODEL = "LOCAL_LLM_MODEL"

ENABLE_CLAUDE_FALLBACK = "ENABLE_CLAUDE_FALLBACK"

# --- defaults ---------------------------------------------------------------
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "openrouter/auto"
DEFAULT_LOCAL_BASE_URL = "http://localhost:8080/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-3-5-sonnet-latest"

# Strings treated as truthy for boolean env vars.
TRUTHY = frozenset({"1", "true", "yes", "on"})
