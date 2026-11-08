import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";
import { loginUrl } from "@/lib/auth-navigation";
import { NextResponse } from "next/server";
import { requireRoot } from "@/lib/root-admin";
import RootAdmin from "@/app/_components/RootAdmin";

export const metadata: Metadata = { title: "Администрирование — Anon", robots: { index: false, follow: false } };

export default async function AdminPage() {
  const access = await requireRoot();
  if (access instanceof NextResponse) {
    if (access.status === 401) redirect(loginUrl("/admin"));
    return <main style={{ padding: "3rem" }}><h1>Доступ запрещён</h1><p>У вас нет доступа к этой странице.</p><Link href="/">Вернуться к документам</Link></main>;
  }
  return <RootAdmin />;
}
