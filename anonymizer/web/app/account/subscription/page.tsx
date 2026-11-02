import type { Metadata } from "next";
import AccountBilling from "@/app/_components/AccountBilling";

export const metadata: Metadata = { title: "Моя подписка — Anon" };

export default function SubscriptionPage() {
  return <AccountBilling section="subscription" />;
}
