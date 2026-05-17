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

from pathlib import Path

import pytest

from gestalt_connector_protocol import Document, RedactionPipeline, RedactionWhitelist, SourceMetadata


def _doc(text: str) -> Document:
    return Document(
        doc_id="redaction-doc",
        source=SourceMetadata(connector_id="test", source_type="fixture", external_id="redaction-doc"),
        body_text=text,
    )


def _api_cases() -> list[tuple[str, str]]:
    return [
        ("api_key", "aws_key=" + "AKIA" + "A1B2C3D4E5F6G7H8"),
        ("api_key", "github=" + "ghp_" + "A" * 36),
        ("api_key", "github_pat=" + "github_pat_" + "B" * 24),
        ("api_key", "stripe=" + "sk_live_" + "C" * 24),
        ("api_key", "anthropic=" + "sk-ant-" + "D" * 24),
        ("api_key", "openai=" + "sk-" + "E" * 24),
        ("api_key", "google=" + "AIza" + "F" * 35),
        ("api_key", "azure_client_secret=" + "G" * 24),
        ("jwt", "bearer " + "eyJ" + "A" * 12 + "." + "B" * 12 + "." + "C" * 12),
        ("private_key", "\n".join(["-----BEGIN FAKE PRIVATE KEY-----", "H" * 32, "-----END FAKE PRIVATE KEY-----"])),
    ]


def _fixture_cases() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = [
        ("password", "password=" + "CorrectHorse1"),
        ("password", "passwd: " + "RouterSecret2"),
        ("password", "pwd=" + "ShortButValid3"),
        ("password", "client_secret=" + "clientSecretValue4"),
        ("password", "POSTGRES_PASSWORD=" + "Aa1!" * 8),
        ("ssn", "ssn " + "123" + "-45-6789"),
        ("email", "contact " + "person" + "@" + "example.test"),
        ("phone", "call " + "212" + "-555-0199"),
        ("credit_card", "card " + "4111" + "1111" * 3),
        ("bank_account", "account number " + "123456789"),
        ("internal_network", "host 10.1.2.3"),
        ("internal_network", "host 192.168.1.20"),
        ("internal_network", "host 172.16.0.5"),
    ]
    cases.extend(_api_cases())
    for index in range(30):
        cases.append(("password", f"PASSWORD_{index}=" + ("Ab9$" * 6) + str(index)))
    assert len(cases) >= 50
    return cases


@pytest.mark.parametrize(("sensitive_class", "text"), _fixture_cases())
def test_default_pipeline_redacts_sensitive_fixtures(sensitive_class: str, text: str) -> None:
    redacted, events = RedactionPipeline.default().run(text)
    assert text != redacted
    assert "[REDACTED:" in redacted
    assert sensitive_class in {event.sensitive_class for event in events}
    assert all(event.snippet_hash not in text for event in events)


def test_luhn_validation_prevents_credit_card_false_positive() -> None:
    text = "tracking number " + "4111" * 3 + "1112"
    redacted, events = RedactionPipeline.default().run(text)
    assert redacted == text
    assert not [event for event in events if event.sensitive_class == "credit_card"]


def test_whitelisted_internal_network_is_not_redacted_but_blocks_cloud() -> None:
    pipeline = RedactionPipeline.default(RedactionWhitelist(allow_sensitive_classes=frozenset({"internal_network"})))
    text = "internal host 10.0.0.8"
    redacted, events = pipeline.run(text)
    document = pipeline.apply_to_document(_doc(text))
    assert redacted == text
    assert events == []
    assert document.body_text == text
    assert document.privacy.cloud_llm_eligible is False
    assert document.privacy.sensitive_classes_present == ["internal_network"]


def test_never_allow_class_is_redacted_even_when_whitelisted() -> None:
    pipeline = RedactionPipeline.default(RedactionWhitelist(allow_sensitive_classes=frozenset({"api_key"})))
    text = "token=" + "sk-" + "Z" * 24
    redacted, events = pipeline.run(text)
    assert "[REDACTED:api_key]" in redacted
    assert events[0].sensitive_class == "api_key"


def test_yaml_whitelist_loader(tmp_path: Path) -> None:
    config = tmp_path / "redaction_whitelist.yaml"
    config.write_text("allow_sensitive_classes:\n  - internal_network\ninternal_hostname_patterns:\n  - internal-[a-z]+\\.lan\n", encoding="utf-8")
    whitelist = RedactionWhitelist.from_yaml_file(config)
    pipeline = RedactionPipeline.default(whitelist)
    document = pipeline.apply_to_document(_doc("server internal-router.lan"))
    assert document.body_text == "server internal-router.lan"
    assert document.privacy.cloud_llm_eligible is False
    assert document.privacy.sensitive_classes_present == ["internal_network"]


def test_document_redaction_audit_round_trips_without_content() -> None:
    document = RedactionPipeline.default().apply_to_document(_doc("password=" + "AuditSecret9"))
    payload = document.model_dump(mode="json")
    loaded = Document.model_validate(payload)
    assert loaded.privacy.redactions_applied[0].detector_id == "password-detector-v1"
    assert loaded.privacy.redactions_applied[0].snippet_hash
    assert "AuditSecret9" not in str(payload)