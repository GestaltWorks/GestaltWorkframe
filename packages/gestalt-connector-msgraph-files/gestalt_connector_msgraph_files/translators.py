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
from typing import Any

from gestalt_connector_protocol import BodyStructured, Document, HeadingSection, ParagraphSection, SourceMetadata, TableSection, Timestamps


def translate_drive_item(connector_id: str, drive: dict[str, Any], item: dict[str, Any]) -> Document:
    title = str(item.get("name") or item.get("id") or "drive-item")
    drive_id = str(drive.get("id", ""))
    path = _path(item)
    text = "\n".join(part for part in [title, path, str(item.get("webUrl") or "")] if part)
    return Document(
        doc_id=_doc_id(connector_id, "drive_item", str(item.get("id") or title)),
        source=SourceMetadata(
            connector_id=connector_id,
            connector_name="Microsoft Graph Files",
            source_system="msgraph",
            source_type="drive_item",
            source_url=str(item.get("webUrl") or ""),
            external_id=str(item.get("id") or title),
            parent_external_id=drive_id,
            title=title,
            path=[part for part in path.split("/") if part],
            labels={"drive_id": drive_id, "mime_type": str(item.get("file", {}).get("mimeType", "")) if isinstance(item.get("file"), dict) else ""},
        ),
        body_text=text,
        body_structured=BodyStructured(sections=[HeadingSection(text=title, level=1), ParagraphSection(text=path)]),
        timestamps=Timestamps(source_created_at=_dt(item.get("createdDateTime")), source_updated_at=_dt(item.get("lastModifiedDateTime"))),
        tags=["msgraph", "drive_item", drive_id, *[part for part in path.split("/") if part][:-1]],
    )


def translate_list_item(connector_id: str, site_id: str, item: dict[str, Any]) -> Document:
    fields = item.get("fields") if isinstance(item.get("fields"), dict) else item
    title = str(fields.get("Title") or fields.get("title") or item.get("id") or "list-item")
    rows = [[str(key), str(value)] for key, value in sorted(fields.items()) if value not in (None, "", [], {})]
    return Document(
        doc_id=_doc_id(connector_id, "list_item", f"{site_id}:{item.get('id', title)}"),
        source=SourceMetadata(
            connector_id=connector_id,
            connector_name="Microsoft Graph Files",
            source_system="msgraph",
            source_type="list_item",
            source_url=str(item.get("webUrl") or ""),
            external_id=str(item.get("id") or title),
            parent_external_id=site_id,
            title=title,
            labels={"site_id": site_id},
        ),
        body_text="\n".join([title, *[f"{row[0]}: {row[1]}" for row in rows]]),
        body_structured=BodyStructured(sections=[HeadingSection(text=title, level=1), TableSection(headers=["Field", "Value"], rows=rows)]),
        timestamps=Timestamps(source_created_at=_dt(item.get("createdDateTime")), source_updated_at=_dt(item.get("lastModifiedDateTime"))),
        tags=["msgraph", "list_item", site_id],
    )


def _path(item: dict[str, Any]) -> str:
    parent = item.get("parentReference")
    parent_path = str(parent.get("path", "")) if isinstance(parent, dict) else ""
    return f"{parent_path}/{item.get('name', '')}".replace("/drive/root:", "").strip("/")


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _doc_id(connector_id: str, resource_type: str, external_id: str) -> str:
    return hashlib.sha256(f"{connector_id}:{resource_type}:{external_id}".encode("utf-8")).hexdigest()
