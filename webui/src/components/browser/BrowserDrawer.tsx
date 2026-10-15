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
 *
 * 生命周期（占位优先）：抽屉打开且还没有目标网址时**不创建**原生 webview，
 * 内容区显示起始占位（避免空白 about:blank 原生页让用户以为界面无响应）；
 * 用户在地址栏提交网址或智能体请求浏览器（url prop）时才创建/导航。
 * 非桌面端：不做抽屉（智能体走托管实例）。
 */
export function BrowserDrawer({
  open,
  url,
  onNavigateUrl,
  hostChromeActive,
  onClose,
}: {
  /** 抽屉是否展开（由 App 控制；仅 chat 视图渲染）。 */
  open: boolean;
  /** 当前目标网址（单一数据源在 App：地址栏提交与智能体请求都写这里）。 */
  url: string | null;
  /** 用户在地址栏提交新网址时回调（App 更新 url prop）。 */
  onNavigateUrl: (url: string) => void;
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
  /** 原生 webview 是否已创建（决定内容区显示占位还是被原生页覆盖）。 */
  const [hasNative, setHasNative] = useState(false);
  /** 已导航到的网址（避免同址重复导航）。 */
  const lastNavRef = useRef<string | null>(null);

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
    const offActivity = client.onBrowserActivity(({ url: active }) => {
      if (active) setCurrentUrl(active);
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
  // 未创建且有目标网址 → 创建（open_browser_drawer 幂等：已存在则更新 bounds
  // 并按需导航）；已创建且 url 变化 → 再次调用同一命令完成导航；其余 rect
  // 变化只同步 bounds（update_browser_drawer）。
  useEffect(() => {
    if (!open || !rect || !invoke) return;
    if (rect.width < 50 || rect.height < 50) return;
    if (!hasNative && !url) return; // 占位优先：无网址不创建原生 webview
    const args = {
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
      url: url ?? null,
    };
    if (!hasNative) {
      void invoke("open_browser_drawer", args)
        .then(() => {
          lastNavRef.current = url ?? null;
          setHasNative(true);
        })
        .catch((err: unknown) => {
          console.warn("open_browser_drawer failed", err);
        });
    } else if (url && url !== lastNavRef.current) {
      void invoke("open_browser_drawer", args)
        .then(() => {
          lastNavRef.current = url;
        })
        .catch((err: unknown) => {
          console.warn("open_browser_drawer navigate failed", err);
        });
    } else {
      void invoke("update_browser_drawer", {
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
      }).catch(() => {});
    }
  }, [invoke, open, rect, url, hasNative]);

  // open 翻转为 false（抽屉收起）→ 关掉原生 webview 并复位。
  useEffect(() => {
    if (open || !invoke) return;
    setHasNative(false);
    lastNavRef.current = null;
    void invoke("close_browser_drawer").catch(() => {});
  }, [invoke, open]);

  // 组件卸载（切视图等）兜底关闭。
  useEffect(() => {
    return () => {
      const cleanup = tauriInvoke();
      void cleanup?.("close_browser_drawer").catch(() => {});
    };
  }, []);

  // -- 导航动作：Rust 命令负责原生导航；REST 仅确保 manager 附着（供智能体） --
  const navigate = useCallback(
    (raw: string) => {
      const target = raw.trim();
      if (!target) return;
      setBusy(true);
      try {
        onNavigateUrl(target);
        setCurrentUrl(target);
        setUrlDraft("");
        setError(null);
      } finally {
        setBusy(false);
      }
      // 附着失败不阻塞本地导航（智能体侧下次 ensure_started 会重试）。
      startBrowserService(token).catch(() => {});
    },
    [onNavigateUrl, token],
  );

  const refreshCurrent = useCallback(() => {
    const target = currentUrl || url || status?.url || "";
    if (target && target !== "about:blank") navigate(target);
  }, [currentUrl, navigate, status?.url, url]);

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
            if (event.key === "Enter" && urlDraft.trim()) navigate(urlDraft.trim());
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

      {/* 内容区：有原生 webview 时被其精确覆盖；否则显示起始占位 */}
      <div
        ref={contentRef}
        className="relative min-h-0 flex-1 bg-card"
        aria-label={tx("browserDrawer.content", "浏览器内容区")}
      >
        {!hasNative ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 px-8 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-[hsl(var(--cyber-glow)/0.12)] shadow-[0_0_18px_hsl(var(--cyber-glow-soft)/0.4)]">
              <Globe className="h-6 w-6 text-primary" aria-hidden />
            </div>
            <p className="text-sm font-medium text-foreground">
              {tx("browserDrawer.placeholderTitle", "内置浏览器")}
            </p>
            <p className="text-xs leading-5 text-muted-foreground">
              {tx(
                "browserDrawer.placeholderHint",
                "在上方地址栏输入网址开始浏览；智能体执行浏览器任务时，操作画面也会实时显示在这里。",
              )}
            </p>
          </div>
        ) : null}
      </div>

      {error ? (
        <p className="border-t border-destructive/30 bg-destructive/10 px-3 py-1.5 text-xs text-destructive">
          {error}
        </p>
      ) : null}
    </aside>
  );
}
