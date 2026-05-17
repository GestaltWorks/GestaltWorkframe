import type { Metadata } from "next";
import AdminHealthPanel from "@/components/AdminHealthPanel";

export const metadata: Metadata = {
  title: "Admin Health",
  robots: { index: false, follow: false },
};

export default function AdminHealthPage() {
  return <AdminHealthPanel />;
}