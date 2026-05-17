import Link from "next/link";
import { siteUrl } from "@/lib/site";
import { buildPageMetadata } from "@/lib/pageTemplates";
import { JsonLd, buildBreadcrumbList } from "@/components/StructuredData";
import { Card } from "@/components/ui/Card";
import { SectionLabel } from "@/components/ui/SectionLabel";

const pageDescription =
  "Sample privacy policy template describing data the application collects, retention windows, and deletion requests. Replace with deployment-specific language before production use.";

export const metadata = buildPageMetadata({
  title: "Privacy policy",
  description: pageDescription,
  path: "/privacy",
});

const privacyBreadcrumbs = buildBreadcrumbList([
  { name: "Home", url: siteUrl },
  { name: "Privacy", url: `${siteUrl}/privacy` },
]);

export default function PrivacyPage() {
  return (
    <main id="main-content" className="min-h-screen bg-brand-dark px-4 py-12 text-brand-offwhite sm:px-8 lg:px-12">
      <JsonLd data={privacyBreadcrumbs} />
      <div className="mx-auto max-w-3xl">
        <Link href="/" className="font-mono text-xs uppercase tracking-[0.26em] text-brand-gold-warm/75 hover:text-brand-gold">
          Back to home
        </Link>

        <Card variant="raised" className="mt-10 sm:p-10">
          <SectionLabel>Sample privacy policy</SectionLabel>
          <h1 className="mt-4 font-rajdhani text-5xl font-semibold leading-none text-brand-offwhite">Privacy policy template</h1>
          <p className="mt-6 text-lg leading-8 text-brand-offwhite/72">
            This is a placeholder privacy page shipped with the framework. Replace the body with the privacy policy that fits your deployment before going to production. The framework collects guided-intake answers, chat messages, newsletter signups, and contact submissions when those features are enabled. Document the collection, retention, and deletion handling that applies to your deployment.
          </p>
        </Card>

        <Card variant="muted" className="mt-8">
          <h2 className="font-rajdhani text-3xl font-semibold text-brand-offwhite">Defaults shipped with the framework</h2>
          <ul className="mt-4 space-y-2 text-base leading-7 text-brand-offwhite/72">
            <li><span className="text-brand-gold-warm">Chat conversations and messages:</span> retained per the retention sweep configured for your deployment.</li>
            <li><span className="text-brand-gold-warm">Terminal intake submissions:</span> stored with session id, IP, user agent for abuse limiting.</li>
            <li><span className="text-brand-gold-warm">Contact submissions:</span> stored as business records.</li>
            <li><span className="text-brand-gold-warm">Newsletter subscribers:</span> opted-in email plus unsubscribe token.</li>
            <li><span className="text-brand-gold-warm">No advertising trackers:</span> the template ships without ad pixels or third-party analytics.</li>
          </ul>
        </Card>
      </div>
    </main>
  );
}
