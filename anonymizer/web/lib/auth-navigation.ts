export function safeReturnPath(value: unknown): string {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//") || /[\\\x00-\x1f\x7f]/.test(value)) return "/";
  try {
    const url = new URL(value, "https://local.invalid");
    const path = decodeURIComponent(url.pathname).replace(/\/+$/, "").toLowerCase();
    if (url.origin !== "https://local.invalid" || path === "/login" || path === "/register") return "/";
    return value;
  } catch {
    return "/";
  }
}

export function loginUrl(path: unknown): string {
  return `/login?next=${encodeURIComponent(safeReturnPath(path))}`;
}
