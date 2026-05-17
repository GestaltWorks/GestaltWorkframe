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

import hashlib
import html
import re
from datetime import datetime
from typing import Any

from gestalt_connector_protocol import BodyStructured, Document, HeadingSection, ParagraphSection, Privacy, RedactionEvent, SourceMetadata, TableSection, Timestamps


PASSWORD_KEYS = {"password", "password-value", "otp-secret", "secret", "secure-field", "secure"}


def translate_itglue_asset(connector_id: str, organization: dict[str, Any], asset_type: str, asset: dict[str, Any]) -> Document:
    attributes = _attributes(asset)
    org_attrs = _attributes(organization)
    title = _title(asset_type, attributes, asset)
    external_id = str(asset.get("id") or attributes.get("id") or title)
    org_name = str(org_attrs.get("name") or organization.get("id") or "")
    clean_attributes = _strip_secure(attributes) if asset_type == "passwords" else attributes
    sections = _sections(asset_type, title, clean_attributes)
    body_text = _body_text(title, sections)
    privacy = Privacy()
    if asset_type == "passwords":
        privacy = Privacy(
            cloud_llm_eligible=False,
            sensitive_classes_present=["password"],
            redactions_applied=[RedactionEvent(detector_id="itglue-password-strip-v1", sensitive_class="password", snippet_hash="stripped", position=0)],
        )
    return Document(
        doc_id=_doc_id(connector_id, asset_type, external_id),
        source=SourceMetadata(
            connector_id=connector_id,
            connector_name="ITGlue",
            source_system="itglue",
            source_type=asset_type,
            source_url=str(attributes.get("resource-url") or attributes.get("url") or ""),
            external_id=external_id,
            parent_external_id=str(organization.get("id", "")),
            title=title,
            organization=org_name,
            labels={"asset_type": asset_type, "organization": org_name, "template": _template_name(attributes)},
        ),
        body_text=body_text,
        body_structured=BodyStructured(sections=sections),
        privacy=privacy,
        timestamps=Timestamps(source_created_at=_dt(attributes.get("created-at")), source_updated_at=_dt(attributes.get("updated-at"))),
        tags=["itglue", asset_type, org_name, _template_name(attributes)],
    )


def _sections(asset_type: str, title: str, attributes: dict[str, Any]) -> list[Any]:
    if asset_type == "documents":
        text = _html_to_text(str(attributes.get("body") or attributes.get("content") or attributes.get("description") or ""))
        return [HeadingSection(text=title, level=1), ParagraphSection(text=text)]
    rows = [[_label(key), _value(value)] for key, value in sorted(attributes.items()) if _display_value(key, value)]
    heading = HeadingSection(text=title, level=1)
    table = TableSection(headers=["Field", "Value"], rows=rows)
    if asset_type == "flexible-assets":
        template = _template_name(attributes)
        return [heading, ParagraphSection(text=f"Template: {template}"), table]
    return [heading, table]


def _body_text(title: str, sections: list[Any]) -> str:
    parts = [title]
    for section in sections:
        if isinstance(section, ParagraphSection) and section.text:
            parts.append(section.text)
        if isinstance(section, TableSection):
            parts.extend(f"{row[0]}: {row[1]}" for row in section.rows)
    return "\n".join(part for part in parts if part).strip() or title


def _attributes(asset: dict[str, Any]) -> dict[str, Any]:
    attrs = asset.get("attributes")
    return dict(attrs) if isinstance(attrs, dict) else {}


def _strip_secure(attributes: dict[str, Any]) -> dict[str, Any]:
    return {key: ("[STRIPPED]" if _secure_key(key) else value) for key, value in attributes.items() if not _secure_key(key) or key in {"name", "username", "url", "notes"}}


def _secure_key(key: str) -> bool:
    lowered = key.lower()
    return any(item in lowered for item in PASSWORD_KEYS)


def _display_value(key: str, value: Any) -> bool:
    return value not in (None, "", [], {}) and not _secure_key(key)


def _value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_value(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{_label(key)}={_value(item)}" for key, item in value.items() if _display_value(str(key), item))
    return _html_to_text(str(value))


def _title(asset_type: str, attributes: dict[str, Any], asset: dict[str, Any]) -> str:
    return str(attributes.get("name") or attributes.get("title") or attributes.get("hostname") or attributes.get("fqdn") or f"{asset_type}:{asset.get('id', 'unknown')}")


def _template_name(attributes: dict[str, Any]) -> str:
    value = attributes.get("flexible-asset-type-name") or attributes.get("asset-type-name") or attributes.get("template-name") or ""
    return str(value)


def _label(value: str) -> str:
    return value.replace("-", " ").replace("_", " ").strip().title()


def _html_to_text(value: str) -> str:
    no_scripts = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    no_tags = re.sub(r"(?s)<[^>]+>", " ", no_scripts)
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _doc_id(connector_id: str, asset_type: str, external_id: str) -> str:
    return hashlib.sha256(f"{connector_id}:{asset_type}:{external_id}".encode("utf-8")).hexdigest()
