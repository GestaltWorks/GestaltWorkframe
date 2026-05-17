import Link from "next/link";
import { NewsletterSignupForm } from "@/components/NewsletterSignupForm";
import { buildPageMetadata } from "@/lib/pageTemplates";
import { siteUrl } from "@/lib/site";
import { JsonLd, buildBreadcrumbList } from "@/components/StructuredData";

const pageDescription = "Subscribe to the digest.";

export const metadata = buildPageMetadata({
  title: "Subscribe",
  description: pageDescription,
  path: "/newsletter/subscribe",
});

const breadcrumbs = buildBreadcrumbList([
  { name: "Home", url: siteUrl },
  { name: "Subscribe", url: `${siteUrl}/newsletter/subscribe` },
]);

export default function NewsletterSubscribePage() {
  return (
    <main id="main-content" className="min-h-screen bg-brand-dark px-4 py-12 text-brand-offwhite sm:px-8 lg:px-12">
      <JsonLd data={breadcrumbs} />
      <div className="mx-auto max-w-3xl">
        <Link href="/" className="text-sm font-semibold text-brand-gold hover:text-brand-gold-mid">← Back home</Link>
        <header className="mt-6">
          <h1 className="mt-4 font-rajdhani text-5xl font-semibold leading-none text-brand-offwhite">Get on the mailing list.</h1>
          <p className="mt-5 text-base leading-7 text-brand-offwhite/72">
            Periodic updates delivered to your inbox. Unsubscribe in one click from any email.
          </p>
        </header>
        <section className="mt-8">
          <NewsletterSignupForm />
        </section>
      </div>
    </main>
  );
}
