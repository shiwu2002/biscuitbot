import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { PreviewError, PreviewLoading } from "./PreviewStates";

/**
 * PPTX 幻灯片预览。`react-pptx-preview-kit` 的 `<PptxPreview file>` 是纯前端
 * 渲染器，自带缩放与翻页 UI（填满父容器高度）。整个库通过动态 import 按需
 * 加载，避免拖进主 bundle。
 */
export function PptxViewer({ buffer, url }: { buffer: ArrayBuffer; url?: string }) {
  const { t } = useTranslation();
  const [kit, setKit] = useState<typeof import("react-pptx-preview-kit") | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setError(false);
    import("react-pptx-preview-kit")
      .then((mod) => {
        if (!cancelled) setKit(mod);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return (
      <PreviewError
        url={url}
        message={t("documentPreview.loadFailed", { defaultValue: "预览失败，请下载查看" })}
      />
    );
  }
  if (!kit) {
    return (
      <PreviewLoading label={t("documentPreview.loading", { defaultValue: "正在加载预览…" })} />
    );
  }

  const PptxPreview = kit.PptxPreview;
  return (
    <div className="h-[34rem] overflow-hidden bg-background/70">
      <PptxPreview file={buffer} />
    </div>
  );
}
