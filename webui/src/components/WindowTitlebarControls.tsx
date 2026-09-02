import { useEffect, useState, type ReactNode } from "react";
import { Copy, Minus, Square, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

/** 是否跑在 Tauri 桌面壳里（浏览器打开 Gateway 时为 false，渲染空壳）。 */
function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/**
 * 桌面端自定义标题栏的最小化/最大化/关闭按钮组。
 *
 * 仅在 Tauri 壳内渲染；浏览器环境返回 null，绝不调用 @tauri-apps/api。
 * 关闭按钮走 `close()` → 触发 Rust 侧 `CloseRequested` → `prevent_close` +
 * 隐藏到托盘，与原生标题栏 X 行为一致。
 */
export function WindowTitlebarControls() {
  const { t } = useTranslation();
  const [maximized, setMaximized] = useState(false);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!isTauriRuntime()) return;
    let disposed = false;
    void (async () => {
      const { getCurrentWindow } = await import("@tauri-apps/api/window");
      if (disposed) return;
      const win = getCurrentWindow();
      const sync = async () => {
        try {
          if (!disposed) setMaximized(await win.isMaximized());
        } catch {
          // ignore: 窗口被关闭/销毁时读出状态可能抛错
        }
      };
      await sync();
      const unlisten = await win.onResized(() => void sync());
      setReady(true);
      if (disposed) unlisten();
    })();
    return () => {
      disposed = true;
    };
  }, []);

  if (!isTauriRuntime() || !ready) return null;

  return (
    <div className="host-no-drag flex h-11 items-stretch text-foreground/80">
      <TitlebarButton
        ariaLabel={t("titlebar.minimize")}
        onClick={async () => {
          const { getCurrentWindow } = await import("@tauri-apps/api/window");
          await getCurrentWindow().minimize();
        }}
      >
        <Minus className="h-4 w-4" />
      </TitlebarButton>
      <TitlebarButton
        ariaLabel={maximized ? t("titlebar.restore") : t("titlebar.maximize")}
        onClick={async () => {
          const { getCurrentWindow } = await import("@tauri-apps/api/window");
          await getCurrentWindow().toggleMaximize();
        }}
      >
        {maximized ? (
          <Copy className="h-3.5 w-3.5" />
        ) : (
          <Square className="h-3.5 w-3.5" />
        )}
      </TitlebarButton>
      <TitlebarButton
        ariaLabel={t("titlebar.close")}
        onClick={async () => {
          const { getCurrentWindow } = await import("@tauri-apps/api/window");
          await getCurrentWindow().close();
        }}
        isClose
      >
        <X className="h-4 w-4" />
      </TitlebarButton>
    </div>
  );
}

function TitlebarButton({
  ariaLabel,
  onClick,
  isClose = false,
  children,
}: {
  ariaLabel: string;
  onClick: () => void;
  isClose?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={ariaLabel}
      title={ariaLabel}
      onClick={onClick}
      className={cn(
        "flex w-11 items-center justify-center outline-none transition-colors",
        isClose
          ? "hover:bg-red-500 hover:text-white focus-visible:bg-red-500 focus-visible:text-white"
          : "hover:bg-accent/50 focus-visible:bg-accent/50",
      )}
    >
      {children}
    </button>
  );
}
