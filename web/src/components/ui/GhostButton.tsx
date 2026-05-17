import Link from "next/link";
import type { ReactNode } from "react";

/**
 * The "ghost CTA" pill that appears throughout the public pages: rounded
 * border, gold-warm text, hover-amplified. Two flavors:
 *
 * - GhostLink: renders a Next.js Link (internal routes) or a plain <a>
 *   with target/rel set (external URLs).
 * - GhostButton: renders a button for in-page actions.
 *
 * Tailwind classes are consolidated here so a brand-token change doesn't
 * require touching every call site.
 */
const GHOST_CLASS =
  "inline-flex items-center justify-center rounded-full border border-brand-gold-warm/40 px-5 py-3 font-semibold text-brand-gold-warm transition-colors hover:border-brand-gold hover:text-brand-gold";

export function GhostLink({
  href,
  children,
  external = false,
  className = "",
}: {
  href: string;
  children: ReactNode;
  external?: boolean;
  className?: string;
}) {
  if (external) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" className={`${GHOST_CLASS} ${className}`}>
        {children}
      </a>
    );
  }
  return (
    <Link href={href} className={`${GHOST_CLASS} ${className}`}>
      {children}
    </Link>
  );
}

export function GhostButton({
  onClick,
  children,
  disabled,
  type = "button",
  className = "",
}: {
  onClick?: () => void;
  children: ReactNode;
  disabled?: boolean;
  type?: "button" | "submit" | "reset";
  className?: string;
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`${GHOST_CLASS} disabled:cursor-wait disabled:opacity-60 ${className}`}
    >
      {children}
    </button>
  );
}
