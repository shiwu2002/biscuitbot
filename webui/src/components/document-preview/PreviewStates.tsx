import { type ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

/** 文档预览的统一外壳：复用 AttachmentTile 的圆角卡片视觉语言。 */
export function PreviewFrame({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "not-prose mt-2 w-full max-w-[min(100%,44rem)] overflow-hidden rounded-[14px]",
        "border border-border/60 bg-muted/40",
        className,
      )}
    >
      {children}
    </div>
  );
}

/** 加载中态：转圈 + 文案。 */
export function PreviewLoading({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 px-4 py-5 text-xs text-muted-foreground">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
      <span>{label}</span>
    </div>
  );
}

/** 失败态：错误文案 + 下载回退链接。 */
export function PreviewError({ url, message }: { url?: string; message: string }) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-3 text-xs text-muted-foreground">
      <span className="min-w-0 truncate">{message}</span>
      {url ? (
        <a
          href={url}
          download
          className="flex-none rounded-md border border-border/60 bg-muted/30 px-2 py-1 text-foreground/80 transition-colors hover:bg-muted/50 hover:text-foreground"
        >
          {t("documentPreview.download", { defaultValue: "下载" })}
        </a>
      ) : null}
    </div>
  );
}
