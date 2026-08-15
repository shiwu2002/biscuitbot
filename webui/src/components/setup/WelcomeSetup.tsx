import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Eye, EyeOff, Loader2, Rocket } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { WechatBridge } from "@/components/wechat/WechatBridge";
import { completeSetup, fetchSettings } from "@/lib/api";
import type { SettingsPayload } from "@/lib/types";
import { providerBrand } from "@/lib/provider-brand";

const SETUP_SKIP_KEY = "biscuitbot-webui.setup-skipped";

/**
 * Pre-filled recommended model per provider (the fallback when unset).
 * 随各服务商最新模型轮换更新（2026-08）：
 * - deepseek: deepseek-chat 已于 2026-07-24 退役 → deepseek-v4-flash
 * - openai:   gpt-4o-mini 已退役 → gpt-5.6-terra（-mini 档位继任者）
 * - anthropic: claude-sonnet-4-5 → claude-sonnet-5
 * - dashscope: qwen-plus（旧版）→ qwen3.6-plus（均衡新默认）
 * - moonshot:  moonshot-v1-8k 将于 2026-08-31 停服 → kimi-k3
 * - zhipu:    glm-4-flash → glm-4.5-flash（免费）
 * - stepfun:  step-1-8k 已于 2026-07-08 下线 → step-3.7-flash
 * - google:   gemini-2.0-flash 已于 2026-03 退役 → gemini-2.5-flash（稳定版）
 */
const DEFAULT_MODELS: Record<string, string> = {
  deepseek: "deepseek/deepseek-v4-flash",
  openai: "openai/gpt-5.6-terra",
  anthropic: "anthropic/claude-sonnet-5",
  dashscope: "dashscope/qwen3.6-plus",
  moonshot: "moonshot/kimi-k3",
  zhipu: "zhipu/glm-4.5-flash",
  stepfun: "stepfun/step-3.7-flash",
  groq: "groq/llama-3.3-70b-versatile",
  google: "google/gemini-2.5-flash",
  gemini: "google/gemini-2.5-flash",
};

export function hasSkippedSetup(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(SETUP_SKIP_KEY) === "1";
  } catch {
    return false;
  }
}

export function skipSetup(): void {
  try {
    window.localStorage.setItem(SETUP_SKIP_KEY, "1");
  } catch {
    // ignore storage errors (private mode, etc.)
  }
}

export function clearSetupSkip(): void {
  try {
    window.localStorage.removeItem(SETUP_SKIP_KEY);
  } catch {
    // ignore storage errors (private mode, etc.)
  }
}

type ProviderRow = SettingsPayload["providers"][number];

function ProviderLogo({ provider }: { provider: string }) {
  const brand = providerBrand(provider);
  if (brand) {
    return (
      <span
        className="grid h-10 w-10 shrink-0 place-items-center rounded-[14px] text-[11px] font-semibold text-white shadow-[inset_0_0_0_1px_rgba(255,255,255,0.18)]"
        style={{ backgroundColor: brand.color }}
        aria-hidden
      >
        {brand.initials}
      </span>
    );
  }
  return (
    <span className="grid h-10 w-10 shrink-0 place-items-center rounded-2xl bg-muted text-foreground/82">
      <span className="text-[11px] font-semibold uppercase">
        {provider.slice(0, 2)}
      </span>
    </span>
  );
}

function ProviderOption({
  provider,
  selected,
  onSelect,
}: {
  provider: ProviderRow;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "flex min-w-0 items-center gap-3 rounded-2xl border px-4 py-3 text-left transition-colors",
        selected
          ? "border-foreground/35 bg-muted/35"
          : "border-border/50 bg-background/55 hover:border-border hover:bg-muted/25",
      )}
    >
      <ProviderLogo provider={provider.name} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[14px] font-semibold leading-5 text-foreground">
          {provider.label}
        </span>
        <span className="block truncate text-[12px] text-muted-foreground">
          {provider.default_api_base || provider.name}
        </span>
      </span>
      {selected ? (
        <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-foreground text-background">
          <span className="h-2 w-2 rounded-full bg-background" aria-hidden />
        </span>
      ) : (
        <span className="h-5 w-5 shrink-0 rounded-full border border-border" aria-hidden />
      )}
    </button>
  );
}

export function WelcomeSetup({
  token,
  onDone,
}: {
  token: string;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, options?: Record<string, unknown>) =>
    t(key, { ...(options ?? {}), defaultValue: fallback });

  const [settings, setSettings] = useState<SettingsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [selected, setSelected] = useState<string | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [apiBase, setApiBase] = useState("");
  const [model, setModel] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchSettings(token)
      .then((payload) => {
        if (cancelled) return;
        setSettings(payload);
        setLoadError(null);
      })
      .catch((e) => {
        if (cancelled) return;
        setLoadError((e as Error).message);
        setSettings(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const providers = useMemo(() => {
    if (!settings) return [];
    return settings.providers.filter(
      (provider) =>
        provider.auth_type !== "oauth" &&
        provider.model_selectable !== false &&
        (provider.api_key_required ?? true),
    );
  }, [settings]);

  const handleSelect = (provider: ProviderRow) => {
    setSelected(provider.name);
    setSubmitError(null);
    setApiBase(provider.default_api_base ?? "");
    const recommended = DEFAULT_MODELS[provider.name] ?? "";
    setModel(recommended);
    if (provider.name !== selected) setApiKey("");
  };

  const canSubmit =
    !!selected &&
    apiKey.trim().length > 0 &&
    !submitting &&
    !loading;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selected || !apiKey.trim()) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      await completeSetup(token, {
        provider: selected,
        apiKey: apiKey.trim(),
        apiBase: apiBase.trim() || undefined,
        model: model.trim() || undefined,
      });
      onDone();
    } catch (err) {
      setSubmitError((err as Error).message);
      setSubmitting(false);
    }
  };

  const handleSkip = () => {
    skipSetup();
    onDone();
  };

  return (
    <div className="flex h-full w-full items-center justify-center overflow-y-auto px-6 py-8">
      <div className="w-full max-w-lg">
        <div className="mb-6 flex flex-col items-center gap-2 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-foreground text-background">
            <Rocket className="h-7 w-7" aria-hidden />
          </div>
          <p className="mt-2 text-xl font-semibold leading-6">
            {tx("setup.welcome.title", "欢迎使用 biscuitbot")}
          </p>
          <p className="max-w-sm text-[13px] leading-5 text-muted-foreground">
            {tx(
              "setup.welcome.subtitle",
              "选择一个 AI 服务商并粘贴 API 密钥，即可开始对话。密钥仅保存在本机配置中。",
            )}
          </p>
        </div>

        {loading ? (
          <div className="flex h-40 items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" aria-hidden />
          </div>
        ) : loadError ? (
          <div className="space-y-4">
            <p className="rounded-xl border border-border/50 bg-muted/20 px-4 py-3 text-center text-[13px] text-destructive">
              {tx("setup.welcome.loadError", "无法加载服务商列表：{{message}}", {
                message: loadError,
              })}
            </p>
            <Button
              type="button"
              variant="outline"
              className="w-full"
              onClick={() => {
                setLoading(true);
                setLoadError(null);
                fetchSettings(token)
                  .then((payload) => setSettings(payload))
                  .catch((e) => setLoadError((e as Error).message))
                  .finally(() => setLoading(false));
              }}
            >
              {tx("setup.welcome.retry", "重试")}
            </Button>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-5">
            <div>
              <span className="mb-2 block text-[12px] font-medium text-muted-foreground">
                {tx("setup.welcome.chooseProvider", "选择服务商")}
              </span>
              <div className="grid max-h-64 grid-cols-1 gap-2 overflow-y-auto pr-1">
                {providers.map((provider) => (
                  <ProviderOption
                    key={provider.name}
                    provider={provider}
                    selected={selected === provider.name}
                    onSelect={() => handleSelect(provider)}
                  />
                ))}
                {providers.length === 0 ? (
                  <p className="py-4 text-center text-[13px] text-muted-foreground">
                    {tx(
                      "setup.welcome.noProviders",
                      "暂无可配置的服务商，请稍后在设置中手动配置。",
                    )}
                  </p>
                ) : null}
              </div>
            </div>

            {selected ? (
              <div className="space-y-4">
                <label className="block space-y-1.5">
                  <span className="text-[12px] font-medium text-muted-foreground">
                    {tx("setup.welcome.apiKey", "API 密钥")}
                  </span>
                  <div className="relative">
                    <Input
                      type={showKey ? "text" : "password"}
                      value={apiKey}
                      onChange={(e) => {
                        setApiKey(e.target.value);
                        setSubmitError(null);
                      }}
                      placeholder={tx(
                        "setup.welcome.apiKeyPlaceholder",
                        "粘贴你的 API 密钥",
                      )}
                      className="pr-10"
                      autoFocus
                    />
                    <button
                      type="button"
                      onClick={() => setShowKey((v) => !v)}
                      aria-label={
                        showKey
                          ? tx("setup.welcome.hideKey", "隐藏密钥")
                          : tx("setup.welcome.showKey", "显示密钥")
                      }
                      className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                    >
                      {showKey ? (
                        <EyeOff className="h-4 w-4" aria-hidden />
                      ) : (
                        <Eye className="h-4 w-4" aria-hidden />
                      )}
                    </button>
                  </div>
                </label>

                {apiBase ? (
                  <label className="block space-y-1.5">
                    <span className="text-[12px] font-medium text-muted-foreground">
                      {tx("setup.welcome.apiBase", "API 地址（可选）")}
                    </span>
                    <Input
                      type="text"
                      value={apiBase}
                      onChange={(e) => setApiBase(e.target.value)}
                      placeholder="https://…"
                      className="text-[13px]"
                    />
                  </label>
                ) : null}

                <label className="block space-y-1.5">
                  <span className="text-[12px] font-medium text-muted-foreground">
                    {tx("setup.welcome.model", "模型（可选）")}
                  </span>
                  <Input
                    type="text"
                    value={model}
                    onChange={(e) => setModel(e.target.value)}
                    placeholder={tx(
                      "setup.welcome.modelPlaceholder",
                      "留空则使用默认模型",
                    )}
                    className="text-[13px]"
                  />
                </label>
              </div>
            ) : null}

            <WechatBridge token={token} />

            {submitError ? (
              <p className="rounded-xl border border-border/50 bg-muted/20 px-4 py-3 text-center text-[13px] text-destructive">
                {tx("setup.welcome.submitError", "保存失败：{{message}}", {
                  message: submitError,
                })}
              </p>
            ) : null}

            <div className="space-y-2">
              <Button
                type="submit"
                className="w-full"
                size="lg"
                disabled={!canSubmit}
              >
                {submitting ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
                    {tx("setup.welcome.saving", "正在保存…")}
                  </>
                ) : (
                  tx("setup.welcome.start", "开始使用")
                )}
              </Button>
              <Button
                type="button"
                variant="ghost"
                className="w-full text-muted-foreground"
                onClick={handleSkip}
                disabled={submitting}
              >
                {tx("setup.welcome.skip", "稍后在设置中配置")}
              </Button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
