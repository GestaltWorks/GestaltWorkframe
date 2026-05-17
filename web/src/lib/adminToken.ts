/**
 * Admin access token storage for the curator UI.
 *
 * Stored in localStorage (not sessionStorage) so unlocking on one admin
 * page grants the same session to other admin-aware views in other
 * tabs. The token survives until explicitly cleared (clearAdminToken)
 * or until the user clears site data.
 */

const STORAGE_KEY = "admin-token";

export function readAdminToken(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

export function writeAdminToken(token: string): void {
  if (typeof window === "undefined") return;
  const trimmed = token.trim();
  try {
    if (trimmed) {
      window.localStorage.setItem(STORAGE_KEY, trimmed);
    } else {
      window.localStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    // Storage may be unavailable in private mode or with restrictive
    // browser settings. Silently fall through; the in-memory state
    // still carries the token for the current page lifetime.
  }
}

export function clearAdminToken(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // see writeAdminToken
  }
}
