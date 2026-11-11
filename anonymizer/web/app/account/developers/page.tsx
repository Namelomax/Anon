import type { Metadata } from "next";
import DeveloperAccess from "@/app/_components/DeveloperAccess";

export const metadata: Metadata = { title: "API — AnonAI" };

export default function DevelopersAccountPage() {
  return <DeveloperAccess />;
}
