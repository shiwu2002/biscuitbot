import { useState, type ReactNode } from "react";
import { ChevronDown, Eye, FileIcon, ImageIcon, PlaySquare } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import { getDocumentPreviewKind } from "@/lib/file-preview";
import type { UIMediaAttachment } from "@/lib/types";
import { DocumentPreview } from "@/components/document-preview/DocumentPreview";

interface AttachmentTileProps {
  attachment: UIMediaAttachment;
  className?: string;
  inline?: boolean;
  variant?: "default" | "compact";
}

export function AttachmentTile({ attachment, className, inline = false, variant = "default" }: AttachmentTileProps) {
  const { t } = useTranslation();
  const [failed, setFailed] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const hasUrl = typeof attachment.url === "string" && attachment.url.length > 0;
  const label = attachmentLabel(attachment, t);
  // 文档预览只作用于消息媒体路径（非 inline、非 compact）；其余调用点保持下载 chip。
  const docKind = getDocumentPreviewKind(attachment.name ?? attachment.url);

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
      <Icon className="h-4 w-4 flex-none" aria-hidden />
      <span className="min-w-0 truncate">{attachment.name ?? label}</span>
    </>
  );

  // 文档类附件（pptx/docx/xlsx/csv/html）加一个「预览/收起预览」按钮，
  // 点击后才 fetch 签名 URL 的字节（懒加载），折叠即卸载渲染器。
  // inline/compact 路径保持纯下载 chip。
  if (docKind !== null && hasUrl && !failed && !inline && variant !== "compact") {
    return (
      <div className={cn("flex max-w-full flex-col items-start gap-1.5", className)}>
        <div className="flex items-center gap-1.5">
          <a
            href={attachment.url}
            download={attachment.name ?? label}
            title={attachment.name ?? undefined}
            aria-label={label}
            className={cn(
              "flex max-w-[18rem] items-center gap-2 rounded-[14px]",
              "border border-border/60 bg-muted/40 px-3 py-2 text-xs text-muted-foreground",
              "transition-colors hover:bg-muted/55 hover:text-foreground",
            )}
          >
            {body}
          </a>
          <button
            type="button"
            onClick={() => setPreviewOpen((v) => !v)}
            aria-expanded={previewOpen}
            aria-label={
              previewOpen
                ? t("documentPreview.collapse", { defaultValue: "收起预览" })
                : t("documentPreview.preview", { defaultValue: "预览" })
            }
            className={cn(
              "inline-flex items-center gap-1 rounded-[14px] px-2.5 py-2 text-xs",
              "border border-border/60 bg-muted/40 text-muted-foreground",
              "transition-colors hover:bg-muted/55 hover:text-foreground",
            )}
          >
            <Eye className="h-4 w-4" aria-hidden />
            {previewOpen
              ? t("documentPreview.collapse", { defaultValue: "收起预览" })
              : t("documentPreview.preview", { defaultValue: "预览" })}
            <ChevronDown
              className={cn("h-3.5 w-3.5 transition-transform", previewOpen && "rotate-180")}
              aria-hidden
            />
          </button>
        </div>
        {previewOpen ? <DocumentPreview attachment={attachment} kind={docKind} /> : null}
      </div>
    );
  }

  if (hasUrl && !failed) {
    return (
      <a
        href={attachment.url}
        download={attachment.name ?? label}
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
