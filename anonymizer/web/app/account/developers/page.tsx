import type { Metadata } from "next";
import DeveloperAccess from "@/app/_components/DeveloperAccess";

export const metadata: Metadata = { title: "API — Anon" };

export default function DevelopersAccountPage() {
  return <DeveloperAccess />;
}
