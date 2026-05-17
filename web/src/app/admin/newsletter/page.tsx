import type { Metadata } from "next";
import AdminNewsletterPanel from "@/components/AdminNewsletterPanel";

export const metadata: Metadata = {
  title: "Admin Newsletter",
  robots: { index: false, follow: false },
};

export default function AdminNewsletterPage() {
  return <AdminNewsletterPanel />;
}
