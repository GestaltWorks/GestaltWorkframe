from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ALLOWED_SOURCE_TYPES = frozenset({"directory", "html_file", "external_url_reference"})


@dataclass(frozen=True)
class CorpusSource:
    name: str
    path: Path
    source_type: str
    description: str
    canonical_url: str
    provenance: str
    license_notes: str
    attribution: str
    trust_tier: str
    refresh_policy: str
    display_policy: str
    retrieval_policy: str
    curriculum_policy: str
    agent_access_policy: str
    secret_handling: str
    public_display: bool
    retrieval: bool
    curriculum: bool


def _required(value: str, field: str, source_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{source_name} requires {field}")
    return normalized


def source_metadata(source: CorpusSource, source_path: str, extension: str) -> dict[str, str | bool]:
    normalized_source_path = source_path.replace("\\", "/")
    return {
        "source": normalized_source_path,
        "source_name": source.name,
        "corpus": source.name,
        "type": extension,
        "source_type": source.source_type,
        "extension": extension,
        "canonical_url": source.canonical_url,
        "provenance": source.provenance,
        "license_notes": source.license_notes,
        "attribution": source.attribution,
        "trust_tier": source.trust_tier,
        "refresh_policy": source.refresh_policy,
        "display_policy": source.display_policy,
        "retrieval_policy": source.retrieval_policy,
        "curriculum_policy": source.curriculum_policy,
        "agent_access_policy": source.agent_access_policy,
        "secret_handling": source.secret_handling,
        "public_display": source.public_display,
        "retrieval": source.retrieval,
        "curriculum": source.curriculum,
    }


def validate_source_registry(sources: Iterable[CorpusSource]) -> None:
    names: set[str] = set()
    errors: list[str] = []
    for source in sources:
        try:
            name = _required(source.name, "name", "corpus source")
            if name in names:
                errors.append(f"duplicate source name: {name}")
            names.add(name)
            if source.source_type not in ALLOWED_SOURCE_TYPES:
                errors.append(f"{name} has unsupported source_type: {source.source_type}")
            for field, value in {
                "description": source.description,
                "provenance": source.provenance,
                "license_notes": source.license_notes,
                "attribution": source.attribution,
                "trust_tier": source.trust_tier,
                "refresh_policy": source.refresh_policy,
                "display_policy": source.display_policy,
                "retrieval_policy": source.retrieval_policy,
                "agent_access_policy": source.agent_access_policy,
                "secret_handling": source.secret_handling,
            }.items():
                _required(value, field, name)
            if source.public_display and not source.canonical_url.strip():
                errors.append(f"{name} public_display requires canonical_url")
            if source.curriculum and not source.curriculum_policy.strip():
                errors.append(f"{name} curriculum requires curriculum_policy")
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("Invalid source registry: " + "; ".join(errors))
