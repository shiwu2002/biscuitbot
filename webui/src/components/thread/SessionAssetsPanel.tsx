/**
 * 会话资产抽屉面板。
 *
 * 展示智能体给这个会话产出的图片/文档/视频/音频，两种口径在内部切换（见
 * ``AssetsView``）：默认「本会话」（写过的 ∪ 交付给你的），可切到「整个工作区」。
 * 扫描根与台账都由后端按会话键解析（会话可单独指定项目目录），所以这里只传
 * sessionKey，不传路径 —— 路径是服务端的事，前端拿不到也不该知道。
 */

import { Package } from "lucide-react";
import { useTranslation } from "react-i18next";

import { AssetsView } from "@/components/knowledge/AssetsView";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import type { Asset } from "@/lib/types";

interface SessionAssetsPanelProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 当前会话键（如 ``websocket:<chatId>``）。 */
  sessionKey: string;
  /** 点开一个文档类资产：交给会话视图关掉抽屉、在右侧预览框里打开它。 */
  onOpenAsset: (asset: Asset) => void;
}

export function SessionAssetsPanel({
  open,
  onOpenChange,
  sessionKey,
  onOpenAsset,
}: SessionAssetsPanelProps) {
  const { t } = useTranslation();

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col sm:max-w-lg">
        <div className="flex items-center justify-between border-b px-4 py-3 pr-12">
          <SheetTitle className="flex items-center gap-2">
            <Package className="h-4 w-4" aria-hidden />
            {t("thread.assets.title", { defaultValue: "会话资产" })}
          </SheetTitle>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          {/* sessionKey 变了就重挂：扫的是另一个工作区，别把上一个会话的资产
              留在屏幕上（AssetsView 内部也有 key 依赖的 effect，双保险）。 */}
          <AssetsView key={sessionKey} sessionKey={sessionKey} onOpenAsset={onOpenAsset} />
        </div>
      </SheetContent>
    </Sheet>
  );
}
