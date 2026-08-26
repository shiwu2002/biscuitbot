import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

/**
 * HTML 预览。原始 HTML 通过 `srcDoc` 注入 `sandbox=""` iframe：
 * 无脚本、无同源访问、无顶部导航，绝不进入 React 树，脚本永远不执行。
 *
 * 已知 v1 限制：相对路径的图片/CSS 无法解析（sandbox 无同源），按原样展示
 * 文本与内联样式。
 */
export function HtmlViewer({ buffer, name }: { buffer: ArrayBuffer; name?: string }) {
  const { t } = useTranslation();
  const [src, setSrc] = useState<string>("");

  useEffect(() => {
    setSrc(new TextDecoder("utf-8", { fatal: false }).decode(buffer));
  }, [buffer]);

  return (
    <iframe
      title={name ?? t("documentPreview.htmlTitle", { defaultValue: "HTML 预览" })}
      sandbox=""
      referrerPolicy="no-referrer"
      srcDoc={src}
      className="h-[32rem] w-full bg-background"
    />
  );
}
