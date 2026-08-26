import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { PreviewError } from "./PreviewStates";

/**
 * Word (.docx) 预览。用 `docx-preview` 的 `renderAsync` 把文档渲染进容器 DOM。
 *
 * 关键点：`renderAsync` 会把渲染后的正文**和注入的 `<style>`** 都追加进容器，
 * 所以卸载时必须清空 `innerHTML`，否则样式会泄漏到后续预览、卸载后还有竞态
 * 追加。库通过动态 import 按需加载。
 */
export function DocxViewer({ buffer, url }: { buffer: ArrayBuffer; url?: string }) {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let cancelled = false;
    setError(false);
    (async () => {
      try {
        const { renderAsync } = await import("docx-preview");
        if (cancelled) return;
        await renderAsync(buffer, container, undefined, {
          inWrapper: true,
          ignoreWidth: false,
          breakPages: false,
          className: "docx-preview",
        });
      } catch {
        if (!cancelled) setError(true);
      }
    })();
    return () => {
      cancelled = true;
      container.innerHTML = "";
    };
  }, [buffer]);

  if (error) {
    return (
      <PreviewError
        url={url}
        message={t("documentPreview.loadFailed", { defaultValue: "预览失败，请下载查看" })}
      />
    );
  }

  return (
    <div className="max-h-[36rem] overflow-y-auto">
      <div ref={containerRef} className="px-4 py-3" />
    </div>
  );
}
