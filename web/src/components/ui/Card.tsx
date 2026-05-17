import type { ReactNode } from "react";

/**
 * The repeated section-card surface across the public pages: rounded
 * border-on-dark-tint with a subtle shadow. The "raised" variant adds
 * a stronger drop shadow used for the page's hero/lead card; "muted"
 * is the smaller follow-up sections.
 */
type CardVariant = "raised" | "muted";

const VARIANT_CLASSES: Record<CardVariant, string> = {
  raised: "rounded-3xl border border-brand-gold-warm/20 bg-white/[0.035] p-7 shadow-2xl shadow-black/25",
  muted: "rounded-3xl border border-brand-gold-warm/20 bg-black/25 p-7",
};

export function Card({
  children,
  variant = "raised",
  as: Tag = "section",
  className = "",
  ariaLabelledBy,
}: {
  children: ReactNode;
  variant?: CardVariant;
  as?: "section" | "article" | "div";
  className?: string;
  ariaLabelledBy?: string;
}) {
  return (
    <Tag aria-labelledby={ariaLabelledBy} className={`${VARIANT_CLASSES[variant]} ${className}`}>
      {children}
    </Tag>
  );
}
