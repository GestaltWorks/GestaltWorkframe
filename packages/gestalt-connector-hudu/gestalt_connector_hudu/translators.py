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

from gestalt_connector_protocol import BodyStructured, Document, HeadingSection, ListSection, ParagraphSection, Privacy, SourceMetadata, TableSection, Timestamps


SECURE_KEYS = {"password", "secure", "secret", "api_key", "token"}


def translate_hudu_resource(connector_id: str, company: dict[str, Any], resource_type: str, resource: dict[str, Any]) -> Document:
    title = _title(resource_type, resource)
    external_id = str(resource.get("id") or title)
    company_name = str(company.get("name") or company.get("company_name") or company.get("id") or "")
    clean = _strip_secure(resource)
    sections = _sections(resource_type, title, clean)
    privacy = Privacy(cloud_llm_eligible=not _had_secure(resource), sensitive_classes_present=["password"] if _had_secure(resource) else [])
    return Document(
        doc_id=_doc_id(connector_id, resource_type, external_id),
        source=SourceMetadata(
            connector_id=connector_id,
            connector_name="Hudu",
            source_system="hudu",
            source_type=resource_type,
            source_url=str(resource.get("url") or resource.get("public_url") or ""),
            external_id=external_id,
            parent_external_id=str(company.get("id", "")),
            title=title,
            organization=company_name,
            labels={"resource_type": resource_type, "company": company_name},
        ),
        body_text=_body(title, sections),
        body_structured=BodyStructured(sections=sections),
        privacy=privacy,
        timestamps=Timestamps(source_created_at=_dt(resource.get("created_at")), source_updated_at=_dt(resource.get("updated_at"))),
        tags=["hudu", resource_type, company_name],
    )


def _sections(resource_type: str, title: str, resource: dict[str, Any]) -> list[Any]:
    if resource_type in {"articles", "knowledge_base_articles"}:
        return [HeadingSection(text=title, level=1), ParagraphSection(text=_html_to_text(str(resource.get("body") or resource.get("content") or "")))]
    if resource_type == "processes":
        steps = resource.get("steps")
        if isinstance(steps, list):
            return [HeadingSection(text=title, level=1), ListSection(ordered=True, items=[_html_to_text(str(step.get("name") or step.get("description") or step)) if isinstance(step, dict) else _html_to_text(str(step)) for step in steps])]
    rows = [[_label(key), _value(value)] for key, value in sorted(resource.items()) if _display(key, value)]
    return [HeadingSection(text=title, level=1), TableSection(headers=["Field", "Value"], rows=rows)]


def _strip_secure(resource: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in resource.items() if not _secure_key(key)}


def _had_secure(resource: dict[str, Any]) -> bool:
    return any(_secure_key(key) for key in resource)


def _secure_key(key: str) -> bool:
    lowered = key.lower()
    return any(item in lowered for item in SECURE_KEYS)


def _display(key: str, value: Any) -> bool:
    return value not in (None, "", [], {}) and not _secure_key(key)


def _value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_value(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{_label(str(key))}={_value(item)}" for key, item in value.items() if _display(str(key), item))
    return _html_to_text(str(value))


def _body(title: str, sections: list[Any]) -> str:
    parts = [title]
    for section in sections:
        if isinstance(section, ParagraphSection):
            parts.append(section.text)
        elif isinstance(section, ListSection):
            parts.extend(section.items)
        elif isinstance(section, TableSection):
            parts.extend(f"{row[0]}: {row[1]}" for row in section.rows)
    return "\n".join(part for part in parts if part).strip() or title


def _title(resource_type: str, resource: dict[str, Any]) -> str:
    return str(resource.get("name") or resource.get("title") or resource.get("subject") or f"{resource_type}:{resource.get('id', 'unknown')}")


def _label(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()


def _html_to_text(value: str) -> str:
    no_scripts = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    no_tags = re.sub(r"(?s)<[^>]+>", " ", no_scripts)
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _doc_id(connector_id: str, resource_type: str, external_id: str) -> str:
    return hashlib.sha256(f"{connector_id}:{resource_type}:{external_id}".encode("utf-8")).hexdigest()
