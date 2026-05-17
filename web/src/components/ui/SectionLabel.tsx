import type { ReactNode } from "react";

/**
 * Small uppercase eyebrow label that sits above section headings.
 */
export function SectionLabel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <p className={`font-rajdhani text-sm uppercase tracking-[0.32em] text-brand-gold-warm/80 ${className}`}>
      {children}
    </p>
  );
}
