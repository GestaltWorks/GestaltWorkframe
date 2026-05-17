from datetime import datetime, timezone

from gestalt_connector_protocol import Document, Privacy, SourceMetadata

from api.privacy_audit import privacy_audit_payload
from core.db import DiscoveryFind


def _find(find_id: str, *, cloud_eligible: bool) -> DiscoveryFind:
    now = datetime.now(timezone.utc)
    doc = Document(
        doc_id=find_id,
        source=SourceMetadata(connector_id="fixture", source_type="file", external_id=find_id),
        body_text="body",
        privacy=Privacy(cloud_llm_eligible=cloud_eligible),
    )
    return DiscoveryFind(
        id=find_id,
        discovery_source_id="source-1",
        finding_type="file",
        external_id=find_id,
        title=find_id,
        url="https://example.com/" + find_id,
        canonical_document_json=doc.model_dump_json(),
        created_at=now,
    )


def test_privacy_audit_counts_cloud_eligible_and_local_only_documents():
    payload = privacy_audit_payload([_find("a", cloud_eligible=True), _find("b", cloud_eligible=False)])
    assert payload["per_connector"]["fixture"] == {"cloud_eligible": 1, "local_only": 1, "total": 2}
    assert payload["rolling_7_day_cloud_refused_count"] == 1
    assert payload["rolling_7_day_count_may_be_underreported"] is False
    assert payload["max_rows_scanned"] >= 1
    assert payload["rows_scanned"] == 2
