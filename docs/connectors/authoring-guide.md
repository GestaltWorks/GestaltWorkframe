# Connector Authoring Guide

Connectors translate source systems into canonical `Document` records. Each
connector implements the protocol package, declares a config schema, validates
health, emits redacted documents, and passes `connector-test validate` before it
is wired into a deployment.

## Minimal connector shape

```python
class MyConnector:
    connector_id = "my-connector"
    capabilities = ConnectorCapabilities(supported_resource_types=("article",))

    @classmethod
    def config_schema(cls) -> dict[str, object]: ...
    async def health_check(self, config: ConnectorConfig) -> ConnectorHealth: ...
    async def discover_documents(self, config: ConnectorConfig): ...
```

## Required behavior

- Never log credentials or source secrets.
- Run the redaction pipeline before yielding a document.
- Set `source.connector_id` to the connector under test.
- Keep `body_text` non-empty.
- Populate `privacy.redactions_applied` with audit events, not sensitive text.
- Use stable `doc_id` values based on source identity, not crawl order.
- Preserve provenance in `source.source_url`, `source.path`, `tags`, and
  `policy` fields when the source allows it.

## Validation

Run the harness before opening a PR:

```bash
connector-test validate package.module:ConnectorClass connector-config.yaml
```

The harness calls `health_check`, iterates `discover_documents`, validates each
document against the canonical schema, and rejects empty bodies or connector ID
mismatches.
