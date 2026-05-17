import type { Metadata } from "next";
import { Geist, Geist_Mono, Rajdhani, Inter } from "next/font/google";
import { JsonLd, siteStructuredData } from "@/components/StructuredData";
import { siteUrl } from "@/lib/site";
import "./globals.css";

const siteName = process.env.NEXT_PUBLIC_SITE_NAME ?? "Gestalt Workframe";
const siteDescription =
  process.env.NEXT_PUBLIC_SITE_DESCRIPTION ??
  "A guided chat and intake framework with structured intake, retrieval-grounded answers, provider routing, and admin tooling.";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

const rajdhani = Rajdhani({
  variable: "--font-rajdhani",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
});

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  applicationName: siteName,
  title: {
    default: siteName,
    template: `%s | ${siteName}`,
  },
  description: siteDescription,
  alternates: {
    canonical: "/",
  },
  icons: {
    icon: [
      { url: "/favicon.ico", sizes: "any" },
      { url: "/icon.png", type: "image/png", sizes: "512x512" },
    ],
    apple: [
      { url: "/apple-icon.png", sizes: "180x180", type: "image/png" },
    ],
    shortcut: "/favicon.ico",
  },
  openGraph: {
    type: "website",
    locale: "en_US",
    url: siteUrl,
    siteName,
    title: siteName,
    description: siteDescription,
  },
  twitter: {
    card: "summary_large_image",
    title: siteName,
    description: siteDescription,
  },
  robots: {
    index: true,
    follow: true,
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      dir="ltr"
      translate="yes"
      className={`${geistSans.variable} ${geistMono.variable} ${rajdhani.variable} ${inter.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <a href="#main-content" className="sr-only z-50 rounded-full bg-brand-gold px-4 py-3 font-semibold text-brand-dark focus:not-sr-only focus:fixed focus:left-4 focus:top-4">
          Skip to main content
        </a>
        <JsonLd data={siteStructuredData} />
        {children}
      </body>
    </html>
  );
}
