import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

/**
 * 全屏 HTML 渲染（「单端渲染界面」）：把智能体生成的页面铺满整个窗口来查看。
 *
 * 复用 ImageLightbox 的做法 —— 直接使用 Radix DialogPrimitive 铺满 viewport，
 * 因为共享的 `DialogContent` 会被 `max-w-lg` 卡死（对网页预览太小）。
 *
 * sandbox 与 HtmlViewer 一致：`allow-scripts` 可运行页面自身脚本（交互界面因此
 * 能渲染），但不加 `allow-same-origin`，页面无法读取宿主 app 的同源数据。
 */
export function FullscreenHtmlViewer({
  open,
  onOpenChange,
  src,
  name,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  src: string;
  name?: string;
}) {
  const { t } = useTranslation();
  const title = name ?? t("documentPreview.htmlTitle", { defaultValue: "HTML 预览" });

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className={cn(
            "fixed inset-0 z-50 bg-black/70 backdrop-blur-sm",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
            "motion-reduce:data-[state=open]:animate-none motion-reduce:data-[state=closed]:animate-none",
          )}
        />
        <DialogPrimitive.Content
          aria-label={title}
          className={cn(
            "fixed inset-0 z-50 flex flex-col bg-background",
            "focus:outline-none",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
            "data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95",
            "motion-reduce:data-[state=open]:animate-none motion-reduce:data-[state=closed]:animate-none",
          )}
        >
          <DialogPrimitive.Title className="sr-only">{title}</DialogPrimitive.Title>

          <div className="flex items-center justify-between gap-3 border-b border-border/60 px-4 py-3">
            <span className="min-w-0 truncate text-sm font-medium">{title}</span>
            <DialogPrimitive.Close
              aria-label={t("documentPreview.closeFullscreen", { defaultValue: "关闭全屏" })}
              className={cn(
                "grid h-8 w-8 flex-none place-items-center rounded-full text-muted-foreground",
                "transition-colors hover:bg-muted/60 hover:text-foreground",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              )}
            >
              <X className="h-4 w-4" aria-hidden />
            </DialogPrimitive.Close>
          </div>

          <iframe
            title={title}
            sandbox="allow-scripts"
            referrerPolicy="no-referrer"
            srcDoc={src}
            className="h-full w-full flex-1 bg-white"
          />
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
