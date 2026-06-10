from dataclasses import dataclass
from urllib.parse import quote, urlencode


@dataclass(frozen=True)
class SourceLinkProfile:
    source_name: str
    repository_url: str
    branch: str = "main"
    library_url: str = ""
    source_query_param: str = "source"


SOURCE_LINK_PROFILES: tuple[SourceLinkProfile, ...] = ()


def quote_source_path(source: str) -> str:
    return "/".join(quote(part, safe="") for part in source.replace("\\", "/").split("/"))


def source_link_profile(
    metadata: dict,
    profiles: tuple[SourceLinkProfile, ...] = SOURCE_LINK_PROFILES,
) -> SourceLinkProfile | None:
    source_name = str(metadata.get("source_name") or metadata.get("corpus") or "").strip()
    canonical = str(metadata.get("canonical_url") or "").strip().rstrip("/")
    for profile in profiles:
        if source_name == profile.source_name:
            return profile
        if profile.repository_url and canonical == profile.repository_url.rstrip("/"):
            return profile
    return None


def public_source_url(
    metadata: dict,
    profiles: tuple[SourceLinkProfile, ...] = SOURCE_LINK_PROFILES,
) -> str:
    source = str(metadata.get("source") or "").strip()
    canonical = str(metadata.get("canonical_url") or "").strip()
    source_type = str(metadata.get("source_type") or "").strip()

    if source.startswith(("http://", "https://")):
        return source
    if source_type == "external_url_reference" and canonical.startswith(("http://", "https://")):
        return canonical

    profile = source_link_profile(metadata, profiles)
    if profile and profile.repository_url:
        if not source or source.startswith("local:file"):
            return profile.repository_url
        return f"{profile.repository_url.rstrip('/')}/blob/{profile.branch}/{quote_source_path(source)}"

    if canonical.startswith(("http://", "https://")):
        return canonical
    return ""


def library_entry_url(
    metadata: dict | None = None,
    profiles: tuple[SourceLinkProfile, ...] = SOURCE_LINK_PROFILES,
) -> str:
    if not profiles:
        return ""
    profile = source_link_profile(metadata or {}, profiles) if metadata else profiles[0]
    if not profile or not profile.library_url:
        return ""
    if not metadata:
        return profile.library_url
    source = str(metadata.get("source") or "").strip()
    if not source:
        return profile.library_url
    return f"{profile.library_url}?{urlencode({profile.source_query_param: source})}"
