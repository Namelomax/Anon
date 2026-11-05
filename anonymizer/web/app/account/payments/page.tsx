import type { Metadata } from "next";
import AccountBilling from "@/app/_components/AccountBilling";

export const metadata: Metadata = { title: "Платежи и реквизиты — Anon" };

export default function PaymentsPage() {
  return <AccountBilling section="payments" />;
}
