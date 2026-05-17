import type { Metadata } from "next";
import { siteUrl, socialImageUrl } from "@/lib/site";

export type PageMetadataInput = {
  title: string;
  description: string;
  path: `/${string}`;
  siteName?: string;
  imageAlt?: string;
  keywords?: string[];
  index?: boolean;
};

export function absoluteSiteUrl(path: `/${string}`): string {
  return `${siteUrl}${path === "/" ? "" : path}`;
}

export function buildPageMetadata(input: PageMetadataInput): Metadata {
  const url = absoluteSiteUrl(input.path);
  const siteName = input.siteName ?? "Gestalt Workframe";
  const title = input.title.includes(siteName) ? input.title : `${input.title} | ${siteName}`;
  const index = input.index ?? true;

  return {
    metadataBase: new URL(siteUrl),
    title: input.title,
    description: input.description,
    keywords: input.keywords,
    alternates: { canonical: url },
    openGraph: {
      type: "website",
      locale: "en_US",
      siteName,
      title,
      description: input.description,
      url,
      images: [{ url: socialImageUrl, width: 1200, height: 630, alt: input.imageAlt ?? siteName, type: "image/png" }],
    },
    twitter: { card: "summary_large_image", title, description: input.description, images: [socialImageUrl] },
    robots: index ? { index: true, follow: true } : { index: false, follow: false },
  };
}
