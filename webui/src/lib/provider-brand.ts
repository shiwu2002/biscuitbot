export interface ProviderBrand {
  logoUrl: string;
  logoUrls: string[];
  color: string;
  initials: string;
}

function officialFaviconUrl(domain: string): string {
  return `https://${domain}/favicon.ico`;
}

function duckDuckGoFaviconUrl(domain: string): string {
  return `https://icons.duckduckgo.com/ip3/${encodeURIComponent(domain)}.ico`;
}

function googleFaviconUrl(domain: string): string {
  return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=64`;
}

export function faviconUrls(domain: string): string[] {
  const faviconDomain = faviconDomainFromValue(domain);
  return [
    officialFaviconUrl(faviconDomain),
    duckDuckGoFaviconUrl(faviconDomain),
    googleFaviconUrl(domain),
  ];
}

function brand(
  domain: string,
  color: string,
  initials: string,
  logoOverrides: string[] = [],
): ProviderBrand {
  const logoUrls = [...logoOverrides];
  faviconUrls(domain).forEach((url) => addUniqueLogoUrl(logoUrls, url));
  return {
    logoUrl: logoUrls[0],
    logoUrls,
    color,
    initials,
  };
}

function addUniqueLogoUrl(urls: string[], url: string | null | undefined): void {
  const value = url?.trim();
  if (value && !urls.includes(value)) urls.push(value);
}

function domainFromLogoUrl(url: string): string | null {
  if (url.startsWith("/")) return null;
  try {
    const parsed = new URL(url);
    if (!/^https?:$/.test(parsed.protocol)) return null;
    const host = parsed.hostname.toLowerCase();
    if (host === "www.google.com" || host === "google.com") {
      return parsed.searchParams.get("domain");
    }
    if (host === "icons.duckduckgo.com") {
      const match = parsed.pathname.match(/^\/ip3\/(.+)\.ico$/);
      return match ? decodeURIComponent(match[1]) : null;
    }
    return host.replace(/^www\./, "");
  } catch {
    return null;
  }
}

function faviconDomainFromValue(value: string): string {
  const host = value.split("/")[0]?.trim();
  return host || value;
}

export function logoFallbackUrls(logoUrl: string | null | undefined): string[] {
  const value = logoUrl?.trim();
  if (!value) return [];
  if (value.startsWith("/")) return [value];

  const urls: string[] = [];
  const domain = domainFromLogoUrl(value);
  const isFaviconProxy = /^(https?:\/\/)?(www\.google\.com|google\.com|icons\.duckduckgo\.com)\//i.test(value);
  if (domain && isFaviconProxy) {
    addUniqueLogoUrl(urls, value);
    faviconUrls(domain).forEach((url) => addUniqueLogoUrl(urls, url));
    return urls;
  }
  addUniqueLogoUrl(urls, value);
  if (domain) faviconUrls(domain).forEach((url) => addUniqueLogoUrl(urls, url));
  return urls;
}

export const PROVIDER_BRAND_ALIASES: Record<string, string> = {};

export const PROVIDER_LABEL_ALIASES: Record<string, string> = {};

const PROVIDER_BRANDS: Record<string, ProviderBrand> = {
  anthropic: brand("anthropic.com", "#D97757", "A"),
  custom: brand("", "#6B7280", "C"),
  dashscope: brand("dashscope.aliyun.com", "#FF6A00", "DS"),
  deepseek: brand("deepseek.com", "#4D6BFE", "D"),
  moonshot: brand("moonshot.cn", "#6B57D9", "K"),
  ollama: brand("ollama.com", "#111827", "O"),
  openai: brand("openai.com", "#111827", "AI"),
  stepfun: brand("stepfun.com", "#10B981", "S"),
  volcengine: brand("volcengine.com", "#3370FF", "V"),
  zhipu: brand("zhipuai.cn", "#0A87C6", "Z"),
};

export function providerBrand(provider: string | null | undefined): ProviderBrand | null {
  if (!provider) return null;
  const key = PROVIDER_BRAND_ALIASES[provider] ?? provider;
  return PROVIDER_BRANDS[key] ?? null;
}

export function providerDisplayLabel(
  providers: Array<{ name: string; label: string }>,
  value: string | null | undefined,
): string {
  if (!value) return "";
  return providers.find((provider) => provider.name === value)?.label
    ?? PROVIDER_LABEL_ALIASES[value]
    ?? value;
}

export function inferProviderFromModelName(modelName: string | null | undefined): string | null {
  const normalized = (modelName ?? "").trim().toLowerCase();
  if (!normalized) return null;
  const prefix = normalized.split(/[/:]/)[0];
  if (providerBrand(prefix)) return prefix;
  if (/claude|anthropic/.test(normalized)) return "anthropic";
  if (/gpt-|^o\d|chatgpt|openai/.test(normalized)) return "openai";
  if (/deepseek/.test(normalized)) return "deepseek";
  if (/qwen|dashscope|tongyi/.test(normalized)) return "dashscope";
  if (/glm|zhipu|zai|chatglm/.test(normalized)) return "zhipu";
  if (/moonshot|kimi/.test(normalized)) return "moonshot";
  if (/stepfun|step-|^\s*step\s/.test(normalized)) return "stepfun";
  if (/ollama|llama/.test(normalized)) return "ollama";
  if (/doubao|seedream|volcengine|ark/.test(normalized)) return "volcengine";
  return null;
}
