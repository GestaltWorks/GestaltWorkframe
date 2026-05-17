import type { Metadata } from "next";
import AdminDiscoveryPanel from "@/components/AdminDiscoveryPanel";

export const metadata: Metadata = {
  title: "Admin Discovery",
  robots: { index: false, follow: false },
};

export default function AdminDiscoveryPage() {
  return <AdminDiscoveryPanel />;
}