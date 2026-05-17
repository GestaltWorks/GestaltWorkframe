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
import ipaddress
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import yaml

from gestalt_connector_protocol.models import Document, RedactionEvent


NEVER_ALLOW_CLASSES = frozenset({"api_key", "password", "private_key", "jwt"})


@dataclass(frozen=True)
class RedactionDetection:
    detector_id: str
    sensitive_class: str
    start: int
    end: int
    value: str


class RedactionDetector(Protocol):
    detector_id: str
    sensitive_class: str

    def detect(self, text: str) -> list[RedactionDetection]: ...


@dataclass(frozen=True)
class RedactionWhitelist:
    allow_sensitive_classes: frozenset[str] = field(default_factory=frozenset)
    internal_hostname_patterns: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: object) -> "RedactionWhitelist":
        if not isinstance(data, dict):
            return cls()
        allowed = data.get("allow_sensitive_classes", [])
        host_patterns = data.get("internal_hostname_patterns", [])
        return cls(
            allow_sensitive_classes=frozenset(str(item) for item in allowed),
            internal_hostname_patterns=tuple(str(item) for item in host_patterns),
        )

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> "RedactionWhitelist":
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.from_mapping(loaded)

    def allows(self, sensitive_class: str) -> bool:
        return sensitive_class in self.allow_sensitive_classes and sensitive_class not in NEVER_ALLOW_CLASSES


class RedactionPipeline:
    def __init__(self, whitelist: RedactionWhitelist | None = None) -> None:
        self.whitelist = whitelist or RedactionWhitelist()
        self._detectors: list[RedactionDetector] = []

    @classmethod
    def default(cls, whitelist: RedactionWhitelist | None = None) -> "RedactionPipeline":
        pipeline = cls(whitelist=whitelist)
        pipeline.register(PasswordDetector())
        pipeline.register(APIKeyDetector())
        pipeline.register(PrivateKeyDetector())
        pipeline.register(JWTDetector())
        pipeline.register(PIIDetector())
        pipeline.register(InternalNetworkDetector(hostname_patterns=pipeline.whitelist.internal_hostname_patterns))
        return pipeline

    def register(self, detector: RedactionDetector) -> None:
        self._detectors.append(detector)

    def run(self, text: str) -> tuple[str, list[RedactionEvent]]:
        detections = self._merged_detections(text)
        return self._redact(text, detections)

    def _redact(self, text: str, detections: list[RedactionDetection]) -> tuple[str, list[RedactionEvent]]:
        redacted = text
        events: list[RedactionEvent] = []
        for detection in reversed(detections):
            if self.whitelist.allows(detection.sensitive_class):
                continue
            redacted = f"{redacted[:detection.start]}[REDACTED:{detection.sensitive_class}]{redacted[detection.end:]}"
            events.append(_event(detection))
        events.reverse()
        return redacted, events

    def apply_to_document(self, document: Document) -> Document:
        detections = self._merged_detections(document.body_text)
        redacted, events = self._redact(document.body_text, detections)
        classes = sorted({item.sensitive_class for item in detections} | set(document.privacy.sensitive_classes_present))
        cloud_eligible = document.privacy.cloud_llm_eligible and not classes
        return document.model_copy(
            update={
                "body_text": redacted,
                "privacy": document.privacy.model_copy(
                    update={
                        "cloud_llm_eligible": cloud_eligible,
                        "contains_pii": document.privacy.contains_pii or any(_is_pii(item) for item in classes),
                        "sensitive_classes_present": classes,
                        "redactions_applied": [*document.privacy.redactions_applied, *events],
                    }
                ),
            },
            deep=True,
        )

    def _merged_detections(self, text: str) -> list[RedactionDetection]:
        found: list[RedactionDetection] = []
        for detector in self._detectors:
            found.extend(detector.detect(text))
        found.sort(key=lambda item: (item.start, -(item.end - item.start)))
        merged: list[RedactionDetection] = []
        last_end = -1
        for detection in found:
            if detection.start < last_end:
                continue
            merged.append(detection)
            last_end = detection.end
        return merged


class _RegexDetector:
    detector_id: str
    sensitive_class: str
    patterns: tuple[re.Pattern[str], ...]

    def detect(self, text: str) -> list[RedactionDetection]:
        found: list[RedactionDetection] = []
        for pattern in self.patterns:
            for match in pattern.finditer(text):
                group = "secret" if "secret" in pattern.groupindex else 0
                value = match.group(group)
                start, end = match.span(group)
                found.append(RedactionDetection(self.detector_id, self.sensitive_class, start, end, value))
        return found


class PasswordDetector(_RegexDetector):
    detector_id = "password-detector-v1"
    sensitive_class = "password"
    patterns = (
        re.compile(r"(?im)\b(?:password|passwd|pwd|client_secret|secret)\b\s*[:=]\s*[\"']?(?P<secret>[^\s\"']{6,})"),
    )

    def detect(self, text: str) -> list[RedactionDetection]:
        found = super().detect(text)
        for match in re.finditer(r"(?im)\b[A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN)[A-Z0-9_]*\b\s*[:=]\s*[\"']?(?P<secret>[^\s\"']{8,})", text):
            value = match.group("secret")
            if _entropy(value) >= 1.5:
                found.append(RedactionDetection(self.detector_id, self.sensitive_class, match.start("secret"), match.end("secret"), value))
        return found


class APIKeyDetector(_RegexDetector):
    detector_id = "api-key-detector-v1"
    sensitive_class = "api_key"
    patterns = (
        re.compile(r"(?P<secret>AKIA[0-9A-Z]{16})"),
        re.compile(r"(?P<secret>ghp_[A-Za-z0-9_]{20,})"),
        re.compile(r"(?P<secret>github_pat_[A-Za-z0-9_]{20,})"),
        re.compile(r"(?P<secret>sk_live_[A-Za-z0-9]{16,})"),
        re.compile(r"(?P<secret>sk-ant-[A-Za-z0-9_-]{20,})"),
        re.compile(r"(?P<secret>sk-[A-Za-z0-9]{20,})"),
        re.compile(r"(?P<secret>AIza[0-9A-Za-z_-]{20,})"),
        re.compile(r"(?i)\b(?:azure|google|openai|anthropic|stripe)[_-]?(?:api[_-]?key|client[_-]?secret)\b\s*[:=]\s*[\"']?(?P<secret>[^\s\"']{16,})"),
    )


class PrivateKeyDetector(_RegexDetector):
    detector_id = "private-key-detector-v1"
    sensitive_class = "private_key"
    patterns = (re.compile(r"(?P<secret>-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----)"),)


class JWTDetector(_RegexDetector):
    detector_id = "jwt-detector-v1"
    sensitive_class = "jwt"
    patterns = (re.compile(r"(?P<secret>eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"),)


class PIIDetector(RedactionDetector):
    detector_id = "pii-detector-v1"
    sensitive_class = "pii"

    def detect(self, text: str) -> list[RedactionDetection]:
        found: list[RedactionDetection] = []
        for sensitive_class, pattern in (
            ("ssn", re.compile(r"\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b")),
            ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
            ("phone", re.compile(r"(?<![0-9])(?:\+1[-. ]?)?\(?[0-9]{3}\)?[-. ][0-9]{3}[-. ][0-9]{4}(?![0-9])")),
        ):
            found.extend(_detections(pattern, text, self.detector_id, sensitive_class))
        for match in re.finditer(r"\b(?:[0-9][ -]*?){13,19}\b", text):
            value = match.group(0)
            if _luhn(value):
                found.append(RedactionDetection(self.detector_id, "credit_card", match.start(), match.end(), value))
        for match in re.finditer(r"(?i)\b(?:account|acct|routing)\b[^0-9]{0,20}(?P<secret>[0-9]{8,17})\b", text):
            found.append(RedactionDetection(self.detector_id, "bank_account", match.start("secret"), match.end("secret"), match.group("secret")))
        return found


class InternalNetworkDetector(RedactionDetector):
    detector_id = "internal-network-detector-v1"
    sensitive_class = "internal_network"

    def __init__(self, hostname_patterns: tuple[str, ...] = ()) -> None:
        self._hostname_patterns = tuple(re.compile(pattern, re.I) for pattern in hostname_patterns)

    def detect(self, text: str) -> list[RedactionDetection]:
        found: list[RedactionDetection] = []
        for match in re.finditer(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b", text):
            value = match.group(0)
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if address.is_private:
                found.append(RedactionDetection(self.detector_id, self.sensitive_class, match.start(), match.end(), value))
        for pattern in self._hostname_patterns:
            found.extend(_detections(pattern, text, self.detector_id, self.sensitive_class))
        return found


def _detections(pattern: re.Pattern[str], text: str, detector_id: str, sensitive_class: str) -> list[RedactionDetection]:
    return [RedactionDetection(detector_id, sensitive_class, match.start(), match.end(), match.group(0)) for match in pattern.finditer(text)]


def _event(detection: RedactionDetection) -> RedactionEvent:
    return RedactionEvent(
        detector_id=detection.detector_id,
        sensitive_class=detection.sensitive_class,
        snippet_hash=hashlib.sha256(detection.value.encode("utf-8")).hexdigest()[:16],
        position=detection.start,
    )


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    return -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())


def _luhn(value: str) -> bool:
    digits = [int(char) for char in value if char.isdigit()]
    if len(digits) < 13:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _is_pii(sensitive_class: str) -> bool:
    return sensitive_class in {"ssn", "email", "phone", "credit_card", "bank_account"}