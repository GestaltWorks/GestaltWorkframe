# Framework vs. reference implementation (source-of-truth policy)

**GestaltWorkframe is the framework. EGI_bot is its reference implementation.**

## The rule

Framework-level code -- the shared engine: the LLM router
(`core/router.py`), providers, orchestrator, runtime, retrieval, and policy --
is owned by GestaltWorkframe. A framework fix or feature MUST originate here,
go through this repo's PR/review/CI, and then be ported *into* EGI_bot.

Never author a framework change directly in EGI_bot. EGI_bot may only
originate implementation-only changes (its deployment config, content, brand,
`deployments/`, app wiring). When in doubt about who owns a file, treat it as
framework code and make the change here first.

This keeps every deployment that builds on GestaltWorkframe able to inherit
engine fixes from one source instead of each implementation drifting its own
copy.

## Porting direction

```
GestaltWorkframe (origin/source of truth)  ->  EGI_bot (reference impl) -> other impls
```

Land in GestaltWorkframe, then port the merged change downstream. Do not push
engine changes upstream from an implementation as the normal path.

## Reconciliation log

### Local-route health gate / fast cloud failover (router)

A health gate (`LLMRouter._local_route_callable`) was added so a down local
model is skipped in <1s and the turn escalates to cloud immediately, instead
of blocking the local provider's full chat timeout (~30s, twice per turn in
the tool-loop path) and degrading to the directional fallback message.

- **Provenance exception:** this fix was first hotfixed in EGI_bot
  (EGI_bot PR #42) under production pressure, which is the wrong direction.
  It has been forward-ported here so GestaltWorkframe is the canonical source;
  EGI_bot's copy is to be reconciled against this one.
- **Known drift:** `core/router.py` had diverged between the two repos at the
  time of this port. Only the health-gate change was reconciled here; the
  remaining drift should be reconciled in a dedicated follow-up so the two
  routers converge.
