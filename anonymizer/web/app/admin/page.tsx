import type { Metadata } from "next";
import Link from "next/link";
import { NextResponse } from "next/server";
import { requireRoot } from "@/lib/root-admin";
import RootAdmin from "@/app/_components/RootAdmin";

export const metadata: Metadata = { title: "Администрирование — Anon" };

export default async function AdminPage() {
  const access = await requireRoot();
  if (access instanceof NextResponse) {
    return <main style={{ padding: "3rem" }}><h1>Доступ запрещён</h1><p>Страница доступна только root-администратору.</p><Link href="/">Вернуться к документам</Link></main>;
  }
  return <RootAdmin />;
}
