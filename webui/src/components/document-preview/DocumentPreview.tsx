import { useEffect, useMemo, useState } from "react";
import { Maximize2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import type { UIMediaAttachment } from "@/lib/types";
import {
  fetchMediaBuffer,
  PreviewTooLargeError,
  type DocumentPreviewKind,
  type PreviewErrorCode,
} from "@/lib/file-preview";

import { DocxViewer } from "./DocxViewer";
import { FullscreenHtmlViewer } from "./FullscreenHtmlViewer";
import { HtmlViewer } from "./HtmlViewer";
import { PptxViewer } from "./PptxViewer";
import { PreviewError, PreviewFrame, PreviewLoading } from "./PreviewStates";
import { SpreadsheetViewer } from "./SpreadsheetViewer";

interface DocumentPreviewProps {
  attachment: UIMediaAttachment;
  kind: DocumentPreviewKind;
}

/**
 * 消息内文档附件预览编排器：拉取签名 URL 字节，管理 loading/ready/error
 * 状态机，再按类型分派给具体渲染器。失败时回退为下载链接。
 */
export function DocumentPreview({ attachment, kind }: DocumentPreviewProps) {
  const { t } = useTranslation();
  const url =
    typeof attachment.url === "string" && attachment.url.length > 0 ? attachment.url : "";
  const [buffer, setBuffer] = useState<ArrayBuffer | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [errorCode, setErrorCode] = useState<PreviewErrorCode>("fetch");
  const [fullscreenOpen, setFullscreenOpen] = useState(false);

  // HTML 只需解码一次，供内联预览与全屏模态共用。
  const htmlSrc = useMemo(
    () =>
      buffer && kind === "html"
        ? new TextDecoder("utf-8", { fatal: false }).decode(buffer)
        : "",
    [buffer, kind],
  );

  useEffect(() => {
    if (!url) {
      setStatus("error");
      setErrorCode("fetch");
      return;
    }
    let cancelled = false;
    setStatus("loading");
    setBuffer(null);
    fetchMediaBuffer(url)
      .then((buf) => {
        if (cancelled) return;
        setBuffer(buf);
        setStatus("ready");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setErrorCode(err instanceof PreviewTooLargeError ? "too-large" : "fetch");
        setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [url]);

  let message = t("documentPreview.loadFailed", { defaultValue: "预览失败，请下载查看" });
  if (errorCode === "too-large") {
    message = t("documentPreview.tooLarge", {
      defaultValue: "文件过大，无法预览，请下载查看",
    });
  }

  return (
    <PreviewFrame>
      {status === "loading" ? (
        <PreviewLoading label={t("documentPreview.loading", { defaultValue: "正在加载预览…" })} />
      ) : null}
      {status === "error" ? <PreviewError url={url} message={message} /> : null}
      {status === "ready" && buffer ? (
        kind === "pptx" ? (
          <PptxViewer buffer={buffer} url={url} />
        ) : kind === "docx" ? (
          <DocxViewer buffer={buffer} url={url} />
        ) : kind === "xlsx" || kind === "csv" ? (
          <SpreadsheetViewer buffer={buffer} kind={kind} url={url} />
        ) : kind === "html" ? (
          <div className="w-full">
            <div className="flex items-center justify-between gap-2 px-3 py-2">
              <span className="min-w-0 truncate text-xs text-muted-foreground">
                {attachment.name}
              </span>
              <button
                type="button"
                onClick={() => setFullscreenOpen(true)}
                aria-label={t("documentPreview.openFullscreen", { defaultValue: "全屏查看" })}
                className={cn(
                  "inline-flex flex-none items-center gap-1 rounded-md px-2 py-1 text-xs",
                  "text-muted-foreground transition-colors hover:bg-muted/55 hover:text-foreground",
                )}
              >
                <Maximize2 className="h-3.5 w-3.5" aria-hidden />
                {t("documentPreview.openFullscreen", { defaultValue: "全屏查看" })}
              </button>
            </div>
            <HtmlViewer
              src={htmlSrc}
              name={attachment.name}
              className="h-[min(72vh,44rem)] w-full"
            />
            <FullscreenHtmlViewer
              open={fullscreenOpen}
              onOpenChange={setFullscreenOpen}
              src={htmlSrc}
              name={attachment.name}
            />
          </div>
        ) : null
      ) : null}
    </PreviewFrame>
  );
}
