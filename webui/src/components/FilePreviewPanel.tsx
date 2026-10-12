import { useCallback, useEffect, useMemo, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent, ReactNode } from "react";
import { AlertCircle, ChevronRight, Download, FileText, FolderOpen, Loader2, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { CodeBlock } from "@/components/CodeBlock";
import { DocumentPreview } from "@/components/document-preview/DocumentPreview";
import { splitFilePath } from "@/components/FileReferenceChip";
import { getDocumentPreviewKind } from "@/lib/file-preview";
import {
  ApiError,
  fetchFileDownload,
  fetchFilePreview,
  revealWorkspaceFile,
} from "@/lib/api";
import type { FilePreviewPayload } from "@/lib/types";
import { showToast } from "@/lib/toast";
import { cn } from "@/lib/utils";

interface FilePreviewPanelProps {
  sessionKey: string;
  /** 工作区路径预览模式：走 file-preview API（与 ``media`` 二选一）。 */
  path?: string;
  /** 消息附件 media 模式：附件没有原始路径时用签名 URL 直接渲染 DocumentPreview。 */
  media?: { url: string; name: string } | null;
  token: string;
  desktopWidth?: number;
  isClosing?: boolean;
  /** 客户端与工作区同机时为 true：可以「在文件夹中显示」而不必下载一份副本。 */
  canRevealFile?: boolean;
  /** 桌面宿主顶栏（App.tsx 拖拽层 h-11 + 窗口控制按钮 z-[60]）存在时为 true：
   * 面板整体下移 44px，避免头部关闭/下载按钮被顶栏遮挡、点击被拖拽层吞掉。 */
  hostChromeActive?: boolean;
  onResizeStart?: (event: ReactPointerEvent<HTMLButtonElement>) => void;
  onClose: () => void;
}

type PreviewState =
  | { status: "loading" }
  | { status: "error"; message: string; code?: number }
  | { status: "ready"; payload: FilePreviewPayload };

function supportsHoverCloseControl(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia("(hover: hover) and (pointer: fine)").matches;
}

/** 文件操作入口：头部图标按钮与错误态带文字按钮共用。 */
function FileActionButton({
  busy,
  icon,
  label,
  onClick,
  className,
  spinnerClassName = "h-4 w-4",
  showLabel = false,
}: {
  busy: boolean;
  icon: ReactNode;
  label: string;
  onClick: () => void;
  className?: string;
  spinnerClassName?: string;
  showLabel?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      title={label}
      aria-label={label}
      className={cn("disabled:opacity-60", className)}
    >
      {busy ? (
        <Loader2 className={cn("shrink-0 animate-spin", spinnerClassName)} aria-hidden />
      ) : (
        icon
      )}
      {showLabel ? <span>{label}</span> : null}
    </button>
  );
}

const FILE_ACTION_ICON_CLASS = cn(
  "inline-flex h-8 w-8 items-center justify-center rounded-full",
  "text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
);

const FILE_ACTION_LABEL_CLASS = cn(
  "inline-flex items-center gap-1.5 rounded-md border border-border/60 bg-muted/30 px-2.5 py-1 text-xs",
  "text-foreground/80 transition-colors hover:bg-muted/50 hover:text-foreground",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
);

export function FilePreviewPanel({
  sessionKey,
  path,
  media = null,
  token,
  desktopWidth = 544,
  isClosing = false,
  canRevealFile = false,
  hostChromeActive = false,
  onResizeStart,
  onClose,
}: FilePreviewPanelProps) {
  const { t } = useTranslation();
  const [state, setState] = useState<PreviewState>({ status: "loading" });
  const [entered, setEntered] = useState(false);
  // 两个操作共用一个 busy 槽位：任一进行中都禁用另一个，避免重复触发系统调用。
  const [busyAction, setBusyAction] = useState<"download" | "reveal" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [supportsHoverClose, setSupportsHoverClose] = useState(supportsHoverCloseControl);
  const downloading = busyAction === "download";
  const revealing = busyAction === "reveal";
  /** media 模式：附件签名 URL 直渲染，不经过 file-preview API。 */
  const isMediaMode = media !== null;
  const mediaDocKind = isMediaMode ? getDocumentPreviewKind(media.name || media.url) : null;

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => setEntered(true));
    return () => window.cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return undefined;
    const query = window.matchMedia("(hover: hover) and (pointer: fine)");
    const update = () => setSupportsHoverClose(query.matches);
    update();
    if (typeof query.addEventListener === "function") {
      query.addEventListener("change", update);
      return () => query.removeEventListener("change", update);
    }
    query.addListener(update);
    return () => query.removeListener(update);
  }, []);

  useEffect(() => {
    if (isMediaMode || !path) return undefined;
    let cancelled = false;
    setState({ status: "loading" });
    setActionError(null);
    fetchFilePreview(token, sessionKey, path)
      .then((payload) => {
        if (!cancelled) setState({ status: "ready", payload });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        const code = error instanceof ApiError ? error.status : undefined;
        let message = t("filePreview.failed", { defaultValue: "Could not preview this file." });
        if (code === 415) {
          // 二进制文件（压缩包/可执行文件等）：预览走不通，直接指向下载
          message = t("filePreview.binary", {
            defaultValue: "This file is binary and cannot be previewed. Download it to open it.",
          });
        } else if (error instanceof ApiError) {
          message = error.status === 404 && /API route not found/i.test(error.message)
            ? t("filePreview.routeMissing", {
              defaultValue: "File preview needs the latest gateway. Restart 夏奈儿 gateway and try again.",
            })
            : error.message;
        }
        setState({ status: "error", message, code });
      });
    return () => {
      cancelled = true;
    };
  }, [isMediaMode, path, sessionKey, t, token]);

  // 懒加载：只有点下载才 staging 文件并签名，避免预览每个文件都往 media 目录拷一份。
  // media 模式下签名 URL 已就绪，直接 fetch 字节确认后保存。
  const handleDownload = useCallback(async () => {
    if (busyAction) return;
    setBusyAction("download");
    setActionError(null);
    try {
      let url: string;
      let name: string;
      if (media) {
        url = media.url;
        name = media.name;
      } else {
        ({ url, name } = await fetchFileDownload(token, sessionKey, path ?? ""));
      }
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = name;
      anchor.rel = "noreferrer";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      // 签名 URL 已就绪并触发保存，给用户一个明确的成功反馈。
      showToast(
        t("downloadToast.success", { name, defaultValue: `《${name}》下载成功` }),
        "success",
      );
    } catch {
      setActionError(t("filePreview.downloadFailed", { defaultValue: "Download failed. Try again." }));
    } finally {
      setBusyAction(null);
    }
  }, [busyAction, media, path, sessionKey, t, token]);

  // 同机时直接在文件管理器中定位原文件：不产生任何副本，也不执行文件本身。
  const handleReveal = useCallback(async () => {
    if (busyAction || !path) return;
    setBusyAction("reveal");
    setActionError(null);
    try {
      await revealWorkspaceFile(token, sessionKey, path);
    } catch {
      setActionError(t("filePreview.revealFailed", { defaultValue: "Could not show this file in the file manager." }));
    } finally {
      setBusyAction(null);
    }
  }, [busyAction, path, sessionKey, t, token]);

  const downloadLabel = downloading
    ? t("filePreview.downloading", { defaultValue: "Preparing download…" })
    : t("filePreview.download", { defaultValue: "Download" });
  const revealLabel = revealing
    ? t("filePreview.revealing", { defaultValue: "Opening the file manager…" })
    : t("filePreview.reveal", { defaultValue: "Show in folder" });

  const displayPath = isMediaMode
    ? (media?.name ?? "")
    : state.status === "ready"
      ? state.payload.display_path
      : (path ?? "");
  const previewPath = state.status === "ready" ? state.payload.path : displayPath;
  const normalizedPreviewPath = previewPath.replace(/\\/g, "/");
  const hasRootPrefix = normalizedPreviewPath.startsWith("/");
  const { name } = splitFilePath(displayPath);
  const breadcrumbs = useMemo(
    () => normalizedPreviewPath.split("/").filter(Boolean),
    [normalizedPreviewPath],
  );
  const compactBreadcrumbs = useMemo(
    () => (breadcrumbs.length > 2 ? breadcrumbs.slice(-2) : breadcrumbs),
    [breadcrumbs],
  );
  const hasCompactPrefix = breadcrumbs.length > compactBreadcrumbs.length;

  return (
    <aside
      aria-label={t("filePreview.aria", { defaultValue: "File preview" })}
      style={{
        "--file-preview-width": `${desktopWidth}px`,
        "--file-preview-slot-width": !entered || isClosing ? "0px" : `${desktopWidth}px`,
      } as CSSProperties}
      className={cn(
        "absolute right-0 z-30 w-[min(100vw,var(--file-preview-slot-width))] overflow-hidden",
        // 宿主顶栏（44px 拖拽层 + 右上角窗口控制按钮）会盖住面板头部：
        // 移动端覆盖层从顶栏下方开始；桌面端作为 flex 项用 margin-top 让出
        // 标题栏高度（stretch 会自动减去 margin，底部不被裁切）。
        hostChromeActive ? "top-11 bottom-0 md:top-auto md:mt-11" : "inset-y-0",
        "transition-[width] duration-300 ease-out will-change-[width]",
        "md:relative md:z-auto md:w-[var(--file-preview-slot-width)] md:min-w-0 md:shrink-0",
        isClosing && "pointer-events-none",
      )}
      data-testid="file-preview-panel"
      data-file-preview-panel
    >
      <div
        className={cn(
          "absolute inset-y-0 right-0 flex w-[min(100vw,var(--file-preview-width))] flex-col overflow-hidden pb-[env(safe-area-inset-bottom)] md:w-[var(--file-preview-width)] md:pb-0",
          "border-l border-border/70 bg-background shadow-2xl md:shadow-none",
          "transition-[opacity,transform] duration-300 ease-out will-change-transform",
          !entered || isClosing ? "translate-x-full opacity-0" : "translate-x-0 opacity-100",
          "motion-reduce:translate-x-0",
        )}
      >
        {onResizeStart ? (
          <button
            type="button"
            aria-label={t("filePreview.resize", { defaultValue: "Resize file preview" })}
            className={cn(
              "group absolute inset-y-0 left-0 z-20 hidden w-3 -translate-x-1/2 cursor-col-resize touch-none md:flex",
              "items-stretch justify-center focus-visible:outline-none",
            )}
            onPointerDown={onResizeStart}
          >
            <span
              aria-hidden
              className={cn(
                "h-full w-px bg-foreground/25 opacity-0 transition-opacity",
                "group-hover:opacity-100 group-focus-visible:bg-ring group-focus-visible:opacity-100",
              )}
            />
          </button>
        ) : null}
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="flex h-12 shrink-0 items-center gap-2 border-b border-border/60 px-3">
            {supportsHoverClose ? (
              <div
                className={cn(
                  "group inline-flex max-w-full min-w-0 items-center gap-2 rounded-[12px]",
                  "bg-muted/70 px-2.5 py-1.5 text-sm font-medium",
                )}
                title={name || displayPath}
              >
                <button
                  type="button"
                  onClick={onClose}
                  className={cn(
                    "relative inline-flex h-5 w-5 shrink-0 items-center justify-center overflow-hidden rounded-full",
                    "text-muted-foreground/75 transition-[background-color,color,opacity] duration-150 ease-out",
                    "group-hover:bg-foreground group-hover:text-background group-hover:opacity-100",
                    "group-focus-within:bg-foreground group-focus-within:text-background",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  )}
                  aria-label={t("filePreview.close", { defaultValue: "Close file preview" })}
                >
                  <FileText
                    className={cn(
                      "absolute h-4 w-4 transition-all duration-150 ease-out",
                      "opacity-100 group-hover:scale-75 group-hover:opacity-0",
                      "group-focus-within:scale-75 group-focus-within:opacity-0",
                    )}
                    aria-hidden
                  />
                  <X
                    className={cn(
                      "absolute h-3.5 w-3.5 scale-75 opacity-0 transition-all duration-150 ease-out",
                      "group-hover:scale-100 group-hover:opacity-100",
                      "group-focus-within:scale-100 group-focus-within:opacity-100",
                    )}
                    aria-hidden
                  />
                </button>
                <span className="min-w-0 truncate">{name || displayPath}</span>
              </div>
            ) : (
              <>
                <button
                  type="button"
                  onClick={onClose}
                  className={cn(
                    "inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full",
                    "text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  )}
                  aria-label={t("filePreview.close", { defaultValue: "Close file preview" })}
                >
                  <X className="h-5 w-5" aria-hidden />
                </button>
                <span className="min-w-0 truncate text-sm font-medium">
                  {name || displayPath}
                </span>
              </>
            )}
            <div className="ml-auto flex flex-none items-center gap-1">
              {canRevealFile && !isMediaMode ? (
                <FileActionButton
                  busy={revealing}
                  icon={<FolderOpen className="h-4 w-4" aria-hidden />}
                  label={revealLabel}
                  onClick={() => void handleReveal()}
                  className={FILE_ACTION_ICON_CLASS}
                />
              ) : null}
              <FileActionButton
                busy={downloading}
                icon={<Download className="h-4 w-4" aria-hidden />}
                label={downloadLabel}
                onClick={() => void handleDownload()}
                className={FILE_ACTION_ICON_CLASS}
              />
            </div>
          </div>

          <div className="flex min-h-0 flex-1 flex-col">
            <div
              className={cn(
                "flex min-h-10 shrink-0 items-center gap-1.5 overflow-hidden",
                "border-b border-border/45 px-4 text-[13px] text-muted-foreground",
              )}
              title={previewPath}
            >
              <div className="flex min-w-0 items-center gap-1.5">
                {hasCompactPrefix ? (
                  <span className="shrink-0 text-muted-foreground/55">...</span>
                ) : hasRootPrefix ? (
                  <span className="shrink-0 text-muted-foreground/55">/</span>
                ) : null}
                {compactBreadcrumbs.length > 0 ? (
                  compactBreadcrumbs.map((part, index) => (
                    <span key={`${part}-${index}`} className="flex min-w-0 items-center gap-1.5">
                      {index > 0 || hasCompactPrefix || hasRootPrefix ? (
                        <ChevronRight
                          className="h-3 w-3 shrink-0 text-muted-foreground/40"
                          aria-hidden
                        />
                      ) : null}
                      <span
                        className={cn(
                          "min-w-0 truncate",
                          index === compactBreadcrumbs.length - 1
                            ? "font-medium text-foreground"
                            : "max-w-[42vw] shrink text-muted-foreground/76",
                        )}
                      >
                        {part}
                      </span>
                    </span>
                  ))
                ) : (
                  <span className="truncate">{previewPath}</span>
                )}
              </div>
            </div>

            {actionError ? (
              <p className="shrink-0 border-b border-border/45 bg-destructive/10 px-4 py-2 text-xs text-destructive">
                {actionError}
              </p>
            ) : null}

            <div className="min-h-0 flex-1 overflow-auto">
              {isMediaMode ? (
                mediaDocKind !== null && media ? (
                  // 附件 media 模式：直接用签名 URL 渲染文档预览（docx/pptx/xlsx/csv/html 等）
                  <DocumentPreview
                    attachment={{ kind: "file", url: media.url, name: media.name }}
                    kind={mediaDocKind}
                  />
                ) : (
                  <div className="flex h-full items-center justify-center px-8 text-center text-sm text-muted-foreground">
                    <div className="max-w-sm">
                      <AlertCircle className="mx-auto mb-3 h-5 w-5 text-muted-foreground/70" aria-hidden />
                      <p>
                        {t("filePreview.binary", {
                          defaultValue: "This file is binary and cannot be previewed. Download it to open it.",
                        })}
                      </p>
                      <div className="mt-4 flex items-center justify-center gap-2">
                        <FileActionButton
                          busy={downloading}
                          icon={<Download className="h-3.5 w-3.5" aria-hidden />}
                          label={downloadLabel}
                          onClick={() => void handleDownload()}
                          showLabel
                          spinnerClassName="h-3.5 w-3.5"
                          className={FILE_ACTION_LABEL_CLASS}
                        />
                      </div>
                    </div>
                  </div>
                )
              ) : state.status === "loading" ? (
                <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                  {t("filePreview.loading", { defaultValue: "Loading preview..." })}
                </div>
              ) : state.status === "error" ? (
                <div className="flex h-full items-center justify-center px-8 text-center text-sm text-muted-foreground">
                  <div className="max-w-sm">
                    <AlertCircle className="mx-auto mb-3 h-5 w-5 text-muted-foreground/70" aria-hidden />
                    <p>{state.message}</p>
                    {/* 二进制文件预览不了，但仍应能拿到文件 */}
                    {state.code === 415 ? (
                      <div className="mt-4 flex items-center justify-center gap-2">
                        {canRevealFile ? (
                          <FileActionButton
                            busy={revealing}
                            icon={<FolderOpen className="h-3.5 w-3.5" aria-hidden />}
                            label={revealLabel}
                            onClick={() => void handleReveal()}
                            showLabel
                            spinnerClassName="h-3.5 w-3.5"
                            className={FILE_ACTION_LABEL_CLASS}
                          />
                        ) : null}
                        <FileActionButton
                          busy={downloading}
                          icon={<Download className="h-3.5 w-3.5" aria-hidden />}
                          label={downloadLabel}
                          onClick={() => void handleDownload()}
                          showLabel
                          spinnerClassName="h-3.5 w-3.5"
                          className={FILE_ACTION_LABEL_CLASS}
                        />
                      </div>
                    ) : null}
                  </div>
                </div>
              ) : (
                <div className="min-h-full">
                  {state.payload.truncated ? (
                    <div className="mx-4 mt-3 rounded-md border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-200">
                      {t("filePreview.truncated", {
                        defaultValue: "Preview is truncated because this file is large.",
                      })}
                    </div>
                  ) : null}
                  <CodeBlock
                    language={state.payload.language}
                    code={state.payload.content}
                    chrome="none"
                    showLineNumbers
                    wrapLongLines={false}
                    className="min-h-full"
                  />
                </div>
              )}
          </div>
        </div>
      </div>
      </div>
    </aside>
  );
}
