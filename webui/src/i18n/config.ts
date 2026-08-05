export const LOCALE_STORAGE_KEY = "biscuitbot.locale";

export const supportedLocales = [
  { code: "zh-CN", label: "Chinese (Simplified)", nativeLabel: "简体中文" },
  { code: "zh-TW", label: "Chinese (Traditional)", nativeLabel: "繁體中文" },
] as const;

export type SupportedLocale = (typeof supportedLocales)[number]["code"];

export const defaultLocale: SupportedLocale = "zh-CN";
export const fallbackLocale: SupportedLocale = "zh-CN";

export function normalizeLocale(
  input: string | null | undefined,
): SupportedLocale {
  if (!input) return defaultLocale;
  const trimmed = input.trim();
  if (!trimmed) return defaultLocale;

  const exact = supportedLocales.find((locale) => locale.code === trimmed);
  if (exact) return exact.code;

  const lower = trimmed.toLowerCase();
  if (lower === "zh" || lower.startsWith("zh-cn") || lower.startsWith("zh-sg")) {
    return "zh-CN";
  }
  if (
    lower.startsWith("zh-tw") ||
    lower.startsWith("zh-hk") ||
    lower.startsWith("zh-mo") ||
    lower.startsWith("zh-hant")
  ) {
    return "zh-TW";
  }

  // 其他语言统一回退到简体中文
  return defaultLocale;
}

export function readStoredLocale(): SupportedLocale | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(LOCALE_STORAGE_KEY);
    return raw ? normalizeLocale(raw) : null;
  } catch {
    return null;
  }
}

export function detectNavigatorLocale(): SupportedLocale {
  if (typeof navigator === "undefined") return defaultLocale;
  const candidates = [
    ...(navigator.languages ?? []),
    navigator.language,
  ].filter(Boolean);
  for (const locale of candidates) {
    const normalized = normalizeLocale(locale);
    if (normalized) return normalized;
  }
  return defaultLocale;
}

export function resolveInitialLocale(): SupportedLocale {
  return readStoredLocale() ?? defaultLocale;
}

export function persistLocale(locale: SupportedLocale): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, locale);
  } catch {
    // 忽略存储错误
  }
}

export function applyDocumentLocale(locale: SupportedLocale): void {
  if (typeof document === "undefined") return;
  document.documentElement.lang = locale;
}

export function localeOption(locale: SupportedLocale) {
  return supportedLocales.find((entry) => entry.code === locale) ?? supportedLocales[0];
}
