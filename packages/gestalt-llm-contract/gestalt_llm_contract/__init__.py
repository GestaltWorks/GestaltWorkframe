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

"""Gestalt LLM provider contract: env var names, resolution rule, and Protocol.

Shared by the GestaltWorkframe platform and the GestaltWorkframeEDU middleware
so the LLM provider env contract has a single source of truth and cannot drift.
"""

from gestalt_llm_contract import env
from gestalt_llm_contract.config import (
    ProviderConfig,
    ProviderKind,
    provider_config_from_env,
    resolve_provider_config,
)
from gestalt_llm_contract.protocol import LLMProvider

__all__ = [
    "env",
    "ProviderConfig",
    "ProviderKind",
    "provider_config_from_env",
    "resolve_provider_config",
    "LLMProvider",
]
