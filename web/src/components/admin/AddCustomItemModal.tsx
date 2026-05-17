"use client";

/**
 * AddCustomItemModal — operator-pasted URL → newsletter pipeline.
 *
 * Two-step flow:
 *   1. Paste URL, click "Fetch preview" → POST /extract-metadata.
 *      Server-side fetch with SSRF guards returns an OG/Twitter card
 *      preview. Fields are editable from this point.
 *   2. Click "Add to queue" → POST /manual-find.
 *      Persists the find under the synthetic `manual_curation` source
 *      with newsletter_pending=true by default.
 *
 * The component owns its own modal state and admin-token handling so
 * both the discovery panel and the newsletter panel can drop it in with
 * one prop (onCreated callback for the parent to refresh its lists).
 *
 * The modal sits behind a backdrop with role="dialog"+aria-modal so
 * keyboard users can Esc out. Open/close is controlled by the parent.
 */

import { FormEvent, useEffect, useState } from "react";
import { readAdminToken } from "@/lib/adminToken";
import { apiUrl } from "@/lib/api";

type ExtractedMetadata = {
  url: string;
  title: string;
  description: string;
  image_url: string;
  source_name: string;
  raw_html_length: number;
};

type ManualFindResponse = {
  find: {
    id: string;
    title: string;
    url: string;
    summary_text: string;
    newsletter_pending: boolean;
  };
};

type Props = {
  onClose: () => void;
  onCreated: (find: ManualFindResponse["find"]) => void;
  // Newsletter panel wants newsletter_pending=true by default; discovery
  // panel may want false. Defaulted true since the user goal is
  // "schedule into the newsletter".
  defaultQueueForNewsletter?: boolean;
};

const INITIAL_PREVIEW: ExtractedMetadata = {
  url: "",
  title: "",
  description: "",
  image_url: "",
  source_name: "",
  raw_html_length: 0,
};

// Render only when the modal should be visible. The parent gates mount
// with `{open && <AddCustomItemModal ... />}` so every reopen yields a
// fresh component instance — state initializers handle the reset.
export function AddCustomItemModal({
  onClose,
  onCreated,
  defaultQueueForNewsletter = true,
}: Props) {
  const [urlInput, setUrlInput] = useState("");
  const [preview, setPreview] = useState<ExtractedMetadata>(INITIAL_PREVIEW);
  const [hasPreview, setHasPreview] = useState(false);
  const [queueForNewsletter, setQueueForNewsletter] = useState(
    defaultQueueForNewsletter,
  );
  const [fetching, setFetching] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>("");

  // Esc closes the modal.
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const handleFetch = async (event: FormEvent) => {
    event.preventDefault();
    if (!urlInput.trim()) {
      setError("Paste a URL first.");
      return;
    }
    setFetching(true);
    setError("");
    try {
      const response = await fetch(apiUrl("/admin/api/discovery/extract-metadata"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Admin-Token": readAdminToken(),
        },
        body: JSON.stringify({ url: urlInput.trim() }),
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as
          | { detail?: string }
          | null;
        throw new Error(body?.detail || `Fetch failed (HTTP ${response.status})`);
      }
      const data = (await response.json()) as { metadata: ExtractedMetadata };
      setPreview(data.metadata);
      setHasPreview(true);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Fetch failed.");
    } finally {
      setFetching(false);
    }
  };

  const handleSave = async (event: FormEvent) => {
    event.preventDefault();
    if (!preview.url || !preview.title.trim()) {
      setError("Title and URL are required.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const response = await fetch(apiUrl("/admin/api/discovery/manual-find"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Admin-Token": readAdminToken(),
        },
        body: JSON.stringify({
          url: preview.url,
          title: preview.title.trim(),
          description: preview.description.trim(),
          image_url: preview.image_url.trim(),
          source_label: preview.source_name.trim(),
          queue_for_newsletter: queueForNewsletter,
        }),
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as
          | { detail?: string }
          | null;
        throw new Error(body?.detail || `Save failed (HTTP ${response.status})`);
      }
      const data = (await response.json()) as ManualFindResponse;
      onCreated(data.find);
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Save failed.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="add-custom-item-title"
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4 backdrop-blur-sm"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="mt-12 w-full max-w-2xl rounded-2xl border border-brand-gold-warm/30 bg-brand-dark p-6 shadow-2xl shadow-black/40">
        <div className="flex items-start justify-between gap-4">
          <h2
            id="add-custom-item-title"
            className="font-rajdhani text-xl font-semibold uppercase tracking-[0.2em] text-brand-gold-warm"
          >
            Add custom item
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-full border border-white/15 px-3 py-1 text-xs uppercase tracking-[0.2em] text-white/60 hover:border-white/40 hover:text-white"
          >
            Close
          </button>
        </div>

        <p className="mt-2 text-sm text-white/60">
          Paste any public URL. The server fetches the page, pulls the OG /
          Twitter card metadata, and lets you edit the preview before it lands
          in the queue.
        </p>

        {!hasPreview ? (
          <form onSubmit={handleFetch} className="mt-5 space-y-3">
            <label className="block text-xs uppercase tracking-[0.18em] text-white/60">
              URL
              <input
                type="url"
                required
                value={urlInput}
                onChange={(event) => setUrlInput(event.target.value)}
                placeholder="https://example.com/post"
                className="mt-1 w-full rounded-lg border border-white/15 bg-black/30 px-3 py-2 text-sm text-white placeholder:text-white/30 focus:border-brand-gold-warm/60 focus:outline-none"
                autoFocus
              />
            </label>

            {error ? (
              <p className="text-sm text-red-300">{error}</p>
            ) : null}

            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={onClose}
                className="rounded-full border border-white/15 px-4 py-2 text-sm text-white/70 hover:border-white/40 hover:text-white"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={fetching}
                className="rounded-full border border-brand-gold-warm/50 bg-brand-gold-warm/10 px-4 py-2 text-sm font-semibold text-brand-gold-warm hover:border-brand-gold hover:text-brand-gold disabled:cursor-not-allowed disabled:opacity-60"
              >
                {fetching ? "Fetching..." : "Fetch preview"}
              </button>
            </div>
          </form>
        ) : (
          <form onSubmit={handleSave} className="mt-5 space-y-3">
            <div className="rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-xs text-white/50">
              Source URL: <span className="text-white/70">{preview.url}</span>
            </div>

            <label className="block text-xs uppercase tracking-[0.18em] text-white/60">
              Title
              <input
                type="text"
                required
                value={preview.title}
                onChange={(event) =>
                  setPreview({ ...preview, title: event.target.value })
                }
                className="mt-1 w-full rounded-lg border border-white/15 bg-black/30 px-3 py-2 text-sm text-white focus:border-brand-gold-warm/60 focus:outline-none"
              />
            </label>

            <label className="block text-xs uppercase tracking-[0.18em] text-white/60">
              Description
              <textarea
                value={preview.description}
                onChange={(event) =>
                  setPreview({ ...preview, description: event.target.value })
                }
                rows={3}
                className="mt-1 w-full rounded-lg border border-white/15 bg-black/30 px-3 py-2 text-sm text-white focus:border-brand-gold-warm/60 focus:outline-none"
              />
            </label>

            <label className="block text-xs uppercase tracking-[0.18em] text-white/60">
              Source label
              <input
                type="text"
                value={preview.source_name}
                onChange={(event) =>
                  setPreview({ ...preview, source_name: event.target.value })
                }
                className="mt-1 w-full rounded-lg border border-white/15 bg-black/30 px-3 py-2 text-sm text-white focus:border-brand-gold-warm/60 focus:outline-none"
              />
            </label>

            <label className="block text-xs uppercase tracking-[0.18em] text-white/60">
              Image URL (optional)
              <input
                type="url"
                value={preview.image_url}
                onChange={(event) =>
                  setPreview({ ...preview, image_url: event.target.value })
                }
                placeholder="https://..."
                className="mt-1 w-full rounded-lg border border-white/15 bg-black/30 px-3 py-2 text-sm text-white placeholder:text-white/30 focus:border-brand-gold-warm/60 focus:outline-none"
              />
            </label>

            <label className="flex items-center gap-2 text-sm text-white/80">
              <input
                type="checkbox"
                checked={queueForNewsletter}
                onChange={(event) => setQueueForNewsletter(event.target.checked)}
              />
              Queue for next newsletter
            </label>

            {error ? (
              <p className="text-sm text-red-300">{error}</p>
            ) : null}

            <div className="flex flex-wrap justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={() => {
                  setHasPreview(false);
                  setError("");
                }}
                className="rounded-full border border-white/15 px-4 py-2 text-sm text-white/70 hover:border-white/40 hover:text-white"
              >
                Back
              </button>
              <button
                type="button"
                onClick={onClose}
                className="rounded-full border border-white/15 px-4 py-2 text-sm text-white/70 hover:border-white/40 hover:text-white"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={saving}
                className="rounded-full border border-brand-gold-warm/50 bg-brand-gold-warm/10 px-4 py-2 text-sm font-semibold text-brand-gold-warm hover:border-brand-gold hover:text-brand-gold disabled:cursor-not-allowed disabled:opacity-60"
              >
                {saving ? "Saving..." : "Add to queue"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
