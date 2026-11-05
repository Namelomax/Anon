"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { loginUrl } from "@/lib/auth-navigation";

export function useLoginRedirect(status: "loading" | "authenticated" | "unauthenticated") {
  const router = useRouter();
  useEffect(() => {
    if (status === "unauthenticated") {
      router.replace(loginUrl(window.location.pathname + window.location.search + window.location.hash));
    }
  }, [router, status]);
}
