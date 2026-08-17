import { useEffect, useState } from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { FileText, Loader2, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { fetchDocumentPreview } from "@/lib/api";
import type { Asset, DocumentPreviewPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 生成文档资产的文本预览弹窗。
 *
 * 浏览器无法原生渲染 docx/xlsx 等，后端经 ``extract_text`` 把文档提取为
 * 纯文本返回，这里在 Radix Dialog 面板里滚动展示（复用 ImageLightbox 的
 * Dialog 原语用法，绕开共享 DialogContent 的 max-w-lg 宽度限制）。
 */
export function DocumentPreviewDialog({
  asset,
  token,
  onOpenChange,
}: {
  asset: Asset | null;
  token: string;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const [preview, setPreview] = useState<DocumentPreviewPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const open = asset !== null;

  useEffect(() => {
    if (!asset) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setPreview(null);
    (async () => {
      try {
        const payload = await fetchDocumentPreview(token, asset.id);
        if (!cancelled) setPreview(payload);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [asset, token]);

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className={cn(
            "fixed inset-0 z-50 bg-black/60 backdrop-blur-sm",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
          )}
        />
        <DialogPrimitive.Content
          aria-label={asset?.name ?? "document preview"}
          className={cn(
            "fixed left-1/2 top-1/2 z-50 flex max-h-[85vh] w-[min(92vw,760px)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-[20px] border border-border/60 bg-background shadow-2xl outline-none",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
            "data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95",
          )}
        >
          <DialogPrimitive.Title className="sr-only">{asset?.name}</DialogPrimitive.Title>

          <div className="flex items-center justify-between gap-3 border-b border-border/60 px-5 py-3.5">
            <div className="flex min-w-0 items-center gap-2">
              <FileText className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
              <span className="truncate text-[14px] font-medium text-foreground">
                {asset?.name}
              </span>
            </div>
            <DialogPrimitive.Close asChild>
              <button
                type="button"
                aria-label={t("filePreview.close", { defaultValue: "关闭文件预览" })}
                className="rounded-full p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              >
                <X className="h-4 w-4" aria-hidden />
              </button>
            </DialogPrimitive.Close>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
            {loading ? (
              <div className="flex h-40 items-center justify-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                {t("filePreview.loading", { defaultValue: "正在加载预览..." })}
              </div>
            ) : error ? (
              <div className="rounded-[14px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
                {error}
              </div>
            ) : preview ? (
              <>
                <pre className="whitespace-pre-wrap break-words font-mono text-[12.5px] leading-relaxed text-foreground">
                  {preview.content}
                </pre>
                {preview.truncated ? (
                  <div className="mt-3 text-[12px] text-muted-foreground">
                    {t("filePreview.truncated", {
                      defaultValue: "文件较大，当前只显示前半部分预览。",
                    })}
                  </div>
                ) : null}
              </>
            ) : null}
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
