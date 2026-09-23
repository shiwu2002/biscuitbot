import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  BadgeCheck,
  Check,
  ChevronLeft,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Terminal,
  TriangleAlert,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { EmployeeAvatar, isImageAvatar } from "@/components/employees/EmployeeAvatar";
import { Input } from "@/components/ui/input";
import {
  ApiError,
  fetchSkillHubCatalog,
  fetchSkillHubInstalled,
  fetchSkillHubStatus,
  fetchSkillHubUpdates,
  installSkillHubSkill,
  searchSkillHub,
} from "@/lib/api";
import type {
  SkillHubCatalogPayload,
  SkillHubInstalledSkill,
  SkillHubSearchPayload,
  SkillHubSkill,
  SkillHubStatus,
  SkillHubUpdatesPayload,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

/** 目录自动刷新间隔（毫秒）：30 分钟（与人才市场一致）。 */
const REFRESH_INTERVAL_MS = 30 * 60 * 1000;

/** 排行榜类型（商店支持的浏览排序）。 */
const RANKING_TYPES = ["hot", "newest", "trending", "featured", "recommended"] as const;
type RankingType = (typeof RANKING_TYPES)[number];

/** 官方安装命令：CLI 未安装时引导用户自行安装（应用内不代为执行）。 */
const INSTALL_COMMAND =
  "curl -fsSL https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/install.sh | bash";

function skillHubErrorMessage(
  tx: (key: string, fallback: string) => string,
  error: unknown,
): string {
  if (error instanceof Error && error.message) return error.message;
  return tx("skillHub.loadFailed", "加载技能商店失败，请检查网络与 SkillHub CLI 安装情况");
}

/** 技能条目的唯一键：canonical_name（``@handle/slug``）优先，缺失时退回 slug。 */
function skillKey(skill: SkillHubSkill): string {
  return skill.canonical_name || skill.slug;
}

function rankingTypeLabel(type: RankingType, t: (key: string, fallback: string) => string): string {
  switch (type) {
    case "newest":
      return t("skillHub.rankingNewest", "最新");
    case "trending":
      return t("skillHub.rankingTrending", "趋势");
    case "featured":
      return t("skillHub.rankingFeatured", "精选");
    case "recommended":
      return t("skillHub.rankingRecommended", "推荐");
    default:
      return t("skillHub.rankingHot", "热门");
  }
}

export function SkillHubView({
  onInstalled,
  onBackToChat,
  hostChromeInset,
}: {
  /** 安装成功后回调（用于刷新能力/技能列表）。 */
  onInstalled?: () => void;
  onBackToChat: () => void;
  hostChromeInset?: boolean;
}) {
  const { t } = useTranslation();
  // 稳定引用：useCallback 依赖它，若每次渲染都新建会导致加载 effect 反复触发
  const tx = useCallback(
    (key: string, fallback: string) => t(key, { defaultValue: fallback }),
    [t],
  );
  const { token } = useClient();

  const [status, setStatus] = useState<SkillHubStatus | null>(null);
  const [ranking, setRanking] = useState<SkillHubCatalogPayload | null>(null);
  const [rankingType, setRankingType] = useState<RankingType>("hot");
  const [installedSkills, setInstalledSkills] = useState<SkillHubInstalledSkill[]>([]);
  const [keyword, setKeyword] = useState("");
  const [search, setSearch] = useState<SkillHubSearchPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [message, setMessage] = useState<{ text: string; isError: boolean } | null>(null);
  const [updates, setUpdates] = useState<SkillHubUpdatesPayload | null>(null);
  const [updatesBusy, setUpdatesBusy] = useState(false);
  /** 命中 HTTP 409（目标已存在）的条目：行内提供「覆盖安装」。 */
  const [conflicts, setConflicts] = useState<Record<string, boolean>>({});

  /** 商店可用性快照；失败不阻塞页面（目录响应自身也带 available 标记）。 */
  const loadStatus = useCallback(async () => {
    try {
      setStatus(await fetchSkillHubStatus(token));
    } catch {
      // 忽略：可用性仍可由目录/搜索响应推断
    }
  }, [token]);

  const loadInstalled = useCallback(
    async (opts: { silent?: boolean } = {}) => {
      const { silent = false } = opts;
      try {
        const payload = await fetchSkillHubInstalled(token);
        setInstalledSkills(payload.skills ?? []);
      } catch (error) {
        if (!silent) {
          setMessage({ text: skillHubErrorMessage(tx, error), isError: true });
        }
      }
    },
    [token, tx],
  );

  const loadRanking = useCallback(
    async (type: RankingType, opts: { silent?: boolean } = {}) => {
      const { silent = false } = opts;
      if (!silent) {
        setLoading(true);
        setMessage(null);
      }
      try {
        setRanking(await fetchSkillHubCatalog(token, type));
      } catch (error) {
        if (!silent) {
          setMessage({ text: skillHubErrorMessage(tx, error), isError: true });
        }
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [token, tx],
  );

  // 进入页面立即加载一次（可用性 + 已安装 + 默认排行榜）
  useEffect(() => {
    void loadStatus();
    void loadInstalled();
  }, [loadStatus, loadInstalled]);

  // 排行榜类型切换后重新拉取
  useEffect(() => {
    void loadRanking(rankingType);
  }, [rankingType, loadRanking]);

  // 30 分钟静默自动刷新（用 ref 取最新的排行榜类型，避免重建定时器）
  const rankingTypeRef = useRef<RankingType>(rankingType);
  useEffect(() => {
    rankingTypeRef.current = rankingType;
  }, [rankingType]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadRanking(rankingTypeRef.current, { silent: true });
      void loadInstalled({ silent: true });
    }, REFRESH_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [loadRanking, loadInstalled]);

  /** 已安装技能的键集合（canonical_name 与 slug 都登记，兼容两种形态）。 */
  const installedKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const item of installedSkills) {
      if (item.canonical_name) keys.add(item.canonical_name);
      if (item.slug) keys.add(item.slug);
    }
    return keys;
  }, [installedSkills]);

  const handleSearch = async (event: FormEvent) => {
    event.preventDefault();
    const text = keyword.trim();
    if (!text) {
      setSearch(null);
      return;
    }
    setLoading(true);
    setMessage(null);
    try {
      const payload = await searchSkillHub(token, text);
      setSearch({ ...payload, query: payload.query || text, skills: payload.skills ?? [] });
      if ((payload.skills ?? []).length === 0) {
        setMessage({
          text: t("skillHub.searchEmpty", {
            keyword: text,
            defaultValue: "没有找到与「{{keyword}}」匹配的技能",
          }),
          isError: false,
        });
      }
    } catch (error) {
      setMessage({ text: skillHubErrorMessage(tx, error), isError: true });
    } finally {
      setLoading(false);
    }
  };

  const clearSearch = () => {
    setSearch(null);
    setKeyword("");
    setMessage(null);
  };

  const handleInstall = async (entry: SkillHubSkill, force = false) => {
    const key = skillKey(entry);
    setBusyKey(`install:${key}`);
    setMessage(null);
    try {
      const result = await installSkillHubSkill(token, {
        slug: entry.slug,
        namespace: entry.handle,
        force,
      });
      setConflicts((prev) => (prev[key] ? { ...prev, [key]: false } : prev));
      const canonical = `@${result.namespace}/${result.slug}`;
      // 乐观登记，随后由锁文件为准的列表覆盖
      setInstalledSkills((prev) => [
        ...prev.filter((item) => item.canonical_name !== canonical && item.slug !== result.slug),
        {
          canonical_name: canonical,
          slug: result.slug,
          handle: result.namespace,
          name: entry.name,
          version: entry.version,
          source: entry.source,
          install_dir: "",
        },
      ]);
      onInstalled?.();
      void loadInstalled({ silent: true });
      setMessage({
        text: t("skillHub.installed", {
          name: entry.name,
          defaultValue: "已安装「{{name}}」",
        }),
        isError: false,
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setConflicts((prev) => ({ ...prev, [key]: true }));
        setMessage({
          text: t("skillHub.installConflict", {
            name: entry.name,
            defaultValue: "「{{name}}」已安装，可覆盖安装",
          }),
          isError: false,
        });
      } else {
        setMessage({ text: skillHubErrorMessage(tx, error), isError: true });
      }
    } finally {
      setBusyKey(null);
    }
  };

  const handleCheckUpdates = async () => {
    setUpdatesBusy(true);
    setMessage(null);
    try {
      setUpdates(await fetchSkillHubUpdates(token));
    } catch (error) {
      setMessage({ text: skillHubErrorMessage(tx, error), isError: true });
    } finally {
      setUpdatesBusy(false);
    }
  };

  // 商店不可用（CLI 未装 / 已禁用）时给出原因；此时除已安装列表外的接口都返回空态
  const unavailableReason = (() => {
    if (status && (!status.available || !status.enabled)) {
      return status.reason || tx("skillHub.unavailableReason", "未检测到 SkillHub CLI");
    }
    const payload = [ranking, search].find((item) => item && item.available === false) ?? null;
    if (payload) {
      return payload.reason || tx("skillHub.unavailableReason", "未检测到 SkillHub CLI");
    }
    return null;
  })();

  const available = unavailableReason === null;
  const rows = search ? search.skills : (ranking?.skills ?? []);
  const hasList = search !== null || ranking !== null;

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
            {tx("skillHub.title", "技能商店")}
          </h1>
          <p className="mt-2 max-w-[680px] text-[13px] leading-5 text-muted-foreground">
            {tx(
              "skillHub.description",
              "技能由外部商店（SkillHub）提供，安装后下载到本工作区的技能目录。排行榜与搜索都来自商店远端，需要本机已安装 SkillHub CLI。提示：「检查升级」只覆盖自带更新清单（config.json）的技能，多数社区技能会被跳过，需重新安装才能升级，因此应用不会承诺技能「始终最新」。",
            )}
          </p>
        </div>

        <div className="space-y-7">
          {available ? (
            <>
              <div className="flex flex-col gap-3">
                <form onSubmit={handleSearch} className="flex items-center gap-2">
                  <Input
                    value={keyword}
                    onChange={(event) => setKeyword(event.target.value)}
                    placeholder={tx("skillHub.searchPlaceholder", "搜索商店技能…")}
                    aria-label={tx("skillHub.searchPlaceholder", "搜索商店技能…")}
                    className="h-9 max-w-[420px] rounded-[10px]"
                  />
                  <Button type="submit" size="sm" variant="outline" className="h-9 rounded-[10px] px-4">
                    <Search className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                    {tx("skillHub.search", "搜索")}
                  </Button>
                  {search ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={clearSearch}
                      className="h-9 rounded-[10px] px-4"
                    >
                      {tx("skillHub.backToRanking", "返回排行榜")}
                    </Button>
                  ) : null}
                </form>

                {!search ? (
                  <div className="flex flex-wrap items-center gap-1.5">
                    {RANKING_TYPES.map((type) => (
                      <button
                        key={type}
                        type="button"
                        onClick={() => setRankingType(type)}
                        aria-pressed={type === rankingType}
                        className={cn(
                          "flex h-8 items-center rounded-full border px-3 text-[12px] font-medium transition-colors",
                          type === rankingType
                            ? "border-foreground/35 bg-foreground/[0.04] text-foreground"
                            : "border-border/60 text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                        )}
                      >
                        {rankingTypeLabel(type, tx)}
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>

              {message ? (
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

              <p className="text-[12px] font-medium text-muted-foreground">
                {search
                  ? t("skillHub.searchSummary", {
                      keyword: search.query ?? "",
                      count: search.total,
                      defaultValue: "「{{keyword}}」共 {{count}} 个技能",
                    })
                  : t("skillHub.summary", {
                      count: rows.length,
                      defaultValue: "共 {{count}} 个技能 · 每 30 分钟自动刷新",
                    })}
                {!search && status?.available && status.version
                  ? ` · ${t("skillHub.cliVersion", {
                      version: status.version,
                      defaultValue: "CLI {{version}}",
                    })}`
                  : null}
              </p>

              {loading && !hasList ? (
                <div className="cyber-glass-panel relative flex h-48 items-center justify-center overflow-hidden rounded-[24px] border border-border/50 text-sm text-muted-foreground">
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
                  {t("settings.status.loading")}
                </div>
              ) : rows.length === 0 ? (
                <div className="cyber-glass-panel relative flex h-48 items-center justify-center overflow-hidden rounded-[24px] border border-dashed border-border/60 text-sm text-muted-foreground">
                  {search
                    ? tx("skillHub.searchNoResults", "没有匹配的技能，换个关键词试试")
                    : tx("skillHub.noSkills", "该排行榜暂时没有可展示的技能")}
                </div>
              ) : (
                <div className="cyber-glass-panel relative divide-y divide-border/50 overflow-hidden rounded-[16px] border border-border/60">
                  {rows.map((entry) => {
                    const key = skillKey(entry);
                    const alreadyInstalled = installedKeys.has(key) || installedKeys.has(entry.slug);
                    const installBusy = busyKey === `install:${key}`;
                    const conflicted = Boolean(conflicts[key]);
                    return (
                      <article
                        key={key}
                        className="group flex min-w-0 items-center gap-3 rounded-none px-4 py-3 transition-colors hover:bg-muted/45"
                      >
                        <EmployeeAvatar
                          avatar={isImageAvatar(entry.icon_url) ? entry.icon_url : undefined}
                          alt={entry.name}
                          fallback={(entry.name || entry.slug || "?").slice(0, 1).toUpperCase()}
                          className="flex h-10 w-10 shrink-0 items-center justify-center overflow-hidden rounded-[12px] bg-muted/70 text-[16px] font-semibold leading-none text-muted-foreground"
                          imgClassName="h-10 w-10"
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex min-w-0 items-baseline gap-2">
                            <h3 className="truncate text-[14px] font-semibold leading-5 text-foreground">
                              {entry.name || entry.slug}
                            </h3>
                            {entry.verified ? (
                              <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-sky-500/12 px-2 py-0.5 text-[10.5px] font-medium text-sky-700 dark:text-sky-300">
                                <BadgeCheck className="h-3 w-3" aria-hidden />
                                {tx("skillHub.verified", "已验证")}
                              </span>
                            ) : null}
                            {entry.category ? (
                              <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-[10.5px] font-medium text-muted-foreground/80">
                                {entry.category}
                              </span>
                            ) : null}
                            {entry.handle || entry.namespace ? (
                              // 命名空间 chip 展示 handle（与 slug 前缀一致）；商品名挂在 title 上
                              <span
                                title={entry.namespace || entry.handle}
                                className="shrink-0 rounded-full bg-muted/70 px-2 py-0.5 font-mono text-[10.5px] font-medium text-muted-foreground/70"
                              >
                                {entry.handle || entry.namespace}
                              </span>
                            ) : null}
                            {entry.source ? (
                              <span className="shrink-0 rounded-full bg-muted/70 px-2 py-0.5 font-mono text-[10.5px] font-medium text-muted-foreground/70">
                                {entry.source}
                              </span>
                            ) : null}
                            {entry.version ? (
                              <span className="shrink-0 text-[11px] text-muted-foreground/70">
                                v{entry.version}
                              </span>
                            ) : null}
                          </div>
                          <p className="mt-0.5 truncate text-[12.5px] leading-5 text-muted-foreground">
                            {entry.description || tx("skillHub.noDescription", "暂无简介")}
                          </p>
                        </div>
                        <div className="flex shrink-0 items-center gap-2">
                          {alreadyInstalled && !conflicted ? (
                            <span className="inline-flex items-center gap-1.5 rounded-full bg-muted px-3 py-1.5 text-[12px] font-medium text-muted-foreground">
                              <Check className="h-3.5 w-3.5" aria-hidden />
                              {tx("skillHub.installedBadge", "已安装")}
                            </span>
                          ) : (
                            <>
                              {conflicted ? (
                                <span className="inline-flex items-center gap-1.5 text-[11.5px] font-medium text-amber-700 dark:text-amber-300">
                                  <TriangleAlert className="h-3.5 w-3.5" aria-hidden />
                                  {tx("skillHub.installedConflictHint", "已安装，可覆盖")}
                                </span>
                              ) : null}
                              <Button
                                type="button"
                                size="sm"
                                variant={conflicted ? "outline" : "default"}
                                disabled={installBusy}
                                onClick={() => void handleInstall(entry, conflicted)}
                                className="h-9 rounded-[10px] px-4"
                              >
                                {installBusy ? (
                                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
                                ) : (
                                  <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                                )}
                                {conflicted
                                  ? tx("skillHub.forceInstall", "覆盖安装")
                                  : tx("skillHub.install", "安装")}
                              </Button>
                            </>
                          )}
                        </div>
                      </article>
                    );
                  })}
                </div>
              )}

              <div className="cyber-glass-panel relative overflow-hidden rounded-[16px] border border-border/60">
                <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
                  <div className="min-w-0">
                    <h2 className="text-[14px] font-semibold text-foreground">
                      {tx("skillHub.installedTitle", "已安装")}
                    </h2>
                    <p className="mt-0.5 text-[12px] leading-5 text-muted-foreground">
                      {t("skillHub.installedCount", {
                        count: installedSkills.length,
                        defaultValue: "共 {{count}} 个由商店安装的技能",
                      })}
                    </p>
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={updatesBusy}
                    onClick={() => void handleCheckUpdates()}
                    className="h-9 shrink-0 rounded-[10px] px-4"
                  >
                    {updatesBusy ? (
                      <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
                    ) : (
                      <RefreshCw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                    )}
                    {tx("skillHub.checkUpdates", "检查升级")}
                  </Button>
                </div>

                {installedSkills.length === 0 ? (
                  <p className="border-t border-border/50 px-4 py-4 text-[12.5px] text-muted-foreground">
                    {tx("skillHub.noInstalled", "还没有安装任何商店技能")}
                  </p>
                ) : (
                  <ul className="divide-y divide-border/50 border-t border-border/50">
                    {installedSkills.map((item) => (
                      <li
                        key={item.canonical_name || item.slug}
                        className="flex min-w-0 items-center gap-3 px-4 py-2.5"
                      >
                        <span className="truncate text-[13px] font-medium text-foreground">
                          {item.name || item.canonical_name}
                        </span>
                        {item.version ? (
                          <span className="shrink-0 text-[11.5px] text-muted-foreground">
                            v{item.version}
                          </span>
                        ) : null}
                        <span className="truncate font-mono text-[11.5px] text-muted-foreground/70">
                          {item.canonical_name}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}

                {updates ? (
                  <div className="space-y-2 border-t border-border/50 px-4 py-3 text-[12.5px] leading-5 text-muted-foreground">
                    <p className="text-foreground">
                      {t("skillHub.updatesSummary", {
                        checked: updates.checked,
                        upgradable: updates.upgradable,
                        skipped: updates.skipped,
                        failed: updates.failed,
                        defaultValue:
                          "共检查 {{checked}} 个 · 可升级 {{upgradable}} 个 · 跳过 {{skipped}} 个 · 失败 {{failed}} 个",
                      })}
                    </p>
                    <p>
                      {tx(
                        "skillHub.updatesSkippedHint",
                        "「跳过」= 该技能没有自带更新清单（config.json），商店无法为它检查更新，只能重新安装才能拿到新版本，因此跳过数偏高属正常现象。",
                      )}
                    </p>
                    {updates.summary ? (
                      <pre className="max-w-full whitespace-pre-wrap break-all rounded-[10px] bg-muted/70 px-3 py-2 font-mono text-[11.5px] leading-5 text-foreground/80">
                        {updates.summary}
                      </pre>
                    ) : null}
                    {updates.details.length > 0 ? (
                      <ul className="space-y-1">
                        {updates.details.map((line, index) => (
                          <li
                            key={`${index}-${line}`}
                            className="truncate font-mono text-[11.5px] text-muted-foreground/80"
                          >
                            {line}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </div>
                ) : null}
              </div>
            </>
          ) : (
            <div className="cyber-glass-panel relative overflow-hidden rounded-[16px] border border-dashed border-border/60 p-8 text-center">
              <Terminal className="mx-auto mb-3 h-7 w-7 text-muted-foreground/60" aria-hidden />
              <h2 className="text-[14px] font-semibold text-foreground">
                {tx("skillHub.unavailableTitle", "技能商店尚不可用")}
              </h2>
              <p className="mx-auto mt-2 max-w-[520px] text-[13px] leading-5 text-muted-foreground">
                {unavailableReason ||
                  tx("skillHub.unavailableReason", "未检测到 SkillHub CLI，请先安装")}
              </p>
              <p className="mx-auto mt-2 max-w-[520px] text-[13px] leading-5 text-muted-foreground">
                {tx(
                  "skillHub.installHint",
                  "在终端执行官方安装脚本后重启应用即可（CLI 路径由后台配置，应用内不可更改）：",
                )}
              </p>
              <pre className="mx-auto mt-4 inline-block max-w-full whitespace-pre-wrap break-all rounded-[10px] bg-muted/70 px-4 py-2 text-left font-mono text-[12.5px] leading-6 text-foreground/80">
                {INSTALL_COMMAND}
              </pre>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
