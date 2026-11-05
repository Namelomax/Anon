"use client";

import { useRef, useState, type FormEvent } from "react";
import Link from "next/link";
import { ArrowRight, Building2, FileLock2, Settings2 } from "lucide-react";
import { LEGAL_DOCS, LICENSOR } from "../../lib/legal";
import styles from "./OnPremiseLead.module.css";

export default function OnPremiseLead() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);
  const submitting = useRef(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current) return;
    const form = event.currentTarget;
    if (!form.reportValidity()) return;
    const fields = new FormData(form);
    submitting.current = true;
    setLoading(true);
    setError("");
    setSuccess(false);
    try {
      const response = await fetch("/api/leads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          phone: fields.get("phone"), company: fields.get("company"), email: fields.get("email"),
          consent: fields.get("consent") === "on", website: fields.get("website"),
        }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok || data?.ok !== true) {
        setError(typeof data?.error === "string" ? data.error : "Не удалось отправить заявку. Попробуйте ещё раз.");
        return;
      }
      form.reset();
      setSuccess(true);
    } catch {
      setError("Не удалось отправить заявку. Проверьте соединение и попробуйте ещё раз.");
    } finally {
      submitting.current = false;
      setLoading(false);
    }
  }

  return (
    <section className={styles.section} id="on-premise" aria-labelledby="on-premise-title">
      <div className={styles.copy}>
        <div className={styles.kicker}>ANON ДЛЯ КОМПАНИЙ</div>
        <h2 id="on-premise-title">Anon внутри <em>вашей компании</em></h2>
        <p className={styles.lead}>Не хотите отправлять документы в облако? Внедрим Anon в инфраструктуре вашей компании или в вашем контуре.</p>
        <p>Маскируйте персональные данные в привычных рабочих процессах, сохраняя контроль над документами и доступом к ним.</p>
        <div className={styles.benefits}>
          <div><Building2 size={20} /><span>Развёртывание в вашем контуре</span></div>
          <div><FileLock2 size={20} /><span>Обратимое маскирование документов</span></div>
          <div><Settings2 size={20} /><span>Обсудим условия внедрения</span></div>
        </div>
      </div>
      <div className={styles.card}>
        <h3>Обсудим внедрение</h3>
        <p>Оставьте контакты, и мы свяжемся с вами по заявке.</p>
        <form onSubmit={submit}>
          <label>Телефон <span aria-hidden="true">*</span><input name="phone" type="tel" autoComplete="tel" maxLength={80} required disabled={loading} /></label>
          <label>Компания <span className={styles.optional}>необязательно</span><input name="company" autoComplete="organization" maxLength={160} disabled={loading} /></label>
          <label>Email <span className={styles.optional}>необязательно</span><input name="email" type="email" autoComplete="email" maxLength={254} disabled={loading} /></label>
          <div className={styles.honeypot} aria-hidden="true"><label>Website<input name="website" tabIndex={-1} autoComplete="off" disabled={loading} /></label></div>
          <label className={styles.consent}><input type="checkbox" name="consent" required disabled={loading} /><span>Согласен на обработку контактов только для связи по заявке. <Link href={LEGAL_DOCS.privacy.href}>Политика конфиденциальности</Link></span></label>
          <button type="submit" disabled={loading}>{loading ? "Отправляем…" : "Отправить заявку"} <ArrowRight size={17} /></button>
          {error && <p className={styles.error} role="alert">{error}</p>}
          <p className={styles.status} aria-live="polite">{success ? "Заявка отправлена. Мы свяжемся с вами." : ""}</p>
        </form>
        <p>Или напишите нам: <a href={`mailto:${LICENSOR.email}`}>{LICENSOR.email}</a></p>
      </div>
    </section>
  );
}
