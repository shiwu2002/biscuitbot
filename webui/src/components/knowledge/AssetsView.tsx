import { useEffect, useState } from "react";
import {
  ImageIcon,
  Loader2,
  Package,
  Trash2,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { ImageLightbox } from "@/components/ImageLightbox";
import { Button } from "@/components/ui/button";
import { deleteAsset, fetchAssets } from "@/lib/api";
import type { Asset, UIImage } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

const KIND_BADGE: Record<Asset["kind"], string> = {
  image: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
  video: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
  audio: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
};

const KIND_FALLBACK: Record<Asset["kind"], string> = {
  image: "图片",
  video: "视频",
  audio: "音频",
};

function ImageThumb({
  asset,
  onOpen,
}: {
  asset: Asset;
  onOpen: () => void;
}) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <div className="flex aspect-square w-full flex-col items-center justify-center gap-1.5 bg-muted/40 px-3 text-center text-muted-foreground">
        <ImageIcon className="h-5 w-5 opacity-50" aria-hidden />
        <span className="truncate text-[11px]">{asset.name}</span>
      </div>
    );
  }
  return (
    <button
      type="button"
      onClick={onOpen}
      className="group block w-full"
      aria-label={`${asset.name}`}
    >
      <img
        src={asset.media_url}
        alt={asset.caption || asset.name}
        loading="lazy"
        onError={() => setFailed(true)}
        className="aspect-square w-full object-cover transition-transform group-hover:scale-[1.02]"
      />
    </button>
  );
}

/**
 * 生成资产视图（知识库页「资产」Tab）：展示智能体生成的图片/视频/音频，
 * 支持点击放大预览与删除清理。
 */
export function AssetsView() {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const { token } = useClient();

  const [assets, setAssets] = useState<Asset[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const payload = await fetchAssets(token);
        if (!cancelled) setAssets(payload.assets);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const handleDelete = async (asset: Asset) => {
    if (deleting) return;
    const ok = window.confirm(
      t("knowledge.assets.deleteConfirm", {
        name: asset.name,
        defaultValue: "确认删除「{{name}}」？",
      }),
    );
    if (!ok) return;
    setDeleting(asset.id);
    try {
      const payload = await deleteAsset(token, asset.id);
      setAssets(payload.assets);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDeleting(null);
    }
  };

  const lightboxImages: UIImage[] = (assets ?? [])
    .filter((a) => a.kind === "image")
    .map((a) => ({ url: a.media_url, name: a.name }));

  return (
    <div>
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

      {!loading && (assets?.length ?? 0) === 0 ? (
        <div className="cyber-glass-panel relative flex h-48 flex-col items-center justify-center gap-2 overflow-hidden rounded-[24px] border border-dashed border-border/60 text-sm text-muted-foreground">
          <Package className="h-8 w-8 opacity-40" aria-hidden />
          {tx(
            "knowledge.assets.empty",
            "还没有生成任何资产。智能体生成的图片、视频、音频会出现在这里。",
          )}
        </div>
      ) : null}

      {!loading && assets && assets.length > 0 ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          {assets.map((asset) => (
            <div
              key={asset.id}
              className="cyber-glass-panel relative overflow-hidden rounded-[18px]"
            >
              {asset.kind === "image" ? (
                <ImageThumb
                  asset={asset}
                  onOpen={() =>
                    setLightboxIndex(
                      lightboxImages.findIndex((im) => im.url === asset.media_url),
                    )
                  }
                />
              ) : asset.kind === "video" ? (
                <video
                  src={asset.media_url}
                  controls
                  preload="metadata"
                  className="aspect-video w-full bg-black"
                />
              ) : (
                <div className="flex items-center justify-center bg-muted/40 px-4 py-6">
                  <audio src={asset.media_url} controls className="w-full" />
                </div>
              )}

              <div className="flex flex-col gap-2 px-3 py-2.5">
                <div className="min-w-0">
                  <div className="truncate text-[12.5px] font-medium leading-5 text-foreground">
                    {asset.name}
                  </div>
                  <div className="mt-0.5 flex items-center gap-2 text-[11px] leading-4 text-muted-foreground">
                    <span
                      className={cn(
                        "rounded-full px-1.5 py-0.5 text-[10px] font-semibold leading-none",
                        KIND_BADGE[asset.kind],
                      )}
                    >
                      {tx(`knowledge.assets.kind.${asset.kind}`, KIND_FALLBACK[asset.kind])}
                    </span>
                    <span>{formatBytes(asset.size)}</span>
                  </div>
                </div>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => handleDelete(asset)}
                  disabled={deleting === asset.id}
                  className="h-8 rounded-[9px] text-[12.5px] text-destructive"
                >
                  {deleting === asset.id ? (
                    <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
                  ) : (
                    <Trash2 className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                  )}
                  {tx("knowledge.delete", "删除")}
                </Button>
              </div>
            </div>
          ))}
        </div>
      ) : null}

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
