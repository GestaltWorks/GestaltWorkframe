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

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class _SectionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str = ""
    text: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class HeadingSection(_SectionBase):
    section_type: Literal["heading"] = "heading"
    level: int = Field(default=1, ge=1, le=6)


class ParagraphSection(_SectionBase):
    section_type: Literal["paragraph"] = "paragraph"


class ListSection(_SectionBase):
    section_type: Literal["list"] = "list"
    ordered: bool = False
    items: list[str] = Field(default_factory=list)


class TableSection(_SectionBase):
    section_type: Literal["table"] = "table"
    caption: str = ""
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


class CodeSection(_SectionBase):
    section_type: Literal["code"] = "code"
    language: str = ""
    code: str = ""


class CalloutSection(_SectionBase):
    section_type: Literal["callout"] = "callout"
    title: str = ""
    tone: Literal["note", "info", "warning", "danger"] = "note"


class LinkSection(_SectionBase):
    section_type: Literal["link"] = "link"
    label: str = ""
    url: str


StructuredSection = Annotated[
    Union[
        HeadingSection,
        ParagraphSection,
        ListSection,
        TableSection,
        CodeSection,
        CalloutSection,
        LinkSection,
    ],
    Field(discriminator="section_type"),
]
