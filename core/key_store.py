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

Audit logging: All key access operations (get, set, delete) are logged to an
audit table with the operation type, provider_id, success status, and timestamp.
The audit log never contains the actual key values.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

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
                    # Audit log table - never stores actual keys
                    await db.execute(
                        """
                        CREATE TABLE IF NOT EXISTS provider_key_audit_log (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            provider_id TEXT NOT NULL,
                            operation TEXT NOT NULL,  -- 'get', 'set', 'delete', 'list', 'export', 'import'
                            success BOOLEAN NOT NULL,
                            error_type TEXT,  -- e.g., 'decryption_failed', 'not_found', 'permission_denied'
                            source_ip TEXT,   -- optional, for admin tracking
                            created_at TEXT NOT NULL
                        )
                        """
                    )
                    await db.execute(
                        "CREATE INDEX IF NOT EXISTS idx_key_audit_provider ON provider_key_audit_log(provider_id, created_at)"
                    )
                    await db.execute(
                        "CREATE INDEX IF NOT EXISTS idx_key_audit_op ON provider_key_audit_log(operation, created_at)"
                    )
                    await db.commit()
                self._ready = True
            except Exception as exc:
                logger.warning("ApiKeyStore init failed: %s", exc)

    async def _log_access(
        self,
        provider_id: str,
        operation: str,
        success: bool,
        error_type: str | None = None,
        source_ip: str | None = None,
    ) -> None:
        """Log a key access operation to the audit table."""
        if not self._ready:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            async with aiosqlite.connect(self._path) as db:
                await db.execute(
                    """
                    INSERT INTO provider_key_audit_log (provider_id, operation, success, error_type, source_ip, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (provider_id, operation, success, error_type or "", source_ip or "", now),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("Failed to log key access: %s", exc)

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
                result = (await cursor.fetchone()) is not None
            await self._log_access(provider_id, "has", result)
            return result
        except Exception as exc:
            logger.warning("ApiKeyStore.has_key failed: %s", exc)
            await self._log_access(provider_id, "has", False, error_type=type(exc).__name__)
            return False

    async def get_key(self, provider_id: str, admin_token: str, source_ip: str | None = None) -> str | None:
        """Decrypt and return the stored key, or None if absent/wrong token."""
        await self.init()
        if not self._ready:
            await self._log_access(provider_id, "get", False, error_type="store_not_ready", source_ip=source_ip)
            return None
        try:
            async with aiosqlite.connect(self._path) as db:
                cursor = await db.execute(
                    "SELECT salt, nonce, ciphertext FROM provider_key_store WHERE provider_id = ? LIMIT 1",
                    (provider_id,),
                )
                row = await cursor.fetchone()
            if row is None:
                await self._log_access(provider_id, "get", False, error_type="not_found", source_ip=source_ip)
                return None
            salt = bytes.fromhex(row[0])
            nonce = bytes.fromhex(row[1])
            ct = bytes.fromhex(row[2])
            result = _decrypt(salt, nonce, ct, admin_token)
            if result is None:
                await self._log_access(provider_id, "get", False, error_type="decryption_failed", source_ip=source_ip)
            else:
                await self._log_access(provider_id, "get", True, source_ip=source_ip)
            return result
        except Exception as exc:
            logger.warning("ApiKeyStore.get_key failed: %s", exc)
            await self._log_access(provider_id, "get", False, error_type=type(exc).__name__, source_ip=source_ip)
            return None

    def has_key_sync(self, provider_id: str) -> bool:
        """Synchronous version of has_key for use in non-async contexts."""
        # Check if table exists first
        try:
            conn = sqlite3.connect(self._path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM provider_key_store WHERE provider_id = ? LIMIT 1",
                (provider_id,),
            )
            result = cursor.fetchone() is not None
            conn.close()
            return result
        except Exception:
            # Table may not exist yet or other error
            return False

    def get_key_sync(self, provider_id: str, admin_token: str) -> str | None:
        """Synchronous version of get_key for use in non-async contexts."""
        try:
            conn = sqlite3.connect(self._path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT salt, nonce, ciphertext FROM provider_key_store WHERE provider_id = ? LIMIT 1",
                (provider_id,),
            )
            row = cursor.fetchone()
            conn.close()
            if row is None:
                return None
            salt = bytes.fromhex(row[0])
            nonce = bytes.fromhex(row[1])
            ct = bytes.fromhex(row[2])
            return _decrypt(salt, nonce, ct, admin_token)
        except Exception:
            return None

    async def set_key(self, provider_id: str, raw_key: str, admin_token: str, source_ip: str | None = None) -> bool:
        """Encrypt and upsert. Returns True on success."""
        await self.init()
        if not self._ready:
            await self._log_access(provider_id, "set", False, error_type="store_not_ready", source_ip=source_ip)
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
            await self._log_access(provider_id, "set", True, source_ip=source_ip)
            return True
        except Exception as exc:
            logger.warning("ApiKeyStore.set_key failed: %s", exc)
            await self._log_access(provider_id, "set", False, error_type=type(exc).__name__, source_ip=source_ip)
            return False

    async def delete_key(self, provider_id: str, source_ip: str | None = None) -> bool:
        """Remove the row. Returns True if a row was deleted."""
        await self.init()
        if not self._ready:
            await self._log_access(provider_id, "delete", False, error_type="store_not_ready", source_ip=source_ip)
            return False
        try:
            async with aiosqlite.connect(self._path) as db:
                cursor = await db.execute(
                    "DELETE FROM provider_key_store WHERE provider_id = ?",
                    (provider_id,),
                )
                await db.commit()
                deleted = cursor.rowcount > 0
            await self._log_access(provider_id, "delete", deleted, source_ip=source_ip)
            return deleted
        except Exception as exc:
            logger.warning("ApiKeyStore.delete_key failed: %s", exc)
            await self._log_access(provider_id, "delete", False, error_type=type(exc).__name__, source_ip=source_ip)
            return False

    def env_fallback(self, provider_id: str) -> str:
        """Return the env-var key for provider_id, or empty string."""
        env_var = _PROVIDER_ENV_VARS.get(provider_id, "")
        return os.getenv(env_var, "").strip() if env_var else ""

    async def list_keys(self) -> list[dict]:
        """Return list of all stored key metadata (no decryption)."""
        await self.init()
        if not self._ready:
            return []
        try:
            async with aiosqlite.connect(self._path) as db:
                cursor = await db.execute(
                    "SELECT provider_id, salt, nonce, ciphertext, updated_at FROM provider_key_store ORDER BY provider_id"
                )
                rows = await cursor.fetchall()
            await self._log_access("*", "list", True)
            return [
                {
                    "provider_id": row[0],
                    "salt": row[1],
                    "nonce": row[2],
                    "ciphertext": row[3],
                    "updated_at": row[4],
                }
                for row in rows
            ]
        except Exception as exc:
            logger.warning("ApiKeyStore.list_keys failed: %s", exc)
            await self._log_access("*", "list", False, error_type=type(exc).__name__)
            return []

    async def export_encrypted(self) -> dict:
        """Export all keys in encrypted form (safe for backup). Returns manifest."""
        keys = await self.list_keys()
        result = {
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "store_path": self._path,
            "key_count": len(keys),
            "keys": keys,
        }
        await self._log_access("*", "export", True)
        return result

    async def import_encrypted(self, manifest: dict, admin_token: str) -> tuple[int, int]:
        """Import keys from manifest. Returns (imported, skipped). Skips if key identical."""
        await self.init()
        if not self._ready:
            await self._log_access("*", "import", False, error_type="store_not_ready")
            return (0, 0)
        imported = 0
        skipped = 0
        for key_data in manifest.get("keys", []):
            provider_id = key_data["provider_id"]
            stored = await self.get_key(provider_id, admin_token)
            if stored:
                # Check if same key by re-encrypting and comparing ciphertext
                salt, nonce, ct = _encrypt(stored, admin_token)
                if salt.hex() == key_data["salt"] and ct.hex() == key_data["ciphertext"]:
                    skipped += 1
                    continue
            # Import the key
            try:
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
                        (provider_id, key_data["salt"], key_data["nonce"], key_data["ciphertext"], key_data["updated_at"]),
                    )
                    await db.commit()
                imported += 1
            except Exception as exc:
                logger.warning("Import failed for %s: %s", provider_id, exc)
        await self._log_access("*", "import", True)
        return (imported, skipped)

    async def effective_key(self, provider_id: str, admin_token: str) -> str:
        """Return stored key if present, else env fallback."""
        stored = await self.get_key(provider_id, admin_token)
        return stored if stored else self.env_fallback(provider_id)

    async def get_audit_log(
        self,
        provider_id: str | None = None,
        operation: str | None = None,
        hours: int = 24,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Retrieve audit log entries."""
        await self.init()
        if not self._ready:
            return []
        since = datetime.now(timezone.utc).replace(hour=datetime.now(timezone.utc).hour - hours).isoformat()
        try:
            async with aiosqlite.connect(self._path) as db:
                if provider_id and operation:
                    cursor = await db.execute(
                        "SELECT provider_id, operation, success, error_type, source_ip, created_at FROM provider_key_audit_log WHERE provider_id = ? AND operation = ? AND created_at > ? ORDER BY created_at DESC LIMIT ?",
                        (provider_id, operation, since, limit),
                    )
                elif provider_id:
                    cursor = await db.execute(
                        "SELECT provider_id, operation, success, error_type, source_ip, created_at FROM provider_key_audit_log WHERE provider_id = ? AND created_at > ? ORDER BY created_at DESC LIMIT ?",
                        (provider_id, since, limit),
                    )
                elif operation:
                    cursor = await db.execute(
                        "SELECT provider_id, operation, success, error_type, source_ip, created_at FROM provider_key_audit_log WHERE operation = ? AND created_at > ? ORDER BY created_at DESC LIMIT ?",
                        (operation, since, limit),
                    )
                else:
                    cursor = await db.execute(
                        "SELECT provider_id, operation, success, error_type, source_ip, created_at FROM provider_key_audit_log WHERE created_at > ? ORDER BY created_at DESC LIMIT ?",
                        (since, limit),
                    )
                rows = await cursor.fetchall()
            return [
                {
                    "provider_id": row[0],
                    "operation": row[1],
                    "success": bool(row[2]),
                    "error_type": row[3],
                    "source_ip": row[4],
                    "created_at": row[5],
                }
                for row in rows
            ]
        except Exception as exc:
            logger.warning("Failed to retrieve audit log: %s", exc)
            return []

    async def cleanup_old_audit_logs(self, days: int = 30) -> int:
        """Remove audit log entries older than specified days. Returns deleted count."""
        await self.init()
        if not self._ready:
            return 0
        cutoff = datetime.now(timezone.utc).replace(day=datetime.now(timezone.utc).day - days).isoformat()
        try:
            async with aiosqlite.connect(self._path) as db:
                cursor = await db.execute(
                    "DELETE FROM provider_key_audit_log WHERE created_at < ?",
                    (cutoff,),
                )
                await db.commit()
                return cursor.rowcount
        except Exception as exc:
            logger.warning("Failed to cleanup audit logs: %s", exc)
            return 0
