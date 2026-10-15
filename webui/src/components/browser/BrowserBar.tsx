import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, ExternalLink, Globe, RefreshCw, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** 控制栏高度（像素，供需要让位的布局使用）。 */
export const BROWSER_BAR_HEIGHT = 40;

/**
 * 主窗口里的「内置浏览器」紧凑控制栏。
 *
 * 浏览器本体是一个**独立的原生窗口**（见 `src/lib/browser-window.ts` 与本仓库
 * `src-tauri/src/lib.rs` 的 `open_browser_window`）：自带标题栏、可拖动/缩放/
 * 吸附，因此能和主窗口并排同屏。这条控制栏只是遥控器——地址输入、前进后退、
 * 刷新、用系统浏览器打开、关闭窗口。
 *
 * 仅当浏览器窗口处于打开状态时渲染。
 */
export function BrowserBar({
  url,
  onNavigate,
  onBack,
  onForward,
  onReload,
  onOpenExternal,
  onClose,
}: {
  /** 浏览器窗口当前页面地址（由控制栏回填；随页面跳转更新）。 */
  url: string;
  onNavigate: (url: string) => void;
  onBack: () => void;
  onForward: () => void;
  onReload: () => void;
  onOpenExternal: () => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });

  const [draft, setDraft] = useState(url);
  /** 用户正在编辑时不要用外部 URL 覆盖输入框。 */
  const editingRef = useRef(false);

  useEffect(() => {
    if (!editingRef.current) setDraft(url);
  }, [url]);

  return (
    <div
      className="flex shrink-0 items-center gap-1 border-b border-border/60 bg-card/70 px-3 py-1.5"
      style={{ height: BROWSER_BAR_HEIGHT }}
      data-testid="browser-bar"
    >
      <Globe className="mr-1 h-4 w-4 shrink-0 text-primary" aria-hidden />
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7"
        aria-label={tx("browserBar.back", "后退")}
        title={tx("browserBar.back", "后退")}
        onClick={onBack}
      >
        <ArrowLeft className="h-3.5 w-3.5" />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7"
        aria-label={tx("browserBar.forward", "前进")}
        title={tx("browserBar.forward", "前进")}
        onClick={onForward}
      >
        <ArrowRight className="h-3.5 w-3.5" />
      </Button>
      <Input
        value={draft}
        placeholder={tx("browserBar.placeholder", "输入网址后回车")}
        aria-label={tx("browserBar.address", "浏览器地址栏")}
        className="h-7 flex-1 rounded-full text-[13px]"
        onChange={(event) => setDraft(event.target.value)}
        onFocus={() => {
          editingRef.current = true;
        }}
        onBlur={() => {
          editingRef.current = false;
          setDraft(url);
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter" && draft.trim()) {
            onNavigate(draft.trim());
            event.currentTarget.blur();
          }
        }}
      />
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7"
        aria-label={tx("browserBar.reload", "刷新")}
        title={tx("browserBar.reload", "刷新")}
        onClick={onReload}
      >
        <RefreshCw className="h-3.5 w-3.5" />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7"
        aria-label={tx("browserBar.external", "用系统浏览器打开")}
        title={tx("browserBar.external", "用系统浏览器打开")}
        onClick={onOpenExternal}
      >
        <ExternalLink className="h-3.5 w-3.5" />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7"
        aria-label={tx("browserBar.close", "关闭浏览器窗口")}
        title={tx("browserBar.close", "关闭浏览器窗口")}
        onClick={onClose}
      >
        <X className="h-3.5 w-3.5" />
      </Button>
    </div>
  );
}
