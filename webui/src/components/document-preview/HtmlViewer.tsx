import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";

/**
 * HTML 预览。解码后的原始 HTML 通过 `srcDoc` 注入 `sandbox="allow-scripts"` iframe：
 *
 * - 允许页面运行自身脚本 —— 智能体生成的交互式界面（地图 / 仪表盘等 JS 渲染页）
 *   据此可以真正显示，而不是空白。
 * - **不**加 `allow-same-origin`：iframe 处于不透明来源，无法访问宿主 app 的
 *   DOM / localStorage / 同源 fetch，脚本也永远脱离 React 树。
 * - 已知限制：相对路径的资源（`<link>/<script src>/.png`）无法解析 —— 因为 iframe
 *   没有 document baseURL 且来源不透明。页面须为自包含单文件（内联 CSS/JS）；
 *   CDN 引用的脚本/样式仍可加载（沙箱不拦截网络，只隔离宿主同源）。
 */
export function HtmlViewer({
  src,
  name,
  className,
}: {
  src: string;
  name?: string;
  className?: string;
}) {
  const { t } = useTranslation();
  return (
    <iframe
      title={name ?? t("documentPreview.htmlTitle", { defaultValue: "HTML 预览" })}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
      srcDoc={src}
      className={cn("block w-full bg-background", className ?? "h-[32rem]")}
    />
  );
}
