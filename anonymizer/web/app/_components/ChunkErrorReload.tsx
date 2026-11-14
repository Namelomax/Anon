"use client";

import { useEffect } from "react";

// Защита от ChunkLoadError после деплоя. Старая вкладка держит HTML
// предыдущей сборки и просит чанки вида /_next/static/chunks/...-<hash>.js,
// которых в новой .next уже нет - Next.js падает с ChunkLoadError, и страница
// остаётся белой, пока пользователь сам не нажмёт F5. Чанки не перезаписываются
// (имя файла содержит хеш сборки), поэтому единственное лекарство - перезагрузить
// страницу: свежий HTML ссылается уже на чанки текущей сборки.
// Перезагружаемся ОДИН раз за окно (флаг в sessionStorage со сроком), чтобы
// при повторной ошибке не уйти в цикл перезагрузок.
const RELOAD_FLAG = "anon.chunk-reload-at";
const RELOAD_COOLDOWN_MS = 60_000;

function reloadOnce() {
  try {
    const at = Number(window.sessionStorage.getItem(RELOAD_FLAG) || 0);
    if (Date.now() - at < RELOAD_COOLDOWN_MS) return; // уже перезагружались недавно
    window.sessionStorage.setItem(RELOAD_FLAG, String(Date.now()));
  } catch {
    /* sessionStorage недоступен (приватный режим) - перезагрузимся один раз */
  }
  window.location.reload();
}

function isChunkFailure(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  const url = (el?.getAttribute("src") || el?.getAttribute("href") || "").toString();
  return el?.tagName === "SCRIPT" && url.includes("/_next/static/chunks/");
}

function isChunkRejection(reason: unknown): boolean {
  const name = (reason as { name?: string } | null)?.name || "";
  const message = (reason as { message?: string } | null)?.message || String(reason);
  return name === "ChunkLoadError" || message.includes("ChunkLoadError");
}

export default function ChunkErrorReload() {
  useEffect(() => {
    const onError = (e: Event) => {
      if (isChunkFailure(e.target)) reloadOnce();
    };
    const onRejection = (e: PromiseRejectionEvent) => {
      if (isChunkRejection(e.reason)) {
        e.preventDefault();
        reloadOnce();
      }
    };
    // capture=true: ошибки загрузки <script> не всплывают, ловим на фазе захвата
    window.addEventListener("error", onError, true);
    window.addEventListener("unhandledrejection", onRejection);
    return () => {
      window.removeEventListener("error", onError, true);
      window.removeEventListener("unhandledrejection", onRejection);
    };
  }, []);

  return null;
}
