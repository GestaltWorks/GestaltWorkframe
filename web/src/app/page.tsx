import Link from "next/link";
import ChatWidget from "@/components/ChatWidget";

export default function Home() {
  return (
    <div className="min-h-screen bg-brand-dark text-brand-offwhite">
      <ChatWidget />
      <div className="px-4 py-16 sm:px-8 lg:px-12">
        <div className="mx-auto max-w-3xl">
          <p className="font-rajdhani text-sm uppercase tracking-[0.32em] text-brand-gold-warm/80">Gestalt Workframe</p>
          <h1 className="mt-4 font-rajdhani text-4xl font-semibold leading-tight sm:text-5xl">
            A guided chat and intake framework.
          </h1>
          <p className="mt-5 text-base leading-7 text-brand-offwhite/70">
            Multi-mode chat, structured intake, retrieval-grounded answers, provider routing, and admin tooling. Configure a deployment to publish your own brand and copy on top of this template.
          </p>
          <div className="mt-8 flex flex-wrap gap-3">
            <Link href="/terminal" className="rounded-full bg-brand-gold px-5 py-3 font-semibold text-brand-dark hover:bg-brand-gold-mid">
              Open terminal
            </Link>
            <Link href="/privacy" className="rounded-full border border-brand-gold-warm/35 px-5 py-3 font-semibold text-brand-gold-warm hover:border-brand-gold hover:text-brand-gold">
              Privacy
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}