"use client";

import { FormEvent, useState } from "react";
import { apiUrl } from "@/lib/api";

type Role = "automation_engineer" | "student" | "interested_party";

const ROLE_OPTIONS: { value: Role; label: string; description: string }[] = [
  {
    value: "automation_engineer",
    label: "Builder",
    description: "Patterns, examples, schemas worth reusing.",
  },
  {
    value: "student",
    label: "Learning",
    description: "Concepts, walkthroughs, lesson material.",
  },
  {
    value: "interested_party",
    label: "Exploring services",
    description: "Scoping a project.",
  },
];

/**
 * Lightweight newsletter signup form: name, email, company, role.
 *
 * Submits to POST /newsletter/api/subscribe.
 */
export function NewsletterSignupForm() {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [company, setCompany] = useState("");
  const [role, setRole] = useState<Role>("automation_engineer");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [submitted, setSubmitted] = useState(false);

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const response = await fetch(apiUrl("/newsletter/api/subscribe"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, email, company, role }),
      });
      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(detail?.detail ?? `Subscribe failed with HTTP ${response.status}`);
      }
      setSubmitted(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Subscribe failed");
    } finally {
      setSubmitting(false);
    }
  };

  if (submitted) {
    return (
      <section className="rounded-3xl border border-brand-gold-warm/25 bg-gradient-to-br from-brand-sage/20 via-black/35 to-brand-gold/10 p-8 text-center shadow-2xl shadow-black/25" role="status" aria-live="polite">
        <h2 className="font-rajdhani text-3xl font-semibold text-brand-offwhite">You&apos;re on the list.</h2>
        <p className="mt-4 text-base leading-7 text-brand-offwhite/72">
          Check {email} for a confirmation email.
        </p>
        <p className="mt-4 text-sm text-brand-offwhite/55">
          You can unsubscribe at any time via the link at the bottom of every email.
        </p>
      </section>
    );
  }

  return (
    <form onSubmit={onSubmit} className="grid gap-5 rounded-3xl border border-brand-gold-warm/20 bg-black/30 p-7 shadow-2xl shadow-black/25 sm:p-8">
      <fieldset className="grid gap-4 sm:grid-cols-2">
        <label className="grid gap-1 text-sm text-brand-offwhite/75">
          <span>Name</span>
          <input
            type="text"
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={200}
            className="rounded-xl border border-brand-gold-warm/20 bg-black/35 px-4 py-2.5 text-brand-offwhite outline-none focus:border-brand-gold"
            autoComplete="name"
          />
        </label>
        <label className="grid gap-1 text-sm text-brand-offwhite/75">
          <span>Email</span>
          <input
            type="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            maxLength={320}
            className="rounded-xl border border-brand-gold-warm/20 bg-black/35 px-4 py-2.5 text-brand-offwhite outline-none focus:border-brand-gold"
            autoComplete="email"
          />
        </label>
        <label className="grid gap-1 text-sm text-brand-offwhite/75 sm:col-span-2">
          <span>Company <span className="text-brand-offwhite/45">(optional)</span></span>
          <input
            type="text"
            value={company}
            onChange={(event) => setCompany(event.target.value)}
            maxLength={200}
            className="rounded-xl border border-brand-gold-warm/20 bg-black/35 px-4 py-2.5 text-brand-offwhite outline-none focus:border-brand-gold"
            autoComplete="organization"
          />
        </label>
      </fieldset>

      <fieldset className="grid gap-2">
        <legend className="text-sm text-brand-offwhite/75">Which best describes you?</legend>
        <div className="grid gap-2">
          {ROLE_OPTIONS.map((opt) => (
            <label
              key={opt.value}
              className={`flex cursor-pointer items-start gap-3 rounded-xl border px-4 py-3 transition-colors ${
                role === opt.value
                  ? "border-brand-gold bg-brand-gold/10"
                  : "border-brand-gold-warm/20 bg-black/25 hover:border-brand-gold-warm/40"
              }`}
            >
              <input
                type="radio"
                name="role"
                value={opt.value}
                checked={role === opt.value}
                onChange={() => setRole(opt.value)}
                className="mt-1 h-4 w-4 accent-brand-gold"
              />
              <span>
                <span className="block font-rajdhani text-lg font-semibold text-brand-offwhite">{opt.label}</span>
                <span className="block text-xs text-brand-offwhite/55">{opt.description}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      {error && <p role="alert" className="text-sm text-red-300">{error}</p>}

      <p className="text-xs leading-5 text-brand-offwhite/55">
        Submitting opts you into the digest. Unsubscribe anytime via the link in every email. We do not sell or share your information.
      </p>

      <button
        type="submit"
        disabled={submitting}
        className="self-start rounded-full bg-brand-gold px-6 py-3 font-rajdhani text-lg font-semibold text-brand-dark transition-colors hover:bg-brand-gold-mid disabled:cursor-wait disabled:opacity-50"
      >
        {submitting ? "Subscribing..." : "Subscribe"}
      </button>
    </form>
  );
}
