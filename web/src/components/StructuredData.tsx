import { siteUrl } from "@/lib/site";

const organizationId = `${siteUrl}/#organization`;
const websiteId = `${siteUrl}/#website`;

type JsonLdData = Record<string, unknown> | Record<string, unknown>[];

export const siteStructuredData = {
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "Organization",
      "@id": organizationId,
      name: "Gestalt Workframe",
      url: siteUrl,
    },
    {
      "@type": "WebSite",
      "@id": websiteId,
      name: "Gestalt Workframe",
      url: siteUrl,
      publisher: { "@id": organizationId },
      inLanguage: "en-US",
    },
  ],
};

export function buildBreadcrumbList(items: { name: string; url: string }[]) {
  return {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: items.map((item, index) => ({
      "@type": "ListItem",
      position: index + 1,
      name: item.name,
      item: item.url,
    })),
  };
}

export function JsonLd({ data }: { data: JsonLdData }) {
  return (
    <script
      type="application/ld+json"
      dangerouslySetInnerHTML={{ __html: JSON.stringify(data).replace(/</g, "\\u003c").replace(/\//g, "\\u002f") }}
    />
  );
}
