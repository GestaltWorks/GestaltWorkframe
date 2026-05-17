# Copyright 2026 Gestalt Works
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import PurePosixPath
from collections.abc import Mapping
from typing import Any

from gestalt_connector_protocol import BodyStructured, Document, HeadingSection, ParagraphSection, SourceMetadata, Timestamps


def translate_s3_object(connector_id: str, bucket: str, metadata: dict[str, Any], body_text: str, headers: Any | None = None) -> Document:
    headers = dict(headers) if isinstance(headers, Mapping) else {}
    key = str(metadata.get("Key") or metadata.get("key") or "object")
    title = PurePosixPath(key).name or key
    return Document(
        doc_id=_doc_id(connector_id, bucket, key),
        source=SourceMetadata(
            connector_id=connector_id,
            connector_name="S3 Object Storage",
            source_system="s3",
            source_type="s3_object",
            source_url=f"s3://{bucket}/{key}",
            external_id=f"{bucket}/{key}",
            parent_external_id=bucket,
            title=title,
            path=[part for part in PurePosixPath(key).parts if part not in {"/", ""}],
            labels={
                "bucket": bucket,
                "etag": _clean_etag(metadata.get("ETag") or metadata.get("etag") or ""),
                "size": str(metadata.get("Size") or metadata.get("size") or headers.get("Content-Length") or ""),
                "content_type": str(headers.get("Content-Type") or headers.get("content-type") or ""),
                "storage_class": str(metadata.get("StorageClass") or metadata.get("storage_class") or ""),
            },
        ),
        body_text="\n".join(part for part in [title, body_text] if part).strip(),
        body_structured=BodyStructured(sections=[HeadingSection(text=title, level=1), ParagraphSection(text=body_text.strip())]),
        timestamps=Timestamps(source_updated_at=_dt(metadata.get("LastModified") or metadata.get("last_modified"))),
        tags=["s3", bucket, *[part for part in PurePosixPath(key).parts[:-1] if part not in {"/", ""}]],
    )


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _clean_etag(value: Any) -> str:
    return str(value).strip('"')


def _doc_id(connector_id: str, bucket: str, key: str) -> str:
    return hashlib.sha256(f"{connector_id}:s3:{bucket}:{key}".encode("utf-8")).hexdigest()
