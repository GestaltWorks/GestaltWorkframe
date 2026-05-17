# LibraryConfig productization (design note)

Status: reference design. This document captures the architectural
direction for making the corpus/discovery/library pattern reusable
across subjects. The framework ships with one sample library wired up
end to end; this note describes how additional libraries plug in.

## Why this exists

The discovery + KB + publisher pipeline as built today is centered on
a single sample library:

- `kb/watchlist_seed.py` enumerates the sample's watch targets.
- `core/discovery_summary.py` has subject vocabulary baked into the
  topic taxonomy.
- `kb/library_publisher.py` publishes into a single configured corpus
  repo via env vars rather than by library identity.
- `web/src/app/library/page.tsx` renders a single library; there is no
  routing for additional libraries.

The objectives document says the education platform should be
curriculum-agnostic. A subject corpus should be loadable, then drive
lessons, library pages, retrieval grounding, and ingestion. The
current shape can't do that without a library-scoping abstraction.

## Proposed abstraction

A `LibraryConfig` is the aggregate that ties together everything a
single subject needs:

```
LibraryConfig
  id: str                       # stable slug
  display_name: str
  public_route: str             # /library/<slug>, etc.
  github_repo: str | None       # publisher target, optional
  watchlist_seed_module: str    # python dotted path
  topic_taxonomy: TopicTaxonomy # for discovery_summary
  ingestion_policy: IngestionPolicy
  retrieval_policy: RetrievalPolicy
  curriculum_policy: CurriculumPolicy
```

`TopicTaxonomy` replaces the hardcoded keyword lists in
discovery_summary._topic with a per-library data table. Topic
inference is the same algorithm; only the table varies.

`IngestionPolicy` replaces the hardcoded `INGEST_THRESHOLD = 60` and
related constants. Each library declares its own thresholds.

`RetrievalPolicy` declares which Chroma collection to retrieve from
and which approved-discovery context window to use. Today both are
implicit globals.

`CurriculumPolicy` is the seed for the education platform. It carries
the lesson generation prompt template, default course length, default
ability level options, and the assessment style. Not used today.

## Implementation phases

1. **Define the aggregate** (`core/library_config.py`): the dataclass
   above plus a `LibraryRegistry` that loads configs from a directory
   of TOML files. The current sample library becomes the first entry.

2. **Thread library_id through discovery**: each `WatchedSource` and
   `DiscoveryFind` gets a `library_id` column (additive migration).
   The scheduler scopes queries by library. The admin discovery UI
   gains a library selector.

3. **Thread library_id through KB**: `KnowledgeRetriever.retrieve` takes
   a `library_id` and uses the per-library Chroma collection name from
   the RetrievalPolicy.

4. **Per-library public route**: `/library/{slug}` replaces any
   hardcoded single-library route. The repository URL and category
   shape move into a server-loaded library config.

5. **Curriculum hooks**: education-platform endpoints accept a
   library_id and use that library's CurriculumPolicy.

6. **Customer-hosted variant**: a customer instance loads only the
   LibraryConfigs it has licensed; private libraries stay private.

## Why this stays separate

- Only the sample library is wired today. The abstraction has no second
  consumer to validate the shape.
- Done badly, the abstraction adds permanent indirection without any
  product benefit. Better to wait for a second library demand to pull
  the design through use cases.

## Activation checklist

When a second library is added:

- [ ] Decide whether the second library is admin-only or public. If
      admin-only, keep public routing unchanged.
- [ ] Draft the TOML schema and check it into `docs/library-configs/`.
- [ ] Migrate one piece of hardcoded subject vocabulary into the
      taxonomy schema first; that exercise will reveal whether the
      taxonomy table shape is right.
- [ ] Run the discovery scheduler against the second library before
      touching the public site. Discovery is the most failure-mode-
      sensitive subsystem; get it right in isolation.
- [ ] Only after the discovery + KB layer is library-scoped should
      the public page routing change.
