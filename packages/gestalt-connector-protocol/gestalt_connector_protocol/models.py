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

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from gestalt_connector_protocol.sections import StructuredSection


SCHEMA_VERSION = "1.0"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceMetadata(_StrictModel):
    connector_id: str = Field(min_length=1)
    connector_name: str = ""
    source_system: str = ""
    source_type: str = Field(min_length=1)
    source_url: str = ""
    external_id: str = Field(min_length=1)
    parent_external_id: str = ""
    title: str = ""
    path: list[str] = Field(default_factory=list)
    organization: str = ""
    labels: dict[str, str] = Field(default_factory=dict)


class BodyStructured(_StrictModel):
    sections: list[StructuredSection] = Field(default_factory=list)


class Attachment(_StrictModel):
    attachment_id: str = ""
    filename: str
    media_type: str = "application/octet-stream"
    size_bytes: int | None = Field(default=None, ge=0)
    source_url: str = ""
    content_hash: str = ""
    text_extract: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class ACL(_StrictModel):
    visibility: Literal["public", "internal", "restricted", "private"] = "internal"
    allowed_principals: list[str] = Field(default_factory=list)
    denied_principals: list[str] = Field(default_factory=list)
    source_acl_hash: str = ""


class RedactionEvent(_StrictModel):
    detector_id: str
    sensitive_class: str
    snippet_hash: str
    position: int = Field(ge=0)
    count: int = Field(default=1, ge=1)


class Privacy(_StrictModel):
    cloud_llm_eligible: bool = True
    contains_pii: bool = False
    sensitive_classes_present: list[str] = Field(default_factory=list)
    redactions_applied: list[RedactionEvent] = Field(default_factory=list)


class Timestamps(_StrictModel):
    source_created_at: datetime | None = None
    source_updated_at: datetime | None = None
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime | None = None


class DocumentPolicy(_StrictModel):
    display_eligible: bool = False
    retrieval_eligible: bool = True
    curriculum_eligible: bool = False
    retention_class: str = "standard"
    license: str = ""
    attribution: str = ""
    provenance: str = ""


class Diagnostics(_StrictModel):
    connector_version: str = ""
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    checksum: str = ""
    source_etag: str = ""
    trace_id: str = ""
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class Document(_StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    doc_id: str = Field(min_length=1)
    source: SourceMetadata
    body_text: str = Field(min_length=1)
    body_structured: BodyStructured = Field(default_factory=BodyStructured)
    attachments: list[Attachment] = Field(default_factory=list)
    acl: ACL = Field(default_factory=ACL)
    privacy: Privacy = Field(default_factory=Privacy)
    timestamps: Timestamps = Field(default_factory=Timestamps)
    tags: list[str] = Field(default_factory=list)
    policy: DocumentPolicy = Field(default_factory=DocumentPolicy)
    diagnostics: Diagnostics = Field(default_factory=Diagnostics)
