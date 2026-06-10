"""Encrypted API key store backed by SQLite.

Keys are AES-256-GCM encrypted. The 256-bit symmetric key is derived from the
admin token via PBKDF2-SHA256 with a per-row random 16-byte salt and 100 000
iterations. A fresh 12-byte nonce is chosen for every write.

The stored row contains: provider_id, salt (hex), nonce (hex), ciphertext
(hex), and updated_at. The admin token is never stored.

Runtime precedence (highest to lowest):
  1. Key stored in SQLite via this module (set at runtime via admin API)
  2. Environment variable (set at deploy time in .env)

`has_key(provider_id)` checks presence without decrypting -- safe to call
from any context that needs to show masked status in the UI.

`get_key(provider_id, admin_token)` decrypts and returns the raw key string,
or None on failure (bad token, wrong key, missing row). Errors are logged at
WARNING; they do not propagate.

`set_key(provider_id, raw_key, admin_token)` encrypts and upserts.

`delete_key(provider_id)` removes the row. After deletion, the env fallback
is used again.

All DB operations use aiosqlite directly (same pattern as CloudBudgetGate) so
the key store can be used before the full SQLModel engine is initialised.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from datetime import datetime, timezone

import aiosqlite
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

_KEY_ITERATIONS = 100_000
_KEY_LEN = 32   # 256 bits for AES-256-GCM
_SALT_LEN = 16  # bytes
_NONCE_LEN = 12  # 96-bit nonce for GCM

# Env vars to check for each provider ID when the SQLite store has no row.
_PROVIDER_ENV_VARS: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_CLOUD_API_KEY",
    "openai": "OPENAI_API_KEY",
    "github": "APP_GITHUB_TOKEN",
    "brave": "BRAVE_SEARCH_API_KEY",
}


def _derive_key(admin_token: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=SHA256(), length=_KEY_LEN, salt=salt, iterations=_KEY_ITERATIONS)
    return kdf.derive(admin_token.encode("utf-8"))


def _encrypt(plaintext: str, admin_token: str) -> tuple[bytes, bytes, bytes]:
    """Return (salt, nonce, ciphertext). All bytes; caller hex-encodes for storage."""
    salt = secrets.token_bytes(_SALT_LEN)
    nonce = secrets.token_bytes(_NONCE_LEN)
    key = _derive_key(admin_token, salt)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return salt, nonce, ct


def _decrypt(salt: bytes, nonce: bytes, ciphertext: bytes, admin_token: str) -> str | None:
    try:
        key = _derive_key(admin_token, salt)
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        return plaintext.decode("utf-8")
    except Exception as exc:
        logger.warning("Key store decryption failed: %s", type(exc).__name__)
        return None


class ApiKeyStore:
    """Async encrypted key store backed by a SQLite table.

    Instantiate with the path to the app's SQLite database (same file used
    by CloudBudgetGate and the app session store). Call `init()` once at
    startup to create the table if it doesn't exist.
    """

    def __init__(self, sqlite_path: str = "database.db") -> None:
        self._path = sqlite_path
        self._ready = False
        self._init_lock: asyncio.Lock | None = None

    async def init(self) -> None:
        if self._ready:
            return
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self._ready:  # re-check after acquiring the lock
                return
            try:
                async with aiosqlite.connect(self._path) as db:
                    await db.execute(
                        """
                        CREATE TABLE IF NOT EXISTS provider_key_store (
                            provider_id TEXT PRIMARY KEY,
                            salt        TEXT NOT NULL,
                            nonce       TEXT NOT NULL,
                            ciphertext  TEXT NOT NULL,
                            updated_at  TEXT NOT NULL
                        )
                        """
                    )
                    await db.commit()
                self._ready = True
            except Exception as exc:
                logger.warning("ApiKeyStore init failed: %s", exc)

    async def has_key(self, provider_id: str) -> bool:
        """Return True if a row exists for provider_id (no decryption)."""
        await self.init()
        if not self._ready:
            return False
        try:
            async with aiosqlite.connect(self._path) as db:
                cursor = await db.execute(
                    "SELECT 1 FROM provider_key_store WHERE provider_id = ? LIMIT 1",
                    (provider_id,),
                )
                return (await cursor.fetchone()) is not None
        except Exception as exc:
            logger.warning("ApiKeyStore.has_key failed: %s", exc)
            return False

    async def get_key(self, provider_id: str, admin_token: str) -> str | None:
        """Decrypt and return the stored key, or None if absent/wrong token."""
        await self.init()
        if not self._ready:
            return None
        try:
            async with aiosqlite.connect(self._path) as db:
                cursor = await db.execute(
                    "SELECT salt, nonce, ciphertext FROM provider_key_store WHERE provider_id = ? LIMIT 1",
                    (provider_id,),
                )
                row = await cursor.fetchone()
            if row is None:
                return None
            salt = bytes.fromhex(row[0])
            nonce = bytes.fromhex(row[1])
            ct = bytes.fromhex(row[2])
            return _decrypt(salt, nonce, ct, admin_token)
        except Exception as exc:
            logger.warning("ApiKeyStore.get_key failed: %s", exc)
            return None

    async def set_key(self, provider_id: str, raw_key: str, admin_token: str) -> bool:
        """Encrypt and upsert. Returns True on success."""
        await self.init()
        if not self._ready:
            return False
        try:
            salt, nonce, ct = _encrypt(raw_key, admin_token)
            now = datetime.now(timezone.utc).isoformat()
            async with aiosqlite.connect(self._path) as db:
                await db.execute(
                    """
                    INSERT INTO provider_key_store (provider_id, salt, nonce, ciphertext, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(provider_id) DO UPDATE SET
                        salt       = excluded.salt,
                        nonce      = excluded.nonce,
                        ciphertext = excluded.ciphertext,
                        updated_at = excluded.updated_at
                    """,
                    (provider_id, salt.hex(), nonce.hex(), ct.hex(), now),
                )
                await db.commit()
            return True
        except Exception as exc:
            logger.warning("ApiKeyStore.set_key failed: %s", exc)
            return False

    async def delete_key(self, provider_id: str) -> bool:
        """Remove the row. Returns True if a row was deleted."""
        await self.init()
        if not self._ready:
            return False
        try:
            async with aiosqlite.connect(self._path) as db:
                cursor = await db.execute(
                    "DELETE FROM provider_key_store WHERE provider_id = ?",
                    (provider_id,),
                )
                await db.commit()
                return cursor.rowcount > 0
        except Exception as exc:
            logger.warning("ApiKeyStore.delete_key failed: %s", exc)
            return False

    def env_fallback(self, provider_id: str) -> str:
        """Return the env-var key for provider_id, or empty string."""
        env_var = _PROVIDER_ENV_VARS.get(provider_id, "")
        return os.getenv(env_var, "").strip() if env_var else ""

    async def effective_key(self, provider_id: str, admin_token: str) -> str:
        """Return stored key if present, else env fallback."""
        stored = await self.get_key(provider_id, admin_token)
        return stored if stored else self.env_fallback(provider_id)
