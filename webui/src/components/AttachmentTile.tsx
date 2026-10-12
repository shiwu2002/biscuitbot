import { useState, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";
import { FileIcon, ImageIcon, Loader2, PlaySquare } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { downloadUrlWithFeedback } from "@/lib/download";
import { getDocumentPreviewKind } from "@/lib/file-preview";
import { showToast } from "@/lib/toast";
import type { UIMediaAttachment } from "@/lib/types";

interface AttachmentTileProps {
  attachment: UIMediaAttachment;
  className?: string;
  inline?: boolean;
  variant?: "default" | "compact";
  /**
   * 主聊天视图传入：点击 chip 不再下载，而是在右侧预览面板打开生成物
   * （能预览的渲染预览；不能预览的由面板提供「下载 / 打开所在位置」）。
   * 未传时保持旧的下载 chip 行为（inline/compact 等场景）。
   */
  onOpenPreview?: (attachment: UIMediaAttachment) => void;
}

export function AttachmentTile({
  attachment,
  className,
  inline = false,
  variant = "default",
  onOpenPreview,
}: AttachmentTileProps) {
  const { t } = useTranslation();
  const [failed, setFailed] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const hasUrl = typeof attachment.url === "string" && attachment.url.length > 0;
  const label = attachmentLabel(attachment, t);
  // 文档预览只作用于消息媒体路径（非 inline、非 compact）；其余调用点保持下载 chip。
  const docKind = getDocumentPreviewKind(attachment.name ?? attachment.url);

  // 文件 chip 默认是 <a download> 静默下载，用户拿不到任何反馈。
  // 改为 fetch 字节确认拿到文件后提示「下载成功」；失败回退原始链接并提示「已开始下载」。
  const handleDownloadClick = (event: ReactMouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    if (downloading || !attachment.url) return;
    const url = attachment.url;
    const name = attachment.name ?? label;
    setDownloading(true);
    void downloadUrlWithFeedback(url, name)
      .then((outcome) => {
        showToast(
          outcome === "saved"
            ? t("downloadToast.success", { name, defaultValue: `《${name}》下载成功` })
            : t("downloadToast.started", { name, defaultValue: `已开始下载《${name}》` }),
          outcome === "saved" ? "success" : "info",
        );
      })
      .finally(() => setDownloading(false));
  };

  if (attachment.kind === "image" && hasUrl && !failed) {
    return (
      <AttachmentFrame
        attachment={attachment}
        className={className}
        inline={inline}
        variant={variant}
      >
        <a
          href={attachment.url}
          target="_blank"
          rel="noreferrer noopener"
          className="block bg-muted/20"
          aria-label={attachment.name ? `Open ${attachment.name}` : t("lightbox.open", { defaultValue: "Open image" })}
        >
          <img
            src={attachment.url}
            alt={attachment.name ?? ""}
            loading="lazy"
            decoding="async"
            draggable={false}
            onError={() => setFailed(true)}
            className={cn(
              "block h-auto max-w-full bg-background object-contain",
              variant === "compact" ? "max-h-40" : "max-h-[34rem]",
            )}
          />
        </a>
      </AttachmentFrame>
    );
  }

  if (attachment.kind === "video" && hasUrl) {
    return (
      <AttachmentFrame
        attachment={attachment}
        className={className}
        inline={inline}
        variant={variant}
      >
        <video
          src={attachment.url}
          controls
          preload="auto"
          className={cn(
            "block w-full bg-black",
            variant === "compact" ? "max-h-40" : "max-h-[26rem]",
          )}
          aria-label={attachment.name ? `${t("message.videoAttachment", { defaultValue: "Video attachment" })}: ${attachment.name}` : t("message.videoAttachment", { defaultValue: "Video attachment" })}
        />
      </AttachmentFrame>
    );
  }

  const Icon = attachment.kind === "video"
    ? PlaySquare
    : attachment.kind === "image"
      ? ImageIcon
      : FileIcon;
  const body = (
    <>
      {downloading ? (
        <Loader2 className="h-4 w-4 flex-none animate-spin" aria-hidden />
      ) : (
        <Icon className="h-4 w-4 flex-none" aria-hidden />
      )}
      <span className="min-w-0 truncate">{attachment.name ?? label}</span>
    </>
  );

  // 主聊天视图：点击 chip 在右侧预览面板打开生成物（WorkBuddy 式），
  // 不在消息内展开预览、也不触发下载；下载/打开位置由面板头部提供。
  // 可预览 = 后端带了原始路径（工作区路径预览），或是面板可直接渲染的文档类型；
  // 两者都不满足（如无路径的压缩包）保持下载 chip，避免打开后无法展示。
  const canPreviewInPanel =
    (typeof attachment.path === "string" && attachment.path.length > 0) || docKind !== null;
  if (onOpenPreview && hasUrl && !failed && canPreviewInPanel && !inline && variant !== "compact") {
    return (
      <button
        type="button"
        onClick={() => onOpenPreview(attachment)}
        title={attachment.path ?? attachment.name ?? undefined}
        aria-label={t("documentPreview.openInPanel", {
          defaultValue: "预览 {name}",
          name: attachment.name ?? label,
        })}
        className={cn(
          "flex max-w-[18rem] items-center gap-2 rounded-[14px]",
          "border border-border/60 bg-muted/40 px-3 py-2 text-xs text-muted-foreground",
          "transition-colors hover:bg-muted/55 hover:text-foreground",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          className,
        )}
      >
        {body}
      </button>
    );
  }

  if (hasUrl && !failed) {
    return (
      <a
        href={attachment.url}
        download={attachment.name ?? label}
        onClick={handleDownloadClick}
        aria-busy={downloading}
        title={attachment.name ?? undefined}
        aria-label={label}
        className={cn(
          "flex max-w-[18rem] items-center gap-2 rounded-[14px]",
          "border border-border/60 bg-muted/40 px-3 py-2 text-xs text-muted-foreground",
          "transition-colors hover:bg-muted/55 hover:text-foreground",
          variant === "compact" && "max-w-[14rem] rounded-xl px-2.5 py-1.5 text-[11.5px]",
          className,
        )}
      >
        {body}
      </a>
    );
  }

  return (
    <div
      className={cn(
        "flex max-w-[18rem] items-center gap-2 rounded-[14px]",
        "border border-border/60 bg-muted/35 px-3 py-2 text-xs text-muted-foreground",
        variant === "compact" && "max-w-[14rem] rounded-xl px-2.5 py-1.5 text-[11.5px]",
        className,
      )}
      title={attachment.name ?? undefined}
      aria-label={label}
    >
      {body}
      <span className="sr-only">
        {t("message.attachmentUnavailable", { defaultValue: "Attachment unavailable" })}
      </span>
    </div>
  );
}

function AttachmentFrame({
  attachment,
  children,
  className,
  inline = false,
  variant = "default",
}: {
  attachment: UIMediaAttachment;
  children: ReactNode;
  className?: string;
  inline?: boolean;
  variant?: "default" | "compact";
}) {
  const frameClassName = cn(
    "not-prose my-3 block w-fit max-w-full overflow-hidden rounded-[14px]",
    "border border-border/60 bg-muted/40",
    attachment.kind === "image" && "bg-background/85",
    attachment.kind === "video" ? "w-[min(100%,32rem)]" : "",
    variant === "compact" && "my-1 rounded-xl shadow-none",
    variant === "compact" && attachment.kind === "video" && "w-[min(100%,20rem)]",
    className,
  );
  const bodyClassName = "block max-w-full";
  const body = inline ? (
    <span className={bodyClassName}>{children}</span>
  ) : (
    <div className={bodyClassName}>{children}</div>
  );
  return inline ? (
    <span className={frameClassName}>
      {body}
    </span>
  ) : (
    <figure className={frameClassName}>
      {body}
    </figure>
  );
}

function attachmentLabel(attachment: UIMediaAttachment, t: ReturnType<typeof useTranslation>["t"]): string {
  if (attachment.kind === "video") {
    return t("message.videoAttachment", { defaultValue: "Video attachment" });
  }
  if (attachment.kind === "image") {
    return t("message.imageAttachment", { defaultValue: "Image attachment" });
  }
  return t("message.fileAttachment", { defaultValue: "File attachment" });
}
