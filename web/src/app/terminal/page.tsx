import type { Metadata } from "next";
import ChatWidget from "@/components/ChatWidget";
import { siteUrl } from "@/lib/site";

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: {
    absolute: "Terminal",
  },
  description: "Guided terminal.",
  alternates: {
    canonical: `${siteUrl}/terminal`,
  },
  robots: {
    index: false,
    follow: false,
  },
};

export default function TerminalPage() {
  return <ChatWidget />;
}