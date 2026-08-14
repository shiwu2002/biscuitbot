import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Activity,
  ArrowUpRight,
  BarChart3,
  CalendarDays,
  Coins,
  Flame,
  Loader2,
  MessagesSquare,
  Sparkles,
  Target,
  Timer,
  TrendingUp,
  Zap,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  formatCompactTokens,
  UsageDistributionBars,
  UsageLineChart,
} from "@/components/dashboard/UsageCharts";
import { CyberPageFrame, CyberPanel } from "@/components/layout/CyberPageFrame";
import { fetchSettingsUsage } from "@/lib/api";
import type { ChatSummary, Employee, SettingsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

type UsagePayload = NonNullable<SettingsPayload["usage"]>;

function buildLast30DaySeries(
  usage: UsagePayload | null,
  field: "total_tokens" | "requests",
): Array<{ label: string; value: number }> {
  if (!usage?.days?.length) return [];
  const sorted = [...usage.days].sort((a, b) => a.date.localeCompare(b.date));
  const tail = sorted.slice(-30);
  return tail.map((day) => ({
    label: day.date.slice(5),
    value: day[field] ?? 0,
  }));
}

function KpiCard({
  icon,
  label,
  value,
  hint,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="cyber-glass-panel relative overflow-hidden rounded-xl p-4">
      <div className="cyber-kpi-icon mb-3 flex h-9 w-9 items-center justify-center rounded-full text-primary">
        {icon}
      </div>
      <p className="text-[24px] font-semibold leading-none tracking-tight text-foreground sm:text-[28px]">
        {value}
      </p>
      <p className="mt-1.5 text-[12px] text-muted-foreground">{label}</p>
      {hint ? (
        <p className="mt-1 text-[11px] text-muted-foreground/70">{hint}</p>
      ) : null}
    </div>
  );
}

export function AnalyticsDashboardView({
  employees = [],
  sessions = [],
  runningChatIds = [],
  titleOverrides,
  onSelectSession,
  hostChromeInset,
}: {
  employees?: Employee[];
  /** 预留：按用户时区展示日期（当前图表用 UTC 日期键，后续接入）。 */
  timeZone?: string;
  /** 运营区数据：会话列表（最近对话）、运行中任务、标题覆盖、会话跳转。 */
  sessions?: ChatSummary[];
  runningChatIds?: string[];
  titleOverrides?: Record<string, string>;
  onSelectSession?: (key: string) => void;
  hostChromeInset?: boolean;
}) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [usage, setUsage] = useState<UsagePayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setUsage(await fetchSettingsUsage(token));
      setError(null);
    } catch {
      setError(t("dashboard.loadError"));
    } finally {
      setLoading(false);
    }
  }, [token, t]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const employeeLabelById = useMemo(
    () => new Map(employees.map((employee) => [employee.id, employee.name])),
    [employees],
  );

  const tokenSeries = useMemo(
    () => buildLast30DaySeries(usage, "total_tokens"),
    [usage],
  );
  const taskSeries = useMemo(
    () => buildLast30DaySeries(usage, "requests"),
    [usage],
  );
  const modelItems = useMemo(
    () =>
      (usage?.models_30d ?? []).map((row) => ({
        key: row.key,
        value: row.total_tokens,
      })),
    [usage?.models_30d],
  );
  const employeeItems = useMemo(
    () =>
      (usage?.employees_30d ?? []).map((row) => ({
        key: row.key,
        value: row.total_tokens,
      })),
    [usage?.employees_30d],
  );

  const todayTokens = usage?.today_tokens ?? 0;
  const todayTasks = usage?.today_requests ?? 0;

  const runningSet = useMemo(
    () => new Set(runningChatIds ?? []),
    [runningChatIds],
  );
  const runningByEmployee = useMemo(() => {
    const map = new Map<string, number>();
    for (const s of sessions) {
      if (s.employee && runningSet.has(s.key)) {
        map.set(s.employee, (map.get(s.employee) ?? 0) + 1);
      }
    }
    return map;
  }, [sessions, runningSet]);

  const recent = useMemo(
    () =>
      [...sessions]
        .sort((a, b) => {
          const at = a.updatedAt ?? a.createdAt ?? "";
          const bt = b.updatedAt ?? b.createdAt ?? "";
          return bt < at ? -1 : bt > at ? 1 : 0;
        })
        .slice(0, 5),
    [sessions],
  );

  return (
    <CyberPageFrame>
      <header
        className={cn(
          "mb-5 flex flex-wrap items-center gap-3 sm:mb-6",
          hostChromeInset && "pt-[2.5rem]",
        )}
      >
        <div className="min-w-0 flex-1">
          <h1 className="text-[22px] font-semibold tracking-tight text-foreground sm:text-[28px]">
            {t("dashboard.title")}
          </h1>
          <p className="mt-1 text-[13px] text-muted-foreground">
            {t("dashboard.subtitle")}
          </p>
        </div>
        {loading ? (
          <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-hidden />
        ) : null}
      </header>

      {error ? (
        <div className="mb-4 rounded-xl border border-destructive/30 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <KpiCard
          icon={<Coins className="h-4 w-4" />}
          label={t("dashboard.kpi.totalTokens")}
          value={formatCompactTokens(usage?.total_tokens ?? 0)}
        />
        <KpiCard
          icon={<TrendingUp className="h-4 w-4" />}
          label={t("dashboard.kpi.todayTokens")}
          value={formatCompactTokens(todayTokens)}
          hint={t("dashboard.kpi.todayTasksHint", { count: todayTasks })}
        />
        <KpiCard
          icon={<Target className="h-4 w-4" />}
          label={t("dashboard.kpi.completedTasks")}
          value={String(usage?.requests_total ?? 0)}
          hint={t("dashboard.kpi.completedTasksHint")}
        />
        <KpiCard
          icon={<Activity className="h-4 w-4" />}
          label={t("dashboard.kpi.monthTokens")}
          value={formatCompactTokens(usage?.total_tokens_30d ?? 0)}
          hint={t("dashboard.kpi.monthTasksHint", { count: usage?.requests_30d ?? 0 })}
        />
        <KpiCard
          icon={<Zap className="h-4 w-4" />}
          label={t("dashboard.kpi.peakDay")}
          value={formatCompactTokens(usage?.peak_day_tokens ?? 0)}
          hint={t("dashboard.kpi.peakDayHint")}
        />
        <KpiCard
          icon={<Timer className="h-4 w-4" />}
          label={t("dashboard.kpi.currentStreak")}
          value={String(usage?.current_streak_days ?? 0)}
          hint={t("dashboard.kpi.currentStreakHint")}
        />
        <KpiCard
          icon={<Flame className="h-4 w-4" />}
          label={t("dashboard.kpi.longestStreak")}
          value={String(usage?.longest_streak_days ?? 0)}
          hint={t("dashboard.kpi.longestStreakHint")}
        />
        <KpiCard
          icon={<CalendarDays className="h-4 w-4" />}
          label={t("dashboard.kpi.activeDays30")}
          value={String(usage?.active_days_30d ?? 0)}
          hint={t("dashboard.kpi.activeDays30Hint")}
        />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <CyberPanel
          title={t("dashboard.charts.tokenTrend")}
          icon={<TrendingUp className="h-4 w-4" />}
        >
          <UsageLineChart
            data={tokenSeries}
            ariaLabel={t("dashboard.charts.tokenTrend")}
          />
        </CyberPanel>
        <CyberPanel
          title={t("dashboard.charts.taskTrend")}
          icon={<BarChart3 className="h-4 w-4" />}
        >
          <UsageLineChart
            data={taskSeries}
            ariaLabel={t("dashboard.charts.taskTrend")}
            valueFormatter={(value) => String(value)}
          />
        </CyberPanel>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <CyberPanel
          title={t("dashboard.charts.modelDistribution")}
          icon={<Sparkles className="h-4 w-4" />}
        >
          <UsageDistributionBars
            items={modelItems}
            ariaLabel={t("dashboard.charts.modelDistribution")}
            labelForKey={(key) => key}
          />
        </CyberPanel>
        <CyberPanel
          title={t("dashboard.charts.employeeDistribution")}
          icon={<Activity className="h-4 w-4" />}
        >
          <UsageDistributionBars
            items={employeeItems}
            ariaLabel={t("dashboard.charts.employeeDistribution")}
            labelForKey={(key) => {
              const label = employeeLabelById.get(key);
              if (label) return label;
              if (key === "main") return t("dashboard.mainAgent");
              return key;
            }}
          />
        </CyberPanel>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <CyberPanel
          title={t("dashboard.ops.agentStatus")}
          icon={<Activity className="h-4 w-4" />}
        >
          {employees.length === 0 ? (
            <p className="text-[12px] text-muted-foreground">
              {t("dashboard.ops.noAgents")}
            </p>
          ) : (
            <div className="flex flex-col gap-1">
              {employees.map((e) => {
                const running = (runningByEmployee.get(e.id) ?? 0) > 0;
                return (
                  <div
                    key={e.id}
                    className="flex items-center gap-2.5 rounded-lg px-1.5 py-1.5 hover:bg-muted/40"
                  >
                    <span
                      className={cn(
                        "h-2 w-2 shrink-0 rounded-full",
                        running
                          ? "cyber-status-dot-running"
                          : "cyber-status-dot-idle",
                      )}
                    />
                    <span className="min-w-0 flex-1 truncate text-[13px] text-foreground/90">
                      {e.name}
                    </span>
                    <span className="text-[11px] text-muted-foreground">
                      {running
                        ? t("dashboard.ops.statusRunning")
                        : t("dashboard.ops.statusIdle")}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </CyberPanel>
        <CyberPanel
          title={t("dashboard.ops.recentConversations")}
          icon={<MessagesSquare className="h-4 w-4" />}
        >
          {recent.length === 0 ? (
            <p className="text-[12px] text-muted-foreground">
              {t("dashboard.ops.noSessions")}
            </p>
          ) : (
            <div className="flex flex-col gap-1">
              {recent.map((s) => (
                <button
                  key={s.key}
                  type="button"
                  onClick={() => onSelectSession?.(s.key)}
                  className="group flex items-center gap-2.5 rounded-lg px-1.5 py-1.5 text-left hover:bg-muted/40"
                >
                  <span
                    className={cn(
                      "h-2 w-2 shrink-0 rounded-full",
                      runningSet.has(s.key)
                        ? "cyber-status-dot-running"
                        : "cyber-status-dot-idle",
                    )}
                  />
                  <span className="min-w-0 flex-1 truncate text-[13px] text-foreground/90">
                    {(titleOverrides?.[s.key] ?? s.title ?? s.preview) ||
                      t("dashboard.ops.recentConversations")}
                  </span>
                  <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground/50 opacity-0 transition-opacity group-hover:opacity-100" />
                </button>
              ))}
            </div>
          )}
        </CyberPanel>
      </div>

      {usage?.updated_at ? (
        <p className="mt-4 text-center text-[11px] text-muted-foreground/70">
          {t("dashboard.updatedAt", {
            time: new Date(usage.updated_at).toLocaleString(),
          })}
        </p>
      ) : null}
    </CyberPageFrame>
  );
}
