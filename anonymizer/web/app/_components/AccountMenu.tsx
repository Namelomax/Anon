"use client";

import { useEffect, useRef } from "react";
import Link from "next/link";
import { Code2, CreditCard, FileText, LogOut, Settings } from "lucide-react";
import styles from "./account-menu.module.css";

type Props = {
  email: string;
  planTitle?: string;
  renewalLabel: string;
  canManage?: boolean;
  onSettings: () => void;
  onSignOut: () => void;
};

export default function AccountMenu({ email, planTitle, renewalLabel, canManage = false, onSettings, onSignOut }: Props) {
  const details = useRef<HTMLDetailsElement>(null);

  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !details.current?.contains(event.target)) {
        details.current?.removeAttribute("open");
      }
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && details.current?.open) {
        details.current.open = false;
        details.current.querySelector("summary")?.focus();
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, []);

  const close = () => { if (details.current) details.current.open = false; };

  return (
    <div className={styles.account}>
      <details ref={details} className={styles.details}>
        <summary className={styles.trigger}>
          <span className={styles.avatar} aria-hidden="true">{email.trim().charAt(0).toUpperCase()}</span>
          <span className={styles.identity}><span className={styles.email}>{email}</span><span className={styles.plan}>{planTitle || "Аккаунт"}</span></span>
        </summary>
        <nav className={styles.menu} aria-label="Меню аккаунта">
          <Link href="/account/subscription" onClick={close}><CreditCard size={16} aria-hidden="true" />Моя подписка</Link>
          {canManage && <Link href="/account/payments" onClick={close}><FileText size={16} aria-hidden="true" />Платежи и реквизиты</Link>}
          <Link href="/account/developers" onClick={close}><Code2 size={16} aria-hidden="true" />API</Link>
          <button type="button" onClick={() => { close(); onSettings(); }}><Settings size={16} aria-hidden="true" />Настройки аккаунта</button>
          <button type="button" onClick={() => { close(); onSignOut(); }}><LogOut size={16} aria-hidden="true" />Выйти</button>
        </nav>
      </details>
      <Link className={styles.renew} href="/billing" aria-label={renewalLabel === "Тарифы" ? "Тарифы подписки" : "Продлить подписку"}>{renewalLabel}</Link>
    </div>
  );
}
