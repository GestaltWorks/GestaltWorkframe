"""Direct corpus publishing for approved discovery finds into the configured GitHub corpus repo."""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from core.db import DiscoveryFind, DiscoverySource

def _library_cfg():
    """Lazy read of LibraryConfig; returns None if unavailable."""
    try:
        from core.deployment_config import get_deployment_config
        return get_deployment_config().library
    except Exception:
        return None


GITHUB_API = "https://api.github.com"
DEFAULT_REPO = os.getenv("LIBRARY_PUBLISH_REPO", "")
DEFAULT_BASE_BRANCH = "main"
DEFAULT_TARGET_DIR = "discovery/approved"


class LibraryPublisherConfigError(RuntimeError):
    """Raised when library publishing is not configured."""


class LibraryPublisherError(RuntimeError):
    """Raised when GitHub rejects a library publish request."""


@dataclass(frozen=True)
class LibraryPublishResult:
    public_url: str
    commit_url: str
    path: str


@dataclass(frozen=True)
class LibraryDeleteResult:
    commit_url: str
    path: str


def library_publisher_configured() -> bool:
    return _github_app_configured()


async def publish_find_to_library(
    find: DiscoveryFind,
    source: DiscoverySource,
    *,
    notes: str = "",
    target_path: str = "",
) -> LibraryPublishResult:
    lib = _library_cfg()
    repo = os.getenv("LIBRARY_PUBLISHER_REPO", (lib.publish_repo if lib else "") or DEFAULT_REPO).strip() or DEFAULT_REPO
    base_branch = os.getenv("LIBRARY_PUBLISHER_BASE_BRANCH", (lib.publish_base_branch if lib else "") or DEFAULT_BASE_BRANCH).strip() or DEFAULT_BASE_BRANCH
    target_dir = (lib.publish_target_dir if lib and lib.publish_target_dir else DEFAULT_TARGET_DIR)
    path = _safe_target_path(target_path or _default_target_path(find, target_dir))
    content = _document_content(find, source, notes=notes)
    token = await _publisher_token()

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(base_url=GITHUB_API, headers=headers, timeout=30) as client:
        existing_sha = await _existing_file_sha(client, repo, path, base_branch)
        public_url, commit_url = await _put_file(client, repo, path, base_branch, content, find.title, existing_sha)
    return LibraryPublishResult(public_url=public_url, commit_url=commit_url, path=path)


async def delete_library_file(path: str, *, title: str = "discovery reference") -> LibraryDeleteResult:
    lib = _library_cfg()
    repo = os.getenv("LIBRARY_PUBLISHER_REPO", (lib.publish_repo if lib else "") or DEFAULT_REPO).strip() or DEFAULT_REPO
    base_branch = os.getenv("LIBRARY_PUBLISHER_BASE_BRANCH", (lib.publish_base_branch if lib else "") or DEFAULT_BASE_BRANCH).strip() or DEFAULT_BASE_BRANCH
    safe_path = _safe_target_path(path)
    token = await _publisher_token()
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(base_url=GITHUB_API, headers=headers, timeout=30) as client:
        existing_sha = await _existing_file_sha(client, repo, safe_path, base_branch)
        if not existing_sha:
            return LibraryDeleteResult(commit_url="", path=safe_path)
        commit_url = await _delete_file(client, repo, safe_path, base_branch, title, existing_sha)
    return LibraryDeleteResult(commit_url=commit_url, path=safe_path)


async def _publisher_token() -> str:
    if _github_app_configured():
        app_jwt = _github_app_jwt()
        installation_id = os.environ["LIBRARY_PUBLISHER_GITHUB_INSTALLATION_ID"].strip()
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {app_jwt}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(base_url=GITHUB_API, headers=headers, timeout=30) as client:
            response = await client.post(f"/app/installations/{installation_id}/access_tokens")
        _raise_for_github(response, "mint library publisher installation token")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LibraryPublisherError("GitHub installation token response was not valid JSON") from exc
        token = str(payload.get("token") or "") if isinstance(payload, dict) else ""
        if not token:
            raise LibraryPublisherError("GitHub installation token response did not include a token")
        return token
    raise LibraryPublisherConfigError("library publisher GitHub App is not configured")


def _github_app_configured() -> bool:
    return all(
        os.getenv(name, "").strip()
        for name in (
            "LIBRARY_PUBLISHER_GITHUB_APP_ID",
            "LIBRARY_PUBLISHER_GITHUB_INSTALLATION_ID",
            "LIBRARY_PUBLISHER_GITHUB_PRIVATE_KEY_B64",
        )
    )


def _github_app_jwt() -> str:
    app_id = os.environ["LIBRARY_PUBLISHER_GITHUB_APP_ID"].strip()
    key_b64 = os.environ["LIBRARY_PUBLISHER_GITHUB_PRIVATE_KEY_B64"].strip()
    try:
        private_key = serialization.load_pem_private_key(base64.b64decode(key_b64), password=None)
    except Exception as exc:
        raise LibraryPublisherConfigError("library publisher GitHub App private key is invalid") from exc
    now = int(time.time())
    header = _jwt_segment({"alg": "RS256", "typ": "JWT"})
    # Backdate iat for small clock skew while keeping GitHub's 10-minute max window.
    payload = _jwt_segment({"iat": now - 60, "exp": now + 540, "iss": app_id})
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(signature)}"


def _jwt_segment(payload: dict[str, object]) -> str:
    return _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


async def _existing_file_sha(client: httpx.AsyncClient, repo: str, path: str, branch: str) -> str:
    response = await client.get(f"/repos/{repo}/contents/{path}", params={"ref": branch})
    if response.status_code == 404:
        return ""
    _raise_for_github(response, "load existing library file")
    return str(response.json().get("sha") or "")


async def _put_file(
    client: httpx.AsyncClient,
    repo: str,
    path: str,
    branch: str,
    content: str,
    title: str,
    existing_sha: str,
) -> tuple[str, str]:
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    payload = {
        "message": f"Update library discovery reference: {title[:80]}",
        "content": encoded,
        "branch": branch,
    }
    if existing_sha:
        payload["sha"] = existing_sha
    response = await client.put(
        f"/repos/{repo}/contents/{path}",
        json=payload,
    )
    _raise_for_github(response, "write library corpus file")
    data = response.json()
    content_url = str((data.get("content") or {}).get("html_url") or "")
    commit_url = str((data.get("commit") or {}).get("html_url") or "")
    return content_url, commit_url


async def _delete_file(
    client: httpx.AsyncClient,
    repo: str,
    path: str,
    branch: str,
    title: str,
    existing_sha: str,
) -> str:
    response = await client.delete(
        f"/repos/{repo}/contents/{path}",
        json={"message": f"Remove library discovery reference: {title[:80]}", "sha": existing_sha, "branch": branch},
    )
    _raise_for_github(response, "delete library corpus file")
    return str((response.json().get("commit") or {}).get("html_url") or "")


def _document_content(find: DiscoveryFind, source: DiscoverySource, *, notes: str) -> str:
    created = datetime.now(timezone.utc).date().isoformat()
    summary = _markdown_body(find.summary_text or "No summary available.")
    review_notes = _markdown_body(notes or find.decision_notes or "Approved through discovery review.")
    return (
        "---\n"
        f"title: \"{_yaml_escape(find.title)}\"\n"
        f"source_url: \"{_yaml_escape(find.url)}\"\n"
        f"source_name: \"{_yaml_escape(source.name)}\"\n"
        f"watch_type: \"{_yaml_escape(source.watch_type)}\"\n"
        f"finding_type: \"{_yaml_escape(find.finding_type)}\"\n"
        f"importance: \"{_yaml_escape(find.importance_signal)}\"\n"
        f"reviewed_at: \"{created}\"\n"
        "---\n\n"
        f"# {find.title}\n\n"
        f"Source: [{find.url}]({find.url})\n\n"
        f"{summary}\n\n"
        "## Review notes\n\n"
        f"{review_notes}\n"
    )


def _default_target_path(find: DiscoveryFind, target_dir: str = DEFAULT_TARGET_DIR) -> str:
    return f"{target_dir}/{datetime.now(timezone.utc):%Y/%m}/{_slug(find.title)}-{find.id[:8]}.md"


def _safe_target_path(path: str) -> str:
    cleaned = path.strip().replace("\\", "/")
    if (
        not cleaned
        or cleaned.startswith("/")
        or ".." in cleaned
        or ":" in cleaned
        or len(cleaned) > 512
        or not cleaned.endswith(".md")
    ):
        raise ValueError("library target path must be a relative .md path")
    return cleaned


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "discovery-find")[:72]


def _yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")[:512]


def _markdown_body(value: str) -> str:
    cleaned = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = ["- - -" if line.strip() == "---" else line for line in cleaned.split("\n")]
    return "\n".join(lines)[:4000]


def _raise_for_github(response: httpx.Response, action: str) -> None:
    if 200 <= response.status_code < 300:
        return
    raise LibraryPublisherError(f"GitHub failed to {action}: HTTP {response.status_code}")
