import { useCallback, useEffect, useMemo, useState } from "react";
import { Check, ChevronLeft, Loader2, Plus, Terminal } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { fetchTalentCatalog, installTalentEmployee } from "@/lib/api";
import type {
  Employee,
  TalentCatalogPayload,
  TalentEmployee,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

/** 目录自动刷新间隔（毫秒）：30 分钟。 */
const REFRESH_INTERVAL_MS = 30 * 60 * 1000;

function talentErrorMessage(tx: (key: string, fallback: string) => string, error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return tx("talentMarket.loadFailed", "加载人才市场失败，请检查注册表地址");
}

export function TalentMarketView({
  employees,
  onInstalled,
  onBackToChat,
  hostChromeInset,
}: {
  employees: Employee[];
  onInstalled: () => void;
  onBackToChat: () => void;
  hostChromeInset?: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const { token } = useClient();

  const [catalog, setCatalog] = useState<TalentCatalogPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [message, setMessage] = useState<{ text: string; isError: boolean } | null>(
    null,
  );

  const installedIds = useMemo(
    () => new Set(employees.map((employee) => employee.id)),
    [employees],
  );

  const loadCatalog = useCallback(
    async (opts: { forceRefresh?: boolean; silent?: boolean } = {}) => {
      const { forceRefresh = false, silent = false } = opts;
      if (!silent) {
        setLoading(true);
        setMessage(null);
      }
      try {
        const payload = await fetchTalentCatalog(token, "", forceRefresh);
        setCatalog(payload);
        if (silent) {
          // 后台自动刷新：成功后清除旧的错误提示；失败则保留旧目录并提示
          setMessage(null);
        } else if (payload.configured && payload.employees.length > 0) {
          setMessage({
            text: t("talentMarket.loaded", {
              count: payload.employees.length,
              defaultValue: "已加载 {{count}} 名可招聘的数字员工",
            }),
            isError: false,
          });
        } else {
          setMessage(null);
        }
      } catch (error) {
        setMessage({
          text: talentErrorMessage(tx, error),
          isError: true,
        });
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [token, t, tx],
  );

  // 进入页面立即加载一次；之后每 30 分钟自动刷新
  // （注册表 URL 由后台配置，前端无需也不可输入）
  useEffect(() => {
    void loadCatalog({});
    const timer = window.setInterval(() => {
      void loadCatalog({ silent: true });
    }, REFRESH_INTERVAL_MS);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleInstall = async (entry: TalentEmployee) => {
    if (!entry.system_prompt?.trim()) return;
    setBusyKey(`install:${entry.id}`);
    setMessage(null);
    try {
      const result = await installTalentEmployee(
        token,
        {
          id: entry.id,
          name: entry.name,
          avatar: entry.avatar,
          system_prompt: entry.system_prompt,
          skills: entry.skills ?? [],
        },
        catalog?.source_url,
      );
      setCatalog((prev) => {
        if (!prev) return prev;
        const employees_ = prev.employees.map((item) =>
          item.id === entry.id ? { ...item, installed: true } : item,
        );
        return {
          ...prev,
          employees: employees_,
          installed_count: employees_.filter((item) => item.installed).length,
        };
      });
      onInstalled();
      setMessage({
        text: result.already_existed
          ? tx("talentMarket.alreadyAdded", "该员工已添加")
          : t("talentMarket.added", {
              name: entry.name,
              defaultValue: "已添加数字员工「{{name}}」",
            }),
        isError: false,
      });
    } catch (error) {
      setMessage({
        text: talentErrorMessage(tx, error),
        isError: true,
      });
    } finally {
      setBusyKey(null);
    }
  };

  const configured = catalog ? catalog.configured : true;
  const rows = catalog?.employees ?? [];
  const hasCatalog = catalog !== null;
  const showStatus = message !== null;

  return (
    <main className="min-w-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]">
      <div
        className={cn(
          "mx-auto w-full max-w-[920px] px-4 py-6 sm:px-8 sm:py-8 lg:py-12",
          hostChromeInset && "pt-11 sm:pt-11 lg:pt-11",
        )}
      >
        <div className="mb-7">
          <button
            type="button"
            onClick={onBackToChat}
            className="mb-4 inline-flex items-center gap-1.5 rounded-full px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground lg:hidden"
          >
            <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
            {t("settings.backToChat")}
          </button>
          <h1 className="text-[24px] font-normal leading-tight tracking-tight text-foreground sm:text-[28px]">
            {tx("talentMarket.title", "人才市场")}
          </h1>
          <p className="mt-2 max-w-[680px] text-[13px] leading-5 text-muted-foreground">
            {tx(
              "talentMarket.description",
              "展示后台配置的注册表目录（http/https JSON），查看可招聘的数字员工并一键下载为数字人员工。注册表地址由管理员通过 CLI 配置，此处只读，每 30 分钟自动刷新。",
            )}
          </p>
        </div>

        <div className="space-y-7">
          {configured && hasCatalog && rows.length > 0 ? (
            <p className="text-[12px] font-medium text-muted-foreground">
              {t("talentMarket.summary", {
                count: rows.length,
                defaultValue: "共 {{count}} 名员工 · 每 30 分钟自动刷新",
              })}
            </p>
          ) : null}

          {showStatus ? (
            <div
              className={cn(
                "flex items-center justify-between gap-3 rounded-[12px] border py-2.5 pl-4 pr-2 text-[13px]",
                message.isError
                  ? "border-destructive/20 bg-destructive/5 text-destructive"
                  : "border-border/55 bg-muted/35 text-muted-foreground",
              )}
            >
              <span>{message.text}</span>
            </div>
          ) : null}

          {loading && !hasCatalog ? (
            <div className="cyber-glass-panel relative flex h-48 items-center justify-center overflow-hidden rounded-[24px] border border-border/50 text-sm text-muted-foreground">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
              {t("settings.status.loading")}
            </div>
          ) : hasCatalog && !catalog.configured ? (
            <div className="cyber-glass-panel relative overflow-hidden rounded-[16px] border border-dashed border-border/60 p-8 text-center">
              <Terminal
                className="mx-auto mb-3 h-7 w-7 text-muted-foreground/60"
                aria-hidden
              />
              <h2 className="text-[14px] font-semibold text-foreground">
                {tx("talentMarket.notConfiguredTitle", "人才市场尚未配置")}
              </h2>
              <p className="mx-auto mt-2 max-w-[520px] text-[13px] leading-5 text-muted-foreground">
                {tx(
                  "talentMarket.notConfiguredHint",
                  "注册表地址需要在后台通过 CLI 配置，应用内不可更改。",
                )}
              </p>
              <pre className="mx-auto mt-4 inline-block rounded-[10px] bg-muted/70 px-4 py-2 font-mono text-[12.5px] leading-6 text-foreground/80">
                biscuitbot talent-market set{" "}
                <span className="text-muted-foreground">
                  {"<http(s)://.../employees.json>"}
                </span>
              </pre>
            </div>
          ) : hasCatalog && rows.length === 0 ? (
            <div className="cyber-glass-panel relative flex h-48 items-center justify-center overflow-hidden rounded-[24px] border border-dashed border-border/60 text-sm text-muted-foreground">
              {tx("talentMarket.noEmployees", "该注册表中没有可招聘的员工")}
            </div>
          ) : hasCatalog ? (
            <div className="cyber-glass-panel relative divide-y divide-border/50 overflow-hidden rounded-[16px] border border-border/60">
              {rows.map((entry) => {
                const alreadyInstalled = Boolean(entry.installed || installedIds.has(entry.id));
                const missingPersona = !entry.system_prompt?.trim();
                const installBusy = busyKey === `install:${entry.id}`;
                return (
                  <article
                    key={entry.id}
                    className="group flex min-w-0 items-center gap-3 rounded-none px-4 py-3 transition-colors hover:bg-muted/45"
                  >
                    <span
                      className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[12px] bg-muted/70 text-[18px] leading-none"
                      aria-hidden
                    >
                      {entry.avatar || "🧑‍💼"}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex min-w-0 items-baseline gap-2">
                        <h3 className="truncate text-[14px] font-semibold leading-5 text-foreground">
                          {entry.name}
                        </h3>
                        {entry.category ? (
                          <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-[10.5px] font-medium text-muted-foreground/80">
                            {entry.category}
                          </span>
                        ) : null}
                      </div>
                      <p className="mt-0.5 truncate text-[12.5px] leading-5 text-muted-foreground">
                        {entry.description || tx("talentMarket.noDescription", "暂无简介")}
                      </p>
                      {entry.skills && entry.skills.length > 0 ? (
                        <div className="mt-1.5 flex flex-wrap gap-1">
                          {entry.skills.slice(0, 6).map((skill) => (
                            <span
                              key={skill}
                              className="rounded-full bg-muted px-2 py-0.5 text-[10.5px] font-medium text-muted-foreground/80"
                            >
                              {skill}
                            </span>
                          ))}
                        </div>
                      ) : null}
                    </div>
                    <div className="flex shrink-0 items-center">
                      {alreadyInstalled ? (
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-muted px-3 py-1.5 text-[12px] font-medium text-muted-foreground">
                          <Check className="h-3.5 w-3.5" aria-hidden />
                          {tx("talentMarket.added", "已添加")}
                        </span>
                      ) : (
                        <Button
                          type="button"
                          size="sm"
                          variant={missingPersona ? "ghost" : "default"}
                          disabled={missingPersona || installBusy}
                          title={
                            missingPersona
                              ? tx("talentMarket.missingPersona", "该条目缺少角色提示词，无法添加")
                              : undefined
                          }
                          onClick={() => void handleInstall(entry)}
                          className="h-9 rounded-[10px] px-4"
                        >
                          {installBusy ? (
                            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
                          ) : (
                            <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                          )}
                          {tx("talentMarket.install", "下载/添加")}
                        </Button>
                      )}
                    </div>
                  </article>
                );
              })}
            </div>
          ) : null}
        </div>
      </div>
    </main>
  );
}
