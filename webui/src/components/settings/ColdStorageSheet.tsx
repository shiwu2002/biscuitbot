import { useEffect, useState } from "react";
import { Archive, Loader2, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { fetchColdStorage } from "@/lib/api";
import type { ColdStorageEntry } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

/** 冷门仓库抽屉：列出长期未被调用、已被轮转出活跃索引的工具（只读）。
 *
 * 没有「复活」按钮——恢复的既有路径是让 agent 调用那个工具，
 * 由 ``UsageStats.record_call`` 自动把它移出冷存储。
 */
export function ColdStorageSheet({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { token } = useClient();
  const { t } = useTranslation();
  const [entries, setEntries] = useState<ColdStorageEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  // hooks 全部在条件渲染之前——React #310 的老坑。
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setLoadFailed(false);
    fetchColdStorage(token)
      .then((payload) => {
        if (!cancelled) setEntries(payload.entries);
      })
      .catch(() => {
        if (!cancelled) setLoadFailed(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, token, reloadKey]);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-[min(34rem,calc(100vw-1rem))] max-w-none gap-0 overflow-hidden p-0 sm:max-w-none"
      >
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5" data-testid="cold-storage-sheet">
          <div className="flex items-start gap-3 pr-8">
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-[15px] bg-muted/70 text-muted-foreground">
              <Archive className="h-5 w-5" aria-hidden />
            </div>
            <div className="min-w-0">
              <SheetTitle className="truncate text-[20px] font-semibold">
                {t("settings.capabilities.coldStorage", { defaultValue: "冷门仓库" })}
              </SheetTitle>
              <SheetDescription className="mt-1 text-[12.5px] leading-5 text-muted-foreground">
                {t("settings.capabilities.coldStorageDescription", {
                  defaultValue:
                    "长期未被调用的工具会被轮转到此处：schema 不再发送给模型，调用它即可自动恢复。",
                })}
              </SheetDescription>
            </div>
          </div>

          <div className="mt-5 flex items-center justify-between gap-3">
            <span className="text-[12px] font-medium text-muted-foreground">
              {t("settings.capabilities.coldStorageCount", {
                count: entries.length,
                defaultValue: "{{count}} 个工具在冷门仓库",
              })}
            </span>
            <button
              type="button"
              data-testid="cold-storage-refresh"
              onClick={() => setReloadKey((key) => key + 1)}
              className="flex h-8 items-center gap-1.5 rounded-full border border-[hsl(var(--cyber-panel-border)/0.4)] px-3 text-[12px] font-medium text-muted-foreground transition-[color,background-color,box-shadow] duration-200 hover:bg-[hsl(var(--cyber-glow)/0.08)] hover:text-foreground"
            >
              <RotateCcw className="h-3.5 w-3.5" aria-hidden />
              {t("settings.capabilities.coldStorageRefresh", { defaultValue: "刷新" })}
            </button>
          </div>

          {loading ? (
            <div className="mt-6 flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              {t("settings.capabilities.coldStorageLoading", { defaultValue: "正在加载冷门仓库..." })}
            </div>
          ) : loadFailed ? (
            <div className="mt-6 rounded-[16px] bg-destructive/10 px-3 py-3 text-sm text-destructive">
              {t("settings.capabilities.coldStorageLoadFailed", {
                defaultValue: "冷门仓库加载失败。",
              })}
            </div>
          ) : entries.length ? (
            <div className="mt-3 space-y-1">
              {entries.map((entry) => (
                <div
                  key={entry.name}
                  data-testid="cold-storage-row"
                  className="flex min-w-0 items-start gap-3 rounded-[16px] px-3 py-3 transition-[background-color,box-shadow] duration-200 hover:bg-[hsl(var(--cyber-glow)/0.06)] hover:shadow-[0_0_0_1px_hsl(var(--cyber-glow)/0.22)]"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="truncate font-mono text-[13px] font-medium">{entry.name}</span>
                      <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
                        {entry.cold_days > 0
                          ? t("settings.capabilities.coldStorageColdDays", {
                              days: entry.cold_days,
                              defaultValue: "已冷落 {{days}} 天",
                            })
                          : t("settings.capabilities.coldStorageToday", { defaultValue: "刚刚转入" })}
                      </span>
                    </div>
                    {entry.capability ? (
                      <p className="mt-1 line-clamp-2 break-words text-[13px] leading-5 text-muted-foreground">
                        {entry.capability}
                      </p>
                    ) : null}
                    {entry.source_file ? (
                      <p className="mt-0.5 break-words font-mono text-[11px] text-muted-foreground/70">
                        {entry.source_file}
                      </p>
                    ) : null}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="mt-6 px-3 py-12 text-center text-sm text-muted-foreground">
              {t("settings.capabilities.coldStorageEmpty", {
                defaultValue: "冷门仓库为空。所有工具都在活跃索引中。",
              })}
            </div>
          )}

          <p className="mt-6 text-[12px] leading-5 text-muted-foreground/80">
            {t("settings.capabilities.coldStorageReviveHint", {
              defaultValue: "让夏奈调用其中某个工具，它会自动恢复到活跃索引。",
            })}
          </p>
        </div>
      </SheetContent>
    </Sheet>
  );
}
