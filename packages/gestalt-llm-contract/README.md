# gestalt-llm-contract

The shared LLM provider contract for the Gestalt family. It is intentionally
tiny and dependency-free (standard library only): it carries no HTTP transport,
no vendor SDK, and no application runtime, so any layer can depend on it without
inheriting the platform's weight.

It owns the three things the [GestaltWorkframe](https://github.com/GestaltWorks/GestaltWorkframe)
platform and the GestaltWorkframeEDU middleware must agree on:

1. **Env var names** (`gestalt_llm_contract.env`) — the canonical names and
   defaults for the OpenRouter / local / Anthropic configuration.
2. **Resolution rule** (`resolve_provider_config`) — the precedence
   (OpenRouter > local, with an optional Anthropic fallback) as one function,
   so the two layers cannot drift.
3. **Portable interface** (`LLMProvider`) — the single-shot
   `chat(system, user) -> str` Protocol that travels between layers. The
   platform's richer multi-turn, tool-calling provider is a superset kept in the
   platform.

## Usage

```python
from gestalt_llm_contract import resolve_provider_config, env

cfg = resolve_provider_config({
    env.OPENROUTER_API_KEY: "sk-or-...",
})
# cfg.kind == "openrouter"; build your own transport from cfg.
```

Each consumer constructs its own concrete provider objects from the resolved
`ProviderConfig`. This package never makes a network call.
