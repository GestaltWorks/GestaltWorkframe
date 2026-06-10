from __future__ import annotations

import os
import re
import signal
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEPLOYMENT_ID = "test-brand"
DEPLOYMENT_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
PUBLIC_COPY_KEYS_BLOCKLIST = ("secret", "token", "password", "api_key", "apikey", "auth", "credential")


class BrandConfig(BaseModel):
    name: str = "Gestalt Workframe"
    colors: dict[str, str] = Field(default_factory=dict)
    fonts: dict[str, str] = Field(default_factory=dict)
    logo_path: str = ""


class IdentityConfig(BaseModel):
    organization_name: str = "Gestalt Workframe"
    short_name: str = "GWF"
    public_email: str = "hello@example.com"
    bot_name: str = "Workframe Bot"
    bot_persona: str = ""
    signature_name: str = "the team"
    contact_path: str = "/contact"
    library_path: str = "/library"


class SiteConfig(BaseModel):
    base_url: str = "https://example.com"
    title: str = "Gestalt Workframe"
    description: str = "Brandable multi-mode chatbot framework."


class NavItem(BaseModel):
    label: str
    href: str


class IntakeQuestion(BaseModel):
    id: str
    label: str
    options: list[str] = Field(default_factory=list)


class NewsletterConfig(BaseModel):
    enabled: bool = True
    name: str = "Newsletter Digest"
    cadence_days: int = 7
    audience_visibility: Literal["public", "internal-team", "private"] = "public"


class DiscoveryConfig(BaseModel):
    enabled: bool = True


class CurriculumConfig(BaseModel):
    enabled: bool = False



class RoutingConfig(BaseModel):
    """Deployment routing signal overrides loaded from routing.yaml.

    Each field is a list of strings.  An empty list means "use the framework
    default for that signal group".  Deployments only need to override the
    groups whose vocabulary differs from the framework defaults.
    """

    small_talk: list[str] = Field(default_factory=list)
    learning_signals: list[str] = Field(default_factory=list)
    resource_signals: list[str] = Field(default_factory=list)
    implementation_signals: list[str] = Field(default_factory=list)
    trouble_signals: list[str] = Field(default_factory=list)
    service_signals: list[str] = Field(default_factory=list)
    pricing_signals: list[str] = Field(default_factory=list)
    discovery_signals: list[str] = Field(default_factory=list)
    confusion_signals: list[str] = Field(default_factory=list)
    urgency_signals: list[str] = Field(default_factory=list)
    technical_terms: list[str] = Field(default_factory=list)
    build_intent_terms: list[str] = Field(default_factory=list)
    direct_service_handoff_terms: list[str] = Field(default_factory=list)
    complex_build_phrases: list[str] = Field(default_factory=list)


class PersonaModeConfig(BaseModel):
    """Per-mode persona override entry in personas.yaml.

    Only non-empty fields override the framework default.
    ``id`` is the only required field.
    """

    id: str
    name: str = ""
    description: str = ""
    system_prompt: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    force_secondary: bool = False


class PersonasConfig(BaseModel):
    """Deployment personas override loaded from personas.yaml."""

    modes: list[PersonaModeConfig] = Field(default_factory=list)


class LibraryConfig(BaseModel):
    """Deployment library corpus configuration loaded from library.yaml."""

    library_id: str = ""
    display_name: str = ""
    public_url: str = ""
    repo_url: str = ""
    publish_repo: str = ""
    publish_base_branch: str = "main"
    publish_target_dir: str = "discovery/approved"
    watchlist_seed_module: str = ""
    topic_taxonomy_path: str = ""


class DeploymentConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    deployment_id: str = DEFAULT_DEPLOYMENT_ID
    brand: BrandConfig = Field(default_factory=BrandConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    site: SiteConfig = Field(default_factory=SiteConfig)
    nav: list[NavItem] = Field(default_factory=list)
    copy_config: dict[str, Any] = Field(default_factory=dict, alias="copy")
    intake: list[IntakeQuestion] = Field(default_factory=list)
    connectors: list[dict[str, Any]] = Field(default_factory=list)
    redaction_whitelist: dict[str, Any] = Field(default_factory=dict)
    newsletter: NewsletterConfig = Field(default_factory=NewsletterConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    curriculum: CurriculumConfig = Field(default_factory=CurriculumConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    personas: PersonasConfig = Field(default_factory=PersonasConfig)
    library: LibraryConfig = Field(default_factory=LibraryConfig)

    def public_payload(self) -> dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "brand": self.brand.model_dump(),
            "identity": self.identity.model_dump(exclude={"bot_persona"}),
            "site": self.site.model_dump(),
            "nav": [item.model_dump() for item in self.nav],
            "copy": _public_copy(self.copy_config),
            "intake": [item.model_dump() for item in self.intake],
            "newsletter": _public_newsletter(self.newsletter),
            "discovery": self.discovery.model_dump(),
            "curriculum": self.curriculum.model_dump(),
        }

    def admin_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["connectors"] = _redact_sensitive(payload.get("connectors", []))
        return payload


def get_deployment_config() -> DeploymentConfig:
    return _load_cached(_deployment_id())


def reload_deployment_config() -> DeploymentConfig:
    _load_cached.cache_clear()
    return get_deployment_config()


@lru_cache(maxsize=8)
def _load_cached(deployment_id: str) -> DeploymentConfig:
    root = _deployment_root(deployment_id)
    data: dict[str, Any] = {"deployment_id": deployment_id}
    if not root.exists():
        return DeploymentConfig(**data)
    data["brand"] = _yaml(root / "brand.yaml", {})
    data["identity"] = _yaml(root / "identity.yaml", {})
    data["site"] = _yaml(root / "site.yaml", {})
    data["nav"] = _yaml(root / "nav.yaml", [])
    data["copy"] = _load_copy(root / "copy")
    data["intake"] = _yaml(root / "intake.yaml", [])
    data["connectors"] = _yaml(root / "connectors.yaml", [])
    data["redaction_whitelist"] = _yaml(root / "redaction_whitelist.yaml", {})
    data["newsletter"] = _yaml(root / "newsletter.yaml", {})
    data["discovery"] = _yaml(root / "discovery.yaml", {})
    data["curriculum"] = _yaml(root / "curriculum.yaml", {})
    data["routing"] = _yaml(root / "routing.yaml", {})
    data["personas"] = _yaml(root / "personas.yaml", {})
    data["library"] = _yaml(root / "library.yaml", {})
    return DeploymentConfig(**data)


def _deployment_id() -> str:
    raw = os.getenv("DEPLOYMENT_ID", DEFAULT_DEPLOYMENT_ID).strip() or DEFAULT_DEPLOYMENT_ID
    if not DEPLOYMENT_ID_PATTERN.fullmatch(raw):
        raise ValueError(f"Invalid DEPLOYMENT_ID: {raw!r}")
    return raw


def _deployment_root(deployment_id: str) -> Path:
    if not DEPLOYMENT_ID_PATTERN.fullmatch(deployment_id):
        raise ValueError(f"Invalid DEPLOYMENT_ID: {deployment_id!r}")
    root = (REPO_ROOT / "deployments" / deployment_id).resolve()
    deployments_root = (REPO_ROOT / "deployments").resolve()
    if deployments_root not in (root, *root.parents):
        raise ValueError(f"Invalid DEPLOYMENT_ID path: {deployment_id!r}")
    return root


def _yaml(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return default if loaded is None else loaded


def _load_copy(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return {item.stem: _yaml(item, {}) for item in sorted(path.glob("*.yaml"))}


def _public_copy(value: Any) -> Any:
    # Best-effort guard for operator-authored public copy. Copy bundles must still avoid placing secrets in public files.
    if isinstance(value, dict):
        return {key: _public_copy(item) for key, item in value.items() if not _blocked_public_key(str(key))}
    if isinstance(value, list):
        return [_public_copy(item) for item in value]
    return value


def _public_newsletter(value: NewsletterConfig) -> dict[str, Any]:
    return value.model_dump(include={"enabled", "name", "cadence_days"})


def _blocked_public_key(key: str) -> bool:
    lowered = key.lower()
    return any(blocked in lowered for blocked in PUBLIC_COPY_KEYS_BLOCKLIST)


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if _blocked_public_key(str(key)) else _redact_sensitive(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _install_sighup_reload() -> None:
    if not hasattr(signal, "SIGHUP"):
        return
    if threading.current_thread() is not threading.main_thread():
        return

    def _handler(signum: int, frame: object) -> None:
        _load_cached.cache_clear()

    try:
        signal.signal(signal.SIGHUP, _handler)
    except ValueError:
        return


_install_sighup_reload()
