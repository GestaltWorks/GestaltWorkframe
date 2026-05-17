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

import json
from pathlib import Path

import jsonschema

from gestalt_connector_protocol import (
    CalloutSection,
    CodeSection,
    Document,
    HeadingSection,
    LinkSection,
    ListSection,
    ParagraphSection,
    SourceMetadata,
    TableSection,
)


def _generated_schema() -> dict[str, object]:
    schema = Document.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://example.com/schemas/document.v1.json"
    return json.loads(json.dumps(schema, sort_keys=True))


def test_all_section_types_round_trip_through_document() -> None:
    document = Document(
        doc_id="doc-1",
        source=SourceMetadata(
            connector_id="test",
            source_type="article",
            external_id="doc-1",
        ),
        body_text="Heading\nParagraph\none\ntwo\nField Value\nprint('ok')\nNote\nLink",
        body_structured={
            "sections": [
                HeadingSection(text="Heading", level=1),
                ParagraphSection(text="Paragraph"),
                ListSection(items=["one", "two"]),
                TableSection(headers=["Field", "Value"], rows=[["A", "B"]]),
                CodeSection(language="python", code="print('ok')"),
                CalloutSection(title="Note", text="Remember this"),
                LinkSection(label="Docs", url="https://example.com"),
            ]
        },
    )
    dumped = document.model_dump(mode="json")
    loaded = Document.model_validate(dumped)
    assert [section.section_type for section in loaded.body_structured.sections] == [
        "heading",
        "paragraph",
        "list",
        "table",
        "code",
        "callout",
        "link",
    ]


def test_fixture_validates_against_document_schema() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "sample_document.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    schema_path = Path(__file__).resolve().parents[3] / "docs" / "schemas" / "document.v1.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(fixture, schema)
    assert Document.model_validate(fixture).doc_id == "fixture-doc-1"


def test_checked_in_schema_matches_document_model() -> None:
    schema_path = Path(__file__).resolve().parents[3] / "docs" / "schemas" / "document.v1.json"
    checked_in = json.loads(schema_path.read_text(encoding="utf-8"))
    assert checked_in == _generated_schema()
