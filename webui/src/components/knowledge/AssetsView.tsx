import { useEffect, useState } from "react";
import {
  FileText,
  ImageIcon,
  Loader2,
  Package,
  Trash2,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { ImageLightbox } from "@/components/ImageLightbox";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { AudioCard } from "@/components/knowledge/AudioCard";
import { VideoCard } from "@/components/knowledge/VideoCard";
import { deleteAsset, fetchAssets, type AssetsScope } from "@/lib/api";
import { fmtDateTime, relativeTime } from "@/lib/format";
import type { Asset, UIImage } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

type FilterKind = "all" | Asset["kind"];

const FILTERS: { key: FilterKind; labelKey: string; fallback: string }[] = [
  { key: "all", labelKey: "knowledge.assets.filter.all", fallback: "全部" },
  { key: "image", labelKey: "knowledge.assets.kind.image", fallback: "图片" },
  { key: "document", labelKey: "knowledge.assets.kind.document", fallback: "文档" },
  { key: "video", labelKey: "knowledge.assets.kind.video", fallback: "视频" },
  { key: "audio", labelKey: "knowledge.assets.kind.audio", fallback: "音频" },
];

const SCOPES: { key: AssetsScope; labelKey: string; fallback: string }[] = [
  { key: "session", labelKey: "knowledge.assets.scope.session", fallback: "本会话" },
  { key: "workspace", labelKey: "knowledge.assets.scope.workspace", fallback: "整个工作区" },
];

const SCOPE_HINT_KEY: Record<AssetsScope, string> = {
  session: "knowledge.assets.scope.sessionHint",
  workspace: "knowledge.assets.scope.workspaceHint",
};

const SCOPE_HINT_FALLBACK: Record<AssetsScope, string> = {
  session: "智能体在本会话里写过的、交付给你的文件。",
  workspace: "这个会话的工作区里全部的产出。",
};

// 渠道入站媒体的渠道名 → 中文展示名（未知渠道回退为原始 id）。
const CHANNEL_LABEL: Record<string, string> = {
  weixin: "微信",
  feishu: "飞书",
  dingtalk: "钉钉",
  qq: "QQ",
  napcat: "QQ",
  email: "邮件",
  wecom: "企业微信",
  websocket: "WebSocket",
};

/** 文件名切成「主干 + 后缀」：卡面上后缀要始终露出来，不能被 truncate 吃掉。 */
function splitAssetName(name: string): { stem: string; ext: string } {
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return { stem: name, ext: "" };
  return { stem: name.slice(0, dot), ext: name.slice(dot + 1).toUpperCase() };
}

/** 图片卡面：铺满裁切 + 悬停放大，有 caption（生成的提示词）时在底部浮出。 */
function ImageTile({ asset }: { asset: Asset }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-muted/40 text-muted-foreground">
        <ImageIcon className="h-5 w-5 opacity-50" aria-hidden />
      </div>
    );
  }
  return (
    <div className="relative h-full w-full overflow-hidden bg-muted/40">
      <img
        src={asset.media_url}
        alt={asset.caption || asset.name}
        loading="lazy"
        onError={() => setFailed(true)}
        className="h-full w-full object-cover transition-transform duration-300 ease-out group-hover:scale-[1.04]"
      />
      {asset.caption ? (
        <div className="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/70 via-black/30 to-transparent px-2.5 pb-2 pt-7 opacity-0 transition-opacity duration-200 group-hover:opacity-100">
          <p className="line-clamp-2 text-[10.5px] leading-4 text-white/95">{asset.caption}</p>
        </div>
      ) : null}
    </div>
  );
}

/** 文档卡面：图标 + 后缀大字，让 docx/xlsx/zip 在缩略图网格里一眼可辨。 */
function DocumentTile({ asset }: { asset: Asset }) {
  const { ext } = splitAssetName(asset.name);
  return (
    <div className="flex h-full w-full flex-col items-center justify-center gap-2 bg-gradient-to-br from-violet-500/10 via-muted/30 to-muted/50 transition-colors duration-200 group-hover:from-violet-500/18">
      <div className="flex h-10 w-10 items-center justify-center rounded-[12px] bg-background/75 text-violet-600 shadow-sm ring-1 ring-border/60 dark:text-violet-300">
        <FileText className="h-5 w-5" aria-hidden />
      </div>
      <span className="rounded-full bg-background/70 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-muted-foreground ring-1 ring-border/50">
        {ext || "FILE"}
      </span>
    </div>
  );
}

/**
 * 会话资产视图：展示该会话产出的图片/文档/视频/音频（由会话视图的资产面板承载），
 * 支持两种口径切换、分类筛选、点击放大预览与删除清理。
 *
 * - **本会话**（默认）：智能体在本会话里写过的 + 交付给你的文件。口径由会话记录
 *   派生（``file_edit`` / 出站 ``media``），与工作区是否被多个会话共用无关。
 * - **整个工作区**：扫描会话工作区里所有产出 —— 会话不设独立工作区时，各会话看到
 *   的是同一份清单。
 */
export function AssetsView({
  sessionKey,
  onOpenAsset,
}: {
  sessionKey: string;
  /** 点开文档类资产（含预览不了的交付件）：交给宿主在**右侧预览框**里打开。
   *
   * 不在卡片里就地渲染预览，是为了和聊天里的附件/文件引用一致：同一个右侧预览框，
   * 预览不了时给出「在文件夹中显示」/下载 —— 交付件的意义就是最终能拿到那个文件。
   */
  onOpenAsset: (asset: Asset) => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const { token } = useClient();

  const [assets, setAssets] = useState<Asset[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);
  const [filter, setFilter] = useState<FilterKind>("all");
  const [channelFilter, setChannelFilter] = useState<string>("all");
  const [pendingAsset, setPendingAsset] = useState<Asset | null>(null);
  const [scope, setScope] = useState<AssetsScope>("session");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const payload = await fetchAssets(token, sessionKey, scope);
        if (!cancelled) {
          setAssets(payload.assets);
          setTruncated(Boolean(payload.truncated));
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token, sessionKey, scope]);

  const handleDelete = async () => {
    const asset = pendingAsset;
    if (!asset || deleting) return;
    setDeleting(asset.id);
    try {
      const payload = await deleteAsset(token, sessionKey, asset.id, scope);
      setAssets(payload.assets);
      setTruncated(Boolean(payload.truncated));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDeleting(null);
      setPendingAsset(null);
    }
  };

  const lightboxImages: UIImage[] = (assets ?? [])
    .filter((a) => a.kind === "image")
    .map((a) => ({ url: a.media_url, name: a.name }));

  const channelOptions = Array.from(
    new Set(
      (assets ?? [])
        .map((a) => a.channel)
        .filter((c): c is string => typeof c === "string" && c.length > 0),
    ),
  ).sort();

  const visibleAssets = (assets ?? []).filter(
    (a) =>
      (filter === "all" || a.kind === filter) &&
      (channelFilter === "all" || a.channel === channelFilter),
  );

  return (
    <div data-testid="assets-view">
      <div className="mb-4 flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <div
          role="tablist"
          aria-label={tx("knowledge.assets.scope.label", "资产范围")}
          className="inline-flex rounded-[12px] border border-border/60 bg-muted/40 p-0.5"
        >
          {SCOPES.map((item) => (
            <button
              key={item.key}
              type="button"
              role="tab"
              aria-selected={scope === item.key}
              onClick={() => setScope(item.key)}
              className={cn(
                "rounded-[10px] px-3 py-1.5 text-[13px] font-medium transition-[color,background-color,box-shadow] duration-200",
                scope === item.key
                  ? "bg-background text-foreground shadow-[0_0_0_1px_hsl(var(--cyber-glow)/0.42)]"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {tx(item.labelKey, item.fallback)}
            </button>
          ))}
        </div>
        <span className="text-[11.5px] leading-4 text-muted-foreground">
          {tx(SCOPE_HINT_KEY[scope], SCOPE_HINT_FALLBACK[scope])}
        </span>
      </div>

      {loading ? (
        <div className="cyber-glass-panel relative flex h-48 items-center justify-center overflow-hidden rounded-[24px] text-sm text-muted-foreground">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
          {tx("knowledge.assets.loading", "加载资产…")}
        </div>
      ) : error && !assets ? (
        <div className="cyber-glass-panel relative overflow-hidden rounded-[24px] px-5 py-4 text-sm text-muted-foreground">
          <span className="max-w-[520px]">{error}</span>
        </div>
      ) : null}

      {error && assets ? (
        <div className="mb-4 rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}

      {truncated ? (
        <div className="mb-4 rounded-[18px] border border-border/60 bg-muted/40 px-4 py-3 text-[12.5px] text-muted-foreground">
          {tx(
            "knowledge.assets.truncated",
            "工作区文件过多，这里只显示了最近的一部分。",
          )}
        </div>
      ) : null}

      {!loading && (assets?.length ?? 0) === 0 ? (
        <div className="cyber-glass-panel relative flex h-48 flex-col items-center justify-center gap-2 overflow-hidden rounded-[24px] border border-dashed border-border/60 px-6 text-center text-sm text-muted-foreground">
          <Package className="h-8 w-8 opacity-40" aria-hidden />
          {scope === "session"
            ? tx(
                "knowledge.assets.emptySession",
                "这个会话还没有产出。智能体在这里写过的、交付给你的文件会出现在这里。",
              )
            : tx(
                "knowledge.assets.empty",
                "这个会话的工作区里还没有产出。智能体生成的图片、文档、视频、音频会出现在这里。",
              )}
        </div>
      ) : null}

      {!loading && assets && assets.length > 0 ? (
        <>
          <div className="mb-4 -mx-1 flex flex-wrap gap-1.5">
            {FILTERS.map((item) => (
              <button
                key={item.key}
                type="button"
                aria-current={filter === item.key ? "page" : undefined}
                onClick={() => setFilter(item.key)}
                className={cn(
                  "rounded-[10px] px-3 py-1.5 text-[13px] font-medium transition-[color,background-color,box-shadow] duration-200",
                  filter === item.key
                    ? "bg-[hsl(var(--cyber-glow)/0.12)] text-foreground shadow-[0_0_0_1px_hsl(var(--cyber-glow)/0.42),0_0_14px_hsl(var(--cyber-glow-soft)/0.25)]"
                    : "text-muted-foreground hover:bg-[hsl(var(--cyber-glow)/0.08)] hover:text-foreground",
                )}
              >
                {tx(item.labelKey, item.fallback)}
              </button>
            ))}
          </div>

          {channelOptions.length > 1 && (
            <div className="mb-4 -mx-1 flex flex-wrap items-center gap-1.5">
              <span className="px-1 text-[12px] font-medium text-muted-foreground">
                {tx("knowledge.assets.channel", "渠道")}
              </span>
              <button
                type="button"
                aria-current={channelFilter === "all" ? "page" : undefined}
                onClick={() => setChannelFilter("all")}
                className={cn(
                  "rounded-[10px] px-3 py-1.5 text-[13px] font-medium transition-[color,background-color,box-shadow] duration-200",
                  channelFilter === "all"
                    ? "bg-[hsl(var(--cyber-glow)/0.12)] text-foreground shadow-[0_0_0_1px_hsl(var(--cyber-glow)/0.42),0_0_14px_hsl(var(--cyber-glow-soft)/0.25)]"
                    : "text-muted-foreground hover:bg-[hsl(var(--cyber-glow)/0.08)] hover:text-foreground",
                )}
              >
                {tx("knowledge.assets.filter.all", "全部")}
              </button>
              {channelOptions.map((ch) => (
                <button
                  key={ch}
                  type="button"
                  aria-current={channelFilter === ch ? "page" : undefined}
                  onClick={() => setChannelFilter(ch)}
                  className={cn(
                    "rounded-[10px] px-3 py-1.5 text-[13px] font-medium transition-[color,background-color,box-shadow] duration-200",
                    channelFilter === ch
                      ? "bg-[hsl(var(--cyber-glow)/0.12)] text-foreground shadow-[0_0_0_1px_hsl(var(--cyber-glow)/0.42),0_0_14px_hsl(var(--cyber-glow-soft)/0.25)]"
                      : "text-muted-foreground hover:bg-[hsl(var(--cyber-glow)/0.08)] hover:text-foreground",
                  )}
                >
                  {CHANNEL_LABEL[ch] ?? ch}
                </button>
              ))}
            </div>
          )}

          {visibleAssets.length === 0 ? (
            <div className="cyber-glass-panel relative flex h-40 flex-col items-center justify-center gap-2 overflow-hidden rounded-[24px] border border-dashed border-border/60 text-sm text-muted-foreground">
              <Package className="h-7 w-7 opacity-40" aria-hidden />
              {tx("knowledge.assets.filterEmpty", "该分类暂无资产。")}
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              {visibleAssets.map((asset) => {
                const { stem, ext } = splitAssetName(asset.name);
                // 图片点开灯箱、文档点开右侧预览框：整张卡都是热区，吞掉缩略图那点
                // 小目标。音视频要能在卡面里就地播放，控件不能被包进按钮（嵌套无效
                // HTML），所以它们只有各自的播放键响应点击。
                const openable = asset.kind === "image" || asset.kind === "document";
                const open = () =>
                  asset.kind === "image"
                    ? setLightboxIndex(
                        lightboxImages.findIndex((im) => im.url === asset.media_url),
                      )
                    : onOpenAsset(asset);

                const tile = (
                  <div className="relative aspect-[4/3] w-full overflow-hidden">
                    {asset.kind === "image" ? (
                      <ImageTile asset={asset} />
                    ) : asset.kind === "video" ? (
                      <VideoCard asset={asset} />
                    ) : asset.kind === "document" ? (
                      <DocumentTile asset={asset} />
                    ) : (
                      <AudioCard asset={asset} />
                    )}
                  </div>
                );

                const caption = (
                  <div className="flex min-w-0 flex-1 flex-col gap-1.5 px-3 py-2.5">
                    <div
                      className="flex min-w-0 items-baseline gap-0.5"
                      title={asset.name}
                    >
                      <span className="min-w-0 truncate text-[12.5px] font-medium leading-5 text-foreground">
                        {stem}
                      </span>
                      {ext ? (
                        <span className="shrink-0 text-[10.5px] leading-5 text-muted-foreground">
                          .{ext.toLowerCase()}
                        </span>
                      ) : null}
                    </div>
                    <div className="mt-auto flex flex-wrap items-center gap-x-2 gap-y-1 text-[10.5px] leading-4 text-muted-foreground">
                      <span>{formatBytes(asset.size)}</span>
                      {asset.created_at ? (
                        <>
                          <span aria-hidden className="opacity-40">
                            ·
                          </span>
                          <span title={fmtDateTime(asset.created_at)}>
                            {relativeTime(asset.created_at)}
                          </span>
                        </>
                      ) : null}
                      {asset.channel ? (
                        <span className="rounded-full bg-blue-500/10 px-1.5 py-0.5 text-[10px] font-semibold leading-none text-blue-700 dark:text-blue-300">
                          {CHANNEL_LABEL[asset.channel] ?? asset.channel}
                        </span>
                      ) : null}
                    </div>
                  </div>
                );

                return (
                  <div
                    key={asset.id}
                    title={asset.path || asset.name}
                    className={cn(
                      "cyber-glass-panel group relative flex flex-col overflow-hidden rounded-[18px]",
                      "transition-[border-color,box-shadow] duration-200",
                      "hover:border-[hsl(var(--cyber-glow)/0.35)] hover:shadow-[0_0_0_1px_hsl(var(--cyber-glow)/0.22),0_12px_30px_-20px_hsl(var(--cyber-glow)/0.6)]",
                    )}
                  >
                    {asset.delivered ? (
                      <span
                        className="absolute left-2 top-2 z-20 rounded-full bg-emerald-500/90 px-1.5 py-0.5 text-[10px] font-semibold leading-none text-white shadow-sm backdrop-blur-sm"
                        title={tx(
                          "knowledge.assets.deliveredHint",
                          "这个文件已经在会话里交付给你了。",
                        )}
                      >
                        {tx("knowledge.assets.delivered", "已交付")}
                      </span>
                    ) : null}

                    {openable ? (
                      <button
                        type="button"
                        onClick={open}
                        aria-label={asset.name}
                        className="flex min-w-0 flex-1 flex-col rounded-[18px] text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        {tile}
                        {caption}
                      </button>
                    ) : (
                      <div className="flex min-w-0 flex-1 flex-col">
                        {tile}
                        {caption}
                      </div>
                    )}

                    <Button
                      type="button"
                      size="icon"
                      variant="outline"
                      onClick={() => setPendingAsset(asset)}
                      disabled={deleting === asset.id}
                      aria-label={tx("knowledge.delete", "删除")}
                      title={tx("knowledge.delete", "删除")}
                      className={cn(
                        "absolute right-2 top-2 z-20 h-7 w-7 rounded-full border-0 bg-background/85 p-0 text-destructive shadow-sm backdrop-blur-sm",
                        "hover:bg-destructive hover:text-destructive-foreground",
                        // hover/聚焦时显示；触屏（sm 以下）常显
                        "opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100 max-sm:opacity-100",
                        deleting === asset.id && "opacity-100",
                      )}
                    >
                      {deleting === asset.id ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                      ) : (
                        <Trash2 className="h-3.5 w-3.5" aria-hidden />
                      )}
                    </Button>
                  </div>
                );
              })}
            </div>
          )}
        </>
      ) : null}

      <AlertDialog
        open={pendingAsset !== null}
        onOpenChange={(open) => {
          if (!open) setPendingAsset(null);
        }}
      >
        <AlertDialogContent className="w-[min(calc(100vw-2rem),24rem)] gap-0 rounded-[28px] border border-white/70 bg-card/95 p-5 text-center shadow-[0_24px_80px_rgba(15,23,42,0.20)] backdrop-blur-xl data-[state=open]:zoom-in-95 sm:rounded-[28px]">
          <AlertDialogHeader className="items-center space-y-0 text-center">
            <div className="mb-5 grid h-16 w-16 place-items-center rounded-full bg-destructive/10 text-destructive">
              <div className="grid h-9 w-9 place-items-center rounded-full border border-destructive/20 bg-destructive/5">
                <Trash2 className="h-5 w-5" strokeWidth={2.4} aria-hidden />
              </div>
            </div>
            <AlertDialogTitle className="text-center text-[20px] font-semibold leading-tight tracking-[-0.02em] text-foreground">
              {t("knowledge.assets.deleteConfirm", {
                name: pendingAsset?.name ?? "",
                defaultValue: "确认删除「{{name}}」？",
              })}
            </AlertDialogTitle>
            <AlertDialogDescription className="mt-3 max-w-[17rem] text-center text-[14px] leading-6 text-muted-foreground">
              {t("deleteConfirm.description")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter className="mt-7 !grid grid-cols-1 gap-3 space-x-0 sm:grid-cols-2 sm:space-x-0">
            <AlertDialogCancel
              onClick={() => setPendingAsset(null)}
              className="mt-0 h-11 w-full min-w-0 rounded-full border-0 bg-muted/70 px-5 text-[15px] font-semibold text-foreground shadow-none hover:bg-muted"
            >
              {t("deleteConfirm.cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDelete}
              disabled={deleting !== null}
              className="h-11 w-full min-w-0 !whitespace-normal rounded-full bg-destructive px-5 text-center text-[15px] font-semibold text-destructive-foreground shadow-[0_10px_25px_rgba(239,68,68,0.28)] hover:bg-destructive/90"
            >
              {deleting !== null ? (
                <Loader2 className="mr-1.5 h-4 w-4 animate-spin" aria-hidden />
              ) : null}
              {t("deleteConfirm.confirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <ImageLightbox
        images={lightboxImages}
        index={lightboxIndex}
        onIndexChange={setLightboxIndex}
        onOpenChange={(open) => {
          if (!open) setLightboxIndex(null);
        }}
      />
    </div>
  );
}
