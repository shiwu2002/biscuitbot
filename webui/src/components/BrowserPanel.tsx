import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent } from "react";
import { AppWindow, Globe, Loader2, Plus, RotateCw, UserCheck, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { fetchBrowserStatus, startBrowserService, stopBrowserService } from "@/lib/api";
import type {
  BrowserFrameMetadata,
  BrowserPageInfo,
  BrowserStatusPayload,
} from "@/lib/types";
import { showToast } from "@/lib/toast";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

const DEFAULT_WIDTH = 640;

interface BrowserPanelProps {
  token: string;
  desktopWidth?: number;
  isClosing?: boolean;
  /** 桌面宿主顶栏存在时整体下移 44px（与 FilePreviewPanel 同一套避让）。 */
  hostChromeActive?: boolean;
  onClose: () => void;
}

interface FrameInput {
  data: string;
  metadata: BrowserFrameMetadata;
}

/** CDP 键盘事件需要的常用键位映射（windowsVirtualKeyCode）。 */
const SPECIAL_KEYS: Record<string, { code: string; vk: number }> = {
  Enter: { code: "Enter", vk: 13 },
  Backspace: { code: "Backspace", vk: 8 },
  Tab: { code: "Tab", vk: 9 },
  Escape: { code: "Escape", vk: 27 },
  ArrowUp: { code: "ArrowUp", vk: 38 },
  ArrowDown: { code: "ArrowDown", vk: 40 },
  ArrowLeft: { code: "ArrowLeft", vk: 37 },
  ArrowRight: { code: "ArrowRight", vk: 39 },
  Delete: { code: "Delete", vk: 46 },
  Home: { code: "Home", vk: 36 },
  End: { code: "End", vk: 35 },
  PageUp: { code: "PageUp", vk: 33 },
  PageDown: { code: "PageDown", vk: 34 },
};

function mouseButtonName(button: number): string {
  if (button === 1) return "middle";
  if (button === 2) return "right";
  return "left";
}

/** alt=1 ctrl=2 meta=4 shift=8（CDP modifiers 位掩码）。 */
function modifierMask(event: ReactKeyboardEvent): number {
  let mask = 0;
  if (event.altKey) mask |= 1;
  if (event.ctrlKey) mask |= 2;
  if (event.metaKey) mask |= 4;
  if (event.shiftKey) mask |= 8;
  return mask;
}

export function BrowserPanel({
  token,
  desktopWidth = DEFAULT_WIDTH,
  isClosing = false,
  hostChromeActive = false,
  onClose,
}: BrowserPanelProps) {
  const { t } = useTranslation();
  const { client } = useClient();
  const [status, setStatus] = useState<BrowserStatusPayload | null>(null);
  const [pages, setPages] = useState<BrowserPageInfo[]>([]);
  const [starting, setStarting] = useState(false);
  const [entered, setEntered] = useState(false);
  /** 是否已收到过截图帧：用于区分「就绪空态」与真实画面。 */
  const [hasFrame, setHasFrame] = useState(false);
  const [addressDraft, setAddressDraft] = useState("");
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const addressRef = useRef<HTMLInputElement | null>(null);
  const frameMetaRef = useRef<{ deviceWidth: number; deviceHeight: number } | null>(null);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => setEntered(true));
    return () => window.cancelAnimationFrame(frame);
  }, []);

  // 初始拉取状态；运行中则订阅截图流并请求标签页列表。
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const initial = await fetchBrowserStatus(token);
        if (cancelled) return;
        setStatus(initial);
        if (initial.running) {
          client.browserAttach();
          client.sendBrowserControl({ type: "browser_tabs" });
        }
      } catch {
        // 网关未就绪等场景：保持未启动态，用户可手动重试
      }
    })();
    return () => {
      cancelled = true;
      client.browserDetach();
    };
  }, [client, token]);

  // 订阅 WS 事件：帧渲染到 canvas，状态/标签页/错误更新 UI。
  useEffect(() => {
    const drawFrame = (frame: FrameInput) => {
      const canvas = canvasRef.current;
      if (!canvas || !frame.data) return;
      setHasFrame(true);
      if (
        typeof frame.metadata.deviceWidth === "number"
        && typeof frame.metadata.deviceHeight === "number"
      ) {
        frameMetaRef.current = {
          deviceWidth: frame.metadata.deviceWidth,
          deviceHeight: frame.metadata.deviceHeight,
        };
      }
      const img = new Image();
      img.onload = () => {
        const target = canvasRef.current;
        if (!target) return;
        target.width = img.naturalWidth;
        target.height = img.naturalHeight;
        target.getContext("2d")?.drawImage(img, 0, 0);
      };
      img.src = `data:image/jpeg;base64,${frame.data}`;
    };
    const offs = [
      client.onBrowserFrame(drawFrame),
      client.onBrowserStatus((next) => {
        setStatus(next);
        if (!next.running) {
          setPages([]);
          setHasFrame(false);
        }
      }),
      client.onBrowserTabs(setPages),
      client.onBrowserError((detail) => {
        if (detail !== "page_not_connected") {
          showToast(
            t("browserPanel.errorToast", {
              defaultValue: "Browser panel error: {{detail}}",
              detail,
            }),
            "error",
          );
        }
      }),
    ];
    return () => {
      for (const off of offs) off();
    };
  }, [client, t]);

  useEffect(() => {
    if (status?.url) setAddressDraft(status.url);
  }, [status?.url]);

  // 启动后自动聚焦地址栏：引导用户直接输入网址，而不是面对白屏。
  useEffect(() => {
    if (status?.running) addressRef.current?.focus();
  }, [status?.running]);

  // 非被动 wheel 监听：阻止页面滚动，把滚动交给远端浏览器。
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const coords = toPageCoords(event.clientX, event.clientY);
      if (!coords) return;
      client.sendBrowserControl({
        type: "browser_input",
        action: "wheel",
        params: { ...coords, deltaX: event.deltaX, deltaY: event.deltaY },
      });
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    return () => canvas.removeEventListener("wheel", onWheel);
  }, [client, status?.running]);

  const toPageCoords = useCallback(
    (clientX: number, clientY: number): { x: number; y: number } | null => {
      const canvas = canvasRef.current;
      const meta = frameMetaRef.current;
      if (!canvas || !meta) return null;
      const rect = canvas.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return null;
      return {
        x: ((clientX - rect.left) / rect.width) * meta.deviceWidth,
        y: ((clientY - rect.top) / rect.height) * meta.deviceHeight,
      };
    },
    [],
  );

  const handleStart = async () => {
    setStarting(true);
    try {
      const next = await startBrowserService(token);
      setStatus(next);
      if (next.running) {
        client.browserAttach();
        client.sendBrowserControl({ type: "browser_tabs" });
      }
    } catch {
      showToast(t("browserPanel.startFailed", { defaultValue: "浏览器启动失败" }), "error");
    } finally {
      setStarting(false);
    }
  };

  const handleStop = async () => {
    try {
      const next = await stopBrowserService(token);
      setStatus(next);
    } catch {
      // 忽略停止失败：状态事件会纠正显示
    }
  };

  const handleNavigate = (raw: string) => {
    const url = raw.trim();
    if (!url) return;
    client.sendBrowserControl({ type: "browser_navigate", url });
  };

  const handleReload = () => {
    client.sendBrowserControl({ type: "browser_reload" });
  };

  const handleNewTab = () => {
    client.sendBrowserControl({ type: "browser_open_tab" });
    client.sendBrowserControl({ type: "browser_tabs" });
  };

  const handleMouseDown = (event: ReactMouseEvent<HTMLCanvasElement>) => {
    event.preventDefault();
    event.currentTarget.focus();
    const coords = toPageCoords(event.clientX, event.clientY);
    if (!coords) return;
    client.sendBrowserControl({
      type: "browser_input",
      action: "mouse",
      params: {
        type: "mousePressed",
        ...coords,
        button: mouseButtonName(event.button),
        clickCount: event.detail || 1,
        buttons: 1 << event.button,
      },
    });
  };

  const handleMouseUp = (event: ReactMouseEvent<HTMLCanvasElement>) => {
    const coords = toPageCoords(event.clientX, event.clientY);
    if (!coords) return;
    client.sendBrowserControl({
      type: "browser_input",
      action: "mouse",
      params: {
        type: "mouseReleased",
        ...coords,
        button: mouseButtonName(event.button),
        clickCount: event.detail || 1,
        buttons: 0,
      },
    });
  };

  const handleMouseMove = (event: ReactMouseEvent<HTMLCanvasElement>) => {
    const coords = toPageCoords(event.clientX, event.clientY);
    if (!coords) return;
    client.sendBrowserControl({
      type: "browser_input",
      action: "mouse",
      params: { type: "mouseMoved", ...coords, buttons: event.buttons },
    });
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLCanvasElement>) => {
    if (event.ctrlKey && (event.key === "a" || event.key === "c" || event.key === "v" || event.key === "x")) {
      // 浏览器/系统快捷键留在本地剪贴板语义，不转发
      return;
    }
    event.preventDefault();
    const modifiers = modifierMask(event);
    const special = SPECIAL_KEYS[event.key];
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey) {
      client.sendBrowserControl({
        type: "browser_input",
        action: "key",
        params: {
          type: "char",
          key: event.key,
          text: event.key,
          unmodifiedText: event.key,
          code: event.code,
          windowsVirtualKeyCode: event.keyCode,
          nativeVirtualKeyCode: event.keyCode,
          modifiers,
        },
      });
      return;
    }
    if (special) {
      client.sendBrowserControl({
        type: "browser_input",
        action: "key",
        params: {
          type: "rawKeyDown",
          key: event.key,
          code: special.code,
          windowsVirtualKeyCode: special.vk,
          nativeVirtualKeyCode: special.vk,
          modifiers,
        },
      });
      client.sendBrowserControl({
        type: "browser_input",
        action: "key",
        params: {
          type: "keyUp",
          key: event.key,
          code: special.code,
          windowsVirtualKeyCode: special.vk,
          nativeVirtualKeyCode: special.vk,
          modifiers,
        },
      });
      return;
    }
    // 修饰键 / 功能键等：原样下发 rawKeyDown + keyUp
    client.sendBrowserControl({
      type: "browser_input",
      action: "key",
      params: {
        type: "rawKeyDown",
        key: event.key,
        code: event.code,
        windowsVirtualKeyCode: event.keyCode,
        nativeVirtualKeyCode: event.keyCode,
        modifiers,
      },
    });
  };

  const handleKeyUp = (event: ReactKeyboardEvent<HTMLCanvasElement>) => {
    const special = SPECIAL_KEYS[event.key];
    client.sendBrowserControl({
      type: "browser_input",
      action: "key",
      params: {
        type: "keyUp",
        key: event.key,
        code: special?.code ?? event.code,
        windowsVirtualKeyCode: special?.vk ?? event.keyCode,
        nativeVirtualKeyCode: special?.vk ?? event.keyCode,
        modifiers: modifierMask(event),
      },
    });
  };

  const running = status?.running === true;
  // about:blank（或尚未收到第一帧）时画面必然是一块白色——与其让人以为坏了，
  // 不如盖一层「就绪」引导：输入网址 / 让智能体操作。
  const blankScreen = running && (!hasFrame || !status?.url || status.url === "about:blank");
  const modeLabel = running
    ? status?.mode === "external"
      ? t("browserPanel.statusExternal", { defaultValue: "已连接你的浏览器" })
      : t("browserPanel.statusManaged", { defaultValue: "内置浏览器" })
    : t("browserPanel.statusStopped", { defaultValue: "未启动" });

  return (
    <aside
      aria-label={t("browserPanel.aria", { defaultValue: "内置浏览器" })}
      style={{
        "--browser-panel-width": `${desktopWidth}px`,
        "--browser-panel-slot-width": !entered || isClosing ? "0px" : `${desktopWidth}px`,
      } as CSSProperties}
      className={cn(
        "absolute right-0 z-30 w-[min(100vw,var(--browser-panel-slot-width))] overflow-hidden",
        hostChromeActive ? "top-11 bottom-0 md:top-auto md:mt-11" : "inset-y-0",
        "transition-[width] duration-300 ease-out will-change-[width]",
        "md:relative md:z-auto md:w-[var(--browser-panel-slot-width)] md:min-w-0 md:shrink-0",
        isClosing && "pointer-events-none",
      )}
      data-testid="browser-panel"
    >
      <div
        className={cn(
          "absolute inset-y-0 right-0 flex w-[min(100vw,var(--browser-panel-width))] flex-col overflow-hidden pb-[env(safe-area-inset-bottom)] md:w-[var(--browser-panel-width)] md:pb-0",
          "border-l border-border/70 bg-background shadow-2xl md:shadow-none",
          "transition-[opacity,transform] duration-300 ease-out will-change-transform",
          !entered || isClosing ? "translate-x-full opacity-0" : "translate-x-0 opacity-100",
          "motion-reduce:translate-x-0",
        )}
      >
        {/* 头部：状态 + 地址栏 + 标签页 */}
        <div className="flex h-12 shrink-0 items-center gap-2 border-b border-border/60 px-3">
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("browserPanel.close", { defaultValue: "关闭内置浏览器" })}
            onClick={onClose}
            className="h-8 w-8 shrink-0 rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </Button>
          <span
            className={cn(
              "inline-flex h-2 w-2 shrink-0 rounded-full",
              running ? "bg-emerald-500" : "bg-muted-foreground/40",
            )}
            title={modeLabel}
          />
          <span className="shrink-0 text-xs font-medium text-muted-foreground">{modeLabel}</span>
          <form
            className="flex min-w-0 flex-1 items-center"
            onSubmit={(event) => {
              event.preventDefault();
              handleNavigate(addressDraft);
            }}
          >
            <input
              ref={addressRef}
              value={addressDraft}
              onChange={(event) => setAddressDraft(event.target.value)}
              placeholder={t("browserPanel.addressPlaceholder", { defaultValue: "输入网址…" })}
              disabled={!running}
              spellCheck={false}
              className={cn(
                "min-w-0 flex-1 rounded-full border border-border/60 bg-muted/40 px-3 py-1.5 text-xs",
                "text-foreground placeholder:text-muted-foreground/60",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                "disabled:opacity-50",
              )}
            />
          </form>
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("browserPanel.reload", { defaultValue: "刷新页面" })}
            disabled={!running}
            onClick={handleReload}
            className="h-8 w-8 shrink-0 rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <RotateCw className="h-4 w-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("browserPanel.newTab", { defaultValue: "新建标签页" })}
            disabled={!running}
            onClick={handleNewTab}
            className="h-8 w-8 shrink-0 rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <Plus className="h-4 w-4" />
          </Button>
        </div>

        {/* 标签页条 */}
        {running && pages.length > 0 ? (
          <div className="flex shrink-0 items-center gap-1 overflow-x-auto border-b border-border/45 px-2 py-1.5">
            {pages.map((page) => (
              <button
                key={page.id}
                type="button"
                onClick={() =>
                  client.sendBrowserControl({ type: "browser_select_tab", target_id: page.id })
                }
                title={page.url ?? undefined}
                className={cn(
                  "max-w-[10rem] shrink-0 truncate rounded-full px-2.5 py-1 text-[11px]",
                  page.id === activePageId(status, pages)
                    ? "bg-primary/10 font-medium text-foreground"
                    : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
                )}
              >
                {page.title || page.url || t("browserPanel.untitledTab", { defaultValue: "空白页" })}
              </button>
            ))}
          </div>
        ) : null}

        {/* 画面区 */}
        <div className="relative flex min-h-0 flex-1 items-center justify-center overflow-auto bg-muted/25">
          {!running ? (
            <div className="flex flex-col items-center gap-3 px-8 text-center text-sm text-muted-foreground">
              <Globe className="h-8 w-8 text-muted-foreground/50" aria-hidden />
              <p className="max-w-[16rem] leading-relaxed">
                {t("browserPanel.startHint", {
                  defaultValue:
                    "启动内置浏览器后，智能体的浏览器操作将在这里实时展示；已开启调试端口的浏览器会被直接复用。",
                })}
              </p>
              <Button onClick={() => void handleStart()} disabled={starting} size="sm">
                {starting ? (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                ) : (
                  <AppWindow className="h-4 w-4" aria-hidden />
                )}
                {t("browserPanel.start", { defaultValue: "启动内置浏览器" })}
              </Button>
            </div>
          ) : (
            <>
              <canvas
                ref={canvasRef}
                tabIndex={0}
                aria-label={t("browserPanel.screenAria", { defaultValue: "浏览器画面（点击后可遥控）" })}
                onMouseDown={handleMouseDown}
                onMouseUp={handleMouseUp}
                onMouseMove={handleMouseMove}
                onContextMenu={(event) => event.preventDefault()}
                onKeyDown={handleKeyDown}
                onKeyUp={handleKeyUp}
                className={cn(
                  "max-h-full max-w-full select-none bg-white",
                  "outline-none focus-visible:ring-2 focus-visible:ring-ring",
                )}
              />
              {blankScreen ? (
                <div
                  className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 bg-background/92 px-8 text-center backdrop-blur-[2px]"
                  aria-hidden={false}
                >
                  <Globe className="h-9 w-9 text-muted-foreground/45" aria-hidden />
                  <p className="text-sm font-medium text-foreground">
                    {t("browserPanel.readyTitle", { defaultValue: "内置浏览器已就绪" })}
                  </p>
                  <p className="max-w-[17rem] text-xs leading-relaxed text-muted-foreground">
                    {t("browserPanel.readyHint", {
                      defaultValue:
                        "在上方地址栏输入网址开始浏览；智能体做浏览器操作时，画面也会实时显示在这里。",
                    })}
                  </p>
                </div>
              ) : null}
            </>
          )}
        </div>

        {/* 底部提示 */}
        {running ? (
          <div className="flex h-8 shrink-0 items-center justify-between border-t border-border/45 px-3 text-[11px] text-muted-foreground/70">
            <span className="inline-flex items-center gap-1.5">
              {status?.mode === "external" ? (
                <>
                  <UserCheck className="h-3 w-3" aria-hidden />
                  {t("browserPanel.reusedHint", { defaultValue: "复用用户浏览器 · 不会关闭它" })}
                </>
              ) : (
                <>
                  <AppWindow className="h-3 w-3" aria-hidden />
                  {t("browserPanel.managedHint", { defaultValue: "应用专属实例 · 登录态持久" })}
                </>
              )}
            </span>
            {status?.mode === "managed" ? (
              <button
                type="button"
                onClick={() => void handleStop()}
                className="transition-colors hover:text-foreground"
              >
                {t("browserPanel.stop", { defaultValue: "停止" })}
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </aside>
  );
}

/** 当前活动标签页 id（后端 list_pages 把活动页排最前，WS 同步后以 status.url 对齐兜底）。 */
function activePageId(status: BrowserStatusPayload | null, pages: BrowserPageInfo[]): string | null {
  if (pages.length === 0) return null;
  const byUrl = pages.find((p) => p.url && status?.url && p.url === status.url);
  return byUrl?.id ?? pages[0]?.id ?? null;
}
