import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { UIMediaAttachment } from "@/lib/types";
import {
  fetchMediaBuffer,
  PreviewTooLargeError,
  type DocumentPreviewKind,
  type PreviewErrorCode,
} from "@/lib/file-preview";

import { DocxViewer } from "./DocxViewer";
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
          <HtmlViewer buffer={buffer} name={attachment.name} />
        ) : null
      ) : null}
    </PreviewFrame>
  );
}
