import { useCallback, useEffect, useState } from "react";
import {
  ChevronLeft,
  ExternalLink,
  Globe,
  Loader2,
  MonitorSmartphone,
  RefreshCw,
  Square,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  fetchBrowserStatus,
  startBrowserService,
  stopBrowserService,
} from "@/lib/api";
import type { BrowserStatusPayload } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

/** 桌面壳（withGlobalTauri）暴露的 invoke；浏览器宿主下为 null。 */
function tauriInvoke(): ((cmd: string, args?: unknown) => Promise<unknown>) | null {
  if (typeof window === "undefined") return null;
  return window.__TAURI__?.core?.invoke ?? null;
}

const MODE_STYLES: Record<string, string> = {
  embedded: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  external: "bg-sky-500/15 text-sky-600 dark:text-sky-400",
  managed: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  stopped: "bg-muted text-muted-foreground",
};

/**
 * 内置浏览器驾驶舱视图（侧边栏「浏览器」入口）。
 *
 * 桌面端：浏览器本体是 Tauri 壳的原生 WebView2 窗口（独立 profile + CDP
 * 调试端口），本页负责状态展示与窗口的打开/聚焦/关闭；智能体自动化通过
 * playwright MCP 附着同一 CDP 端口，与用户同屏操作。
 * 非桌面端（浏览器访问 WebUI / 服务器部署）：退化为托管常驻实例
 * （REST /api/browser/start|stop）。
 */
export function BrowserView({
  onBackToChat,
  hostChromeInset,
}: {
  onBackToChat: () => void;
  hostChromeInset?: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const { client, token } = useClient();

  const [status, setStatus] = useState<BrowserStatusPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [urlDraft, setUrlDraft] = useState("");
  const [lastActivityUrl, setLastActivityUrl] = useState<string | null>(null);

  const isDesktop = tauriInvoke() !== null;

  const refresh = useCallback(async () => {
    try {
      const payload = await fetchBrowserStatus(token);
      setStatus(payload);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // WS 推送的服务状态 / 智能体浏览器活动（本页打开时实时更新）
  useEffect(() => {
    const offStatus = client.onBrowserStatus((payload) => setStatus(payload));
    const offActivity = client.onBrowserActivity(({ url }) => {
      if (url) setLastActivityUrl(url);
    });
    return () => {
      offStatus();
      offActivity();
    };
  }, [client]);

  const openWindow = useCallback(
    async (url?: string) => {
      setBusy(true);
      try {
        const invoke = tauriInvoke();
        if (invoke) {
          await invoke("open_browser_window", { url: url ?? null });
        } else {
          const payload = await startBrowserService(token, url);
          setStatus(payload);
        }
        setError(null);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(false);
        void refresh();
      }
    },
    [refresh, token],
  );

  const closeWindow = useCallback(async () => {
    setBusy(true);
    try {
      const invoke = tauriInvoke();
      if (invoke) {
        await invoke("close_browser_window");
      } else {
        const payload = await stopBrowserService(token);
        setStatus(payload);
      }
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      void refresh();
    }
  }, [refresh, token]);

  const mode = status?.mode ?? "stopped";
  const running = status?.running ?? false;

  return (
    <div
      className={cn(
        "flex min-h-0 flex-1 flex-col overflow-hidden",
        hostChromeInset && "md:mt-11",
      )}
    >
      <div className="flex items-center gap-2 px-4 pb-3 pt-4">
        <Button variant="ghost" size="icon" aria-label={tx("browser.back", "返回会话")} onClick={onBackToChat}>
          <ChevronLeft className="h-4 w-4" />
        </Button>
        <Globe className="h-5 w-5 text-primary" aria-hidden />
        <div className="min-w-0">
          <h1 className="truncate text-[15px] font-medium">
            {tx("browser.title", "内置浏览器")}
          </h1>
          <p className="truncate text-xs text-muted-foreground">
            {tx(
              "browser.subtitle",
              "应用内访问网址；智能体自动化默认使用同一浏览器实例",
            )}
          </p>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-6">
        {/* 服务状态卡片 */}
        <div className="rounded-xl border border-border/60 bg-card/60 p-4">
          <div className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2">
              <MonitorSmartphone className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
              <span className="text-sm font-medium">
                {tx("browser.statusTitle", "浏览器服务")}
              </span>
              <span
                className={cn(
                  "rounded-full px-2 py-0.5 text-[11px] font-medium",
                  MODE_STYLES[mode] ?? MODE_STYLES.stopped,
                )}
              >
                {tx(`browser.mode.${mode}`, mode)}
              </span>
            </div>
            <Button
              variant="ghost"
              size="icon"
              aria-label={tx("browser.refresh", "刷新状态")}
              onClick={() => void refresh()}
              disabled={loading}
            >
              {loading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <RefreshCw className="h-4 w-4" />
              )}
            </Button>
          </div>
          <dl className="mt-3 grid gap-x-6 gap-y-1.5 text-[13px] sm:grid-cols-[auto_1fr]">
            <dt className="text-muted-foreground">{tx("browser.state", "状态")}</dt>
            <dd>{running ? tx("browser.stateRunning", "运行中") : tx("browser.stateStopped", "未运行")}</dd>
            <dt className="text-muted-foreground">{tx("browser.endpoint", "CDP 端点")}</dt>
            <dd className="break-all font-mono text-xs">{status?.endpoint ?? "—"}</dd>
            <dt className="text-muted-foreground">{tx("browser.currentUrl", "当前页面")}</dt>
            <dd className="break-all">{lastActivityUrl ?? status?.url ?? "—"}</dd>
          </dl>
          {running && (
            <p className="mt-2 text-xs text-muted-foreground">
              {tx(
                "browser.agentSharing",
                "智能体自动化正共享此浏览器实例，操作实时可见。",
              )}
            </p>
          )}
        </div>

        {/* 地址栏：打开 / 导航 */}
        <div className="mt-4 rounded-xl border border-border/60 bg-card/60 p-4">
          <label className="text-sm font-medium" htmlFor="browser-url-input">
            {tx("browser.openUrl", "打开网址")}
          </label>
          <div className="mt-2 flex gap-2">
            <Input
              id="browser-url-input"
              value={urlDraft}
              placeholder={tx("browser.urlPlaceholder", "https://example.com")}
              onChange={(event) => setUrlDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && urlDraft.trim()) {
                  void openWindow(urlDraft.trim());
                }
              }}
              className="h-9 flex-1 rounded-full text-[13px]"
            />
            <Button
              size="sm"
              className="h-9 rounded-full"
              disabled={busy || !urlDraft.trim()}
              onClick={() => void openWindow(urlDraft.trim())}
            >
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <ExternalLink className="h-4 w-4" />}
              {tx("browser.openAction", "打开")}
            </Button>
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            {isDesktop
              ? tx(
                  "browser.desktopHint",
                  "将在应用内嵌浏览器窗口中打开（独立于主界面，登录态持久保存）。",
                )
              : tx(
                  "browser.remoteHint",
                  "当前为浏览器访问模式：将启动/复用托管常驻浏览器实例。",
                )}
          </p>
        </div>

        {/* 窗口控制 */}
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            className="rounded-full"
            disabled={busy}
            onClick={() => void openWindow()}
          >
            {tx("browser.focusWindow", "打开 / 聚焦浏览器窗口")}
          </Button>
          <Button
            variant="outline"
            className="rounded-full"
            disabled={busy || !running || mode === "external"}
            onClick={() => void closeWindow()}
          >
            <Square className="h-3.5 w-3.5" />
            {tx("browser.closeWindow", "关闭")}
          </Button>
          {mode === "external" && (
            <span className="text-xs text-muted-foreground">
              {tx(
                "browser.externalNote",
                "正在复用你自己启动的浏览器，不会替你关闭。",
              )}
            </span>
          )}
        </div>

        {error && (
          <p className="mt-3 rounded-lg bg-destructive/10 px-3 py-2 text-xs text-destructive">
            {error}
          </p>
        )}
      </div>
    </div>
  );
}
