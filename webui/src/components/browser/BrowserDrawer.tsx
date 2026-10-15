import { useCallback, useEffect, useRef, useState } from "react";
import { ExternalLink, Globe, Loader2, RefreshCw, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetchBrowserStatus, startBrowserService } from "@/lib/api";
import type { BrowserStatusPayload } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

/** 桌面壳（withGlobalTauri）暴露的 invoke；浏览器宿主下为 null。 */
function tauriInvoke(): ((cmd: string, args?: unknown) => Promise<unknown>) | null {
  if (typeof window === "undefined") return null;
  return window.__TAURI__?.core?.invoke ?? null;
}

interface DrawerRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** 抽屉宽度（逻辑像素；ThreadShell 容器据此收窄，两者必须一致）。 */
export const BROWSER_DRAWER_WIDTH = 480;

/**
 * 对话内右侧「内置浏览器」抽屉。
 *
 * 桌面端：顶部工具条由 React 渲染（地址栏/刷新/关闭），内容区是 Tauri 壳在
 * 同一主窗口里创建的**原生 WebView2 子 webview**（独立 profile + CDP 调试
 * 端口）——内容区 div 只负责占位，原生 webview 精确覆盖其矩形；智能体经
 * playwright CDP 附着后操作直接发生在这个可见区域，同屏实时。
 * 非桌面端：不做抽屉（智能体走托管实例）。
 */
export function BrowserDrawer({
  open,
  initialUrl,
  hostChromeActive,
  onClose,
}: {
  /** 抽屉是否展开（由 App 控制；仅 chat 视图渲染）。 */
  open: boolean;
  /** 打开抽屉时要求浏览器导航到的地址（browser_request 自动唤起场景）。 */
  initialUrl?: string | null;
  hostChromeActive?: boolean;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const { client, token } = useClient();

  const contentRef = useRef<HTMLDivElement>(null);
  const [rect, setRect] = useState<DrawerRect | null>(null);
  const [status, setStatus] = useState<BrowserStatusPayload | null>(null);
  const [currentUrl, setCurrentUrl] = useState<string>("");
  const [urlDraft, setUrlDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const invoke = tauriInvoke();

  // -- 服务状态 & 智能体活动跟踪 -------------------------------------------
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    fetchBrowserStatus(token)
      .then((payload) => {
        if (!cancelled) setStatus(payload);
      })
      .catch(() => {});
    const offStatus = client.onBrowserStatus((payload) => setStatus(payload));
    const offActivity = client.onBrowserActivity(({ url }) => {
      if (url) setCurrentUrl(url);
    });
    return () => {
      cancelled = true;
      offStatus();
      offActivity();
    };
  }, [client, open, token]);

  // -- 内容区矩形测量（原生 webview 需要精确覆盖这块区域） ------------------
  useEffect(() => {
    const el = contentRef.current;
    if (!el || !open) return;
    const measure = () => {
      const box = el.getBoundingClientRect();
      setRect({
        x: Math.round(box.left),
        y: Math.round(box.top),
        width: Math.round(box.width),
        height: Math.round(box.height),
      });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [open]);

  // -- 原生 webview 生命周期 ------------------------------------------------
  // 首次打开：open_browser_drawer（携带 initialUrl）；此后 rect 变化只同步
  // bounds（update_browser_drawer），避免重复导航丢失用户浏览状态。
  const createdRef = useRef(false);
  useEffect(() => {
    if (!open || !rect || !invoke) return;
    if (rect.width < 50 || rect.height < 50) return;
    if (!createdRef.current) {
      createdRef.current = true;
      void invoke("open_browser_drawer", {
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
        url: initialUrl ?? null,
      }).catch((err: unknown) => {
        createdRef.current = false;
        console.warn("open_browser_drawer failed", err);
      });
    } else {
      void invoke("update_browser_drawer", {
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
      }).catch(() => {});
    }
  }, [invoke, open, rect, initialUrl]);

  // open 翻转为 false（抽屉收起）→ 关掉原生 webview。
  useEffect(() => {
    if (open || !invoke) return;
    createdRef.current = false;
    void invoke("close_browser_drawer").catch(() => {});
  }, [invoke, open]);

  // 组件卸载（切视图等）兜底关闭。
  useEffect(() => {
    return () => {
      const cleanup = tauriInvoke();
      void cleanup?.("close_browser_drawer").catch(() => {});
    };
  }, []);

  // -- 导航动作（经 gateway CDP：附着 embedded 并导航） --------------------
  const navigate = useCallback(
    async (raw: string) => {
      const target = raw.trim();
      if (!target) return;
      setBusy(true);
      try {
        const payload = await startBrowserService(token, target);
        setStatus(payload);
        setCurrentUrl(target);
        setUrlDraft("");
        setError(null);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [token],
  );

  const refreshCurrent = useCallback(() => {
    const target = currentUrl || status?.url || "";
    if (target && target !== "about:blank") void navigate(target);
  }, [currentUrl, navigate, status?.url]);

  const running = status?.running ?? false;
  const mode = status?.mode ?? "stopped";

  if (!open) return null;

  return (
    <aside
      className="absolute right-0 z-20 flex flex-col border-l border-border/60 bg-background shadow-2xl"
      style={{
        width: BROWSER_DRAWER_WIDTH,
        top: hostChromeActive ? 44 : 0,
        bottom: 0,
      }}
      data-testid="browser-drawer"
    >
      {/* 工具条（React 渲染；原生 webview 只覆盖下方内容区） */}
      <div className="flex items-center gap-2 border-b border-border/60 px-3 py-2">
        <Globe className="h-4 w-4 shrink-0 text-primary" aria-hidden />
        <Input
          value={urlDraft}
          placeholder={currentUrl || tx("browserDrawer.addressPlaceholder", "输入网址后回车")}
          aria-label={tx("browserDrawer.address", "浏览器地址栏")}
          className="h-8 flex-1 rounded-full text-[13px]"
          onChange={(event) => setUrlDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && urlDraft.trim()) void navigate(urlDraft.trim());
          }}
        />
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          aria-label={tx("browserDrawer.refresh", "刷新当前页面")}
          disabled={busy || !running}
          onClick={refreshCurrent}
        >
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          aria-label={tx("browserDrawer.close", "关闭浏览器抽屉")}
          onClick={onClose}
        >
          <X className="h-4 w-4" />
        </Button>
      </div>

      {/* 状态条 */}
      <div className="flex items-center gap-2 border-b border-border/60 bg-muted/30 px-3 py-1.5 text-[11px] text-muted-foreground">
        <span
          className={cn(
            "rounded-full px-1.5 py-0.5 font-medium",
            mode === "embedded" && "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
            mode === "external" && "bg-sky-500/15 text-sky-600 dark:text-sky-400",
            mode === "managed" && "bg-amber-500/15 text-amber-600 dark:text-amber-400",
            mode === "stopped" && "bg-muted text-muted-foreground",
          )}
        >
          {tx(`browser.mode.${mode}`, mode)}
        </span>
        <span className="min-w-0 flex-1 truncate" title={currentUrl || status?.url || ""}>
          {currentUrl || status?.url || tx("browserDrawer.idle", "尚未打开页面")}
        </span>
        {currentUrl && !currentUrl.startsWith("about:") ? (
          <ExternalLink className="h-3 w-3 shrink-0" aria-hidden />
        ) : null}
      </div>

      {/* 内容占位区：原生 WebView2 精确覆盖此矩形 */}
      <div
        ref={contentRef}
        className="relative min-h-0 flex-1 bg-card"
        aria-label={tx("browserDrawer.content", "浏览器内容区")}
      />

      {error ? (
        <p className="border-t border-destructive/30 bg-destructive/10 px-3 py-1.5 text-xs text-destructive">
          {error}
        </p>
      ) : null}
    </aside>
  );
}
