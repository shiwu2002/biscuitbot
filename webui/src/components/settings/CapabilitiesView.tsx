import { useCallback, useEffect, useState, type ReactNode } from "react";
import type { TFunction } from "i18next";
import {
  Boxes,
  Brain,
  Check,
  ChevronDown,
  CircleAlert,
  Download,
  KeyRound,
  Loader2,
  Play,
  RotateCcw,
  Search,
  Terminal,
  Trash2,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";

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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import {
  deleteSkill,
  fetchCapabilities,
  fetchCapabilityDetail,
  runCliAppAction,
  runMcpPresetAction,
} from "@/lib/api";
import type { CapabilityDetail, CapabilityInfo } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

type KindFilter = "all" | "prompt" | "process" | "mcp";

const KIND_FILTERS: Array<{ key: KindFilter; runtime?: string }> = [
  { key: "all" },
  { key: "prompt", runtime: "prompt" },
  { key: "process", runtime: "process" },
  { key: "mcp", runtime: "mcp" },
];

type TierFilter = "all" | "system" | "agent" | "user";
type AvailabilityFilter = "all" | "available" | "unavailable";
type SourceFilter = "all" | "builtin" | "workspace" | "cli-anything" | "mcp-preset" | "custom";

const TIER_FILTERS: TierFilter[] = ["all", "system", "agent", "user"];
const AVAILABILITY_FILTERS: AvailabilityFilter[] = ["all", "available", "unavailable"];
const SOURCE_FILTERS: SourceFilter[] = ["all", "builtin", "workspace", "cli-anything", "mcp-preset", "custom"];

/** 已知来源集合，用于「自定义来源」兜底匹配（其余来源名落到 custom）。 */
const KNOWN_SOURCES = new Set(["builtin", "workspace", "cli-anything", "mcp-preset"]);

export function CapabilitiesView({ onSkillsDeleted }: { onSkillsDeleted?: () => void }) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [kind, setKind] = useState<KindFilter>("all");
  const [capabilities, setCapabilities] = useState<CapabilityInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState<CapabilityInfo | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<CapabilityInfo | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [acting, setActing] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [tier, setTier] = useState<TierFilter>("all");
  const [availability, setAvailability] = useState<AvailabilityFilter>("all");
  const [source, setSource] = useState<SourceFilter>("all");

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const runtime = KIND_FILTERS.find((f) => f.key === kind)?.runtime;
      const payload = await fetchCapabilities(token, runtime);
      setCapabilities(payload.capabilities);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [kind, token]);

  useEffect(() => {
    void load();
  }, [load]);

  const query = search.trim().toLowerCase();
  const filtered = capabilities.filter((cap) => {
    if (tier !== "all" && (cap.tier ?? "user") !== tier) return false;
    if (availability === "available" && !cap.available) return false;
    if (availability === "unavailable" && cap.available) return false;
    if (source !== "all" && !matchesSource(cap.source, source)) return false;
    if (query) {
      const haystack = [cap.display_name, cap.name, cap.id, cap.description, ...cap.tags, cap.category]
        .filter((part): part is string => typeof part === "string" && part.length > 0)
        .join(" ")
        .toLowerCase();
      if (!haystack.includes(query)) return false;
    }
    return true;
  });
  const hasActiveFilters = query !== "" || tier !== "all" || availability !== "all" || source !== "all";
  const installedCount = filtered.filter((cap) => cap.installed).length;

  const resetFilters = () => {
    setSearch("");
    setTier("all");
    setAvailability("all");
    setSource("all");
  };

  const handleConfirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteSkill(token, deleteTarget.id);
      setDeleteTarget(null);
      onSkillsDeleted?.();
      await load();
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeleting(false);
    }
  };

  const runProcessAction = async (cap: CapabilityInfo, action: "install" | "uninstall" | "test") => {
    setActing(`${cap.id}:${action}`);
    setActionError(null);
    try {
      await runCliAppAction(token, action, cap.id);
      await load();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setActing(null);
    }
  };

  const runMcpAction = async (cap: CapabilityInfo, action: "enable" | "remove" | "test") => {
    setActing(`${cap.id}:${action}`);
    setActionError(null);
    try {
      await runMcpPresetAction(token, action, cap.id, {});
      await load();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setActing(null);
    }
  };

  return (
    <div className="space-y-6">
      <section className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <p className="max-w-[680px] text-[13px] leading-5 text-muted-foreground">
          {t("settings.capabilities.description", {
            defaultValue:
              "A unified catalog of everything this agent can do — instruction skills, CLI applications, and MCP servers.",
          })}
        </p>
        <span className="text-[12px] font-medium text-muted-foreground">
          {t("settings.capabilities.caption", {
            installed: installedCount,
            total: capabilities.length,
            defaultValue: "{{installed}} installed · {{total}} total",
          })}
        </span>
      </section>

      <section className="flex flex-wrap items-center gap-1.5">
        {KIND_FILTERS.map((filter) => (
          <button
            key={filter.key}
            type="button"
            onClick={() => setKind(filter.key)}
            className={cn(
              "rounded-full px-3 py-1.5 text-[12px] font-medium transition-colors",
              kind === filter.key
                ? "bg-foreground text-background"
                : "bg-muted text-muted-foreground hover:bg-muted/70",
            )}
          >
            {kindFilterLabel(filter.key, t)}
          </button>
        ))}
      </section>

      <section className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[220px] flex-1">
          <Search
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden
          />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("settings.capabilities.searchPlaceholder", {
              defaultValue: "搜索名称、描述…",
            })}
            className="h-9 pl-9 pr-8"
            aria-label={t("settings.capabilities.searchPlaceholder", {
              defaultValue: "搜索名称、描述…",
            })}
          />
          {search ? (
            <button
              type="button"
              aria-label={t("settings.capabilities.searchClear", { defaultValue: "清除搜索" })}
              onClick={() => setSearch("")}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground transition-colors hover:text-foreground"
            >
              <X className="h-4 w-4" aria-hidden />
            </button>
          ) : null}
        </div>

        <FilterMenu
          label={t("settings.capabilities.tierLabel", { defaultValue: "层级" })}
          value={tier}
          options={TIER_FILTERS.map((v) => ({ value: v, label: tierFilterLabel(v, t) }))}
          onValueChange={(v) => setTier(v as TierFilter)}
        />
        <FilterMenu
          label={t("settings.capabilities.availabilityLabel", { defaultValue: "可用性" })}
          value={availability}
          options={AVAILABILITY_FILTERS.map((v) => ({ value: v, label: availabilityFilterLabel(v, t) }))}
          onValueChange={(v) => setAvailability(v as AvailabilityFilter)}
        />
        <FilterMenu
          label={t("settings.capabilities.sourceLabel", { defaultValue: "来源" })}
          value={source}
          options={SOURCE_FILTERS.map((v) => ({ value: v, label: sourceFilterLabel(v, t) }))}
          onValueChange={(v) => setSource(v as SourceFilter)}
        />

        {hasActiveFilters ? (
          <button
            type="button"
            onClick={resetFilters}
            className="flex h-9 items-center gap-1.5 rounded-full border border-border/60 px-3 text-[12px] font-medium text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground"
          >
            <RotateCcw className="h-3.5 w-3.5" aria-hidden />
            {t("settings.capabilities.resetFilters", { defaultValue: "重置筛选" })}
          </button>
        ) : null}
      </section>

      {actionError ? (
        <div className="rounded-[14px] bg-destructive/10 px-3 py-2.5 text-[13px] text-destructive">
          {actionError}
        </div>
      ) : null}

      {loading ? (
        <div className="flex items-center gap-2 px-1 py-10 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          {t("settings.capabilities.loading", { defaultValue: "Loading capabilities..." })}
        </div>
      ) : loadError ? (
        <div className="rounded-[14px] bg-destructive/10 px-3 py-3 text-sm text-destructive">
          {loadError}
        </div>
      ) : filtered.length ? (
        <div className="grid gap-x-10 gap-y-1 py-1 md:grid-cols-2">
          {filtered.map((cap) => (
            <CapabilityRow
              key={`${cap.source}:${cap.id}`}
              cap={cap}
              acting={acting}
              onSelect={setSelected}
              onDelete={setDeleteTarget}
              onProcessAction={runProcessAction}
              onMcpAction={runMcpAction}
            />
          ))}
        </div>
      ) : (
        <div className="px-3 py-12 text-center text-sm text-muted-foreground">
          {query
            ? t("settings.capabilities.emptySearch", {
                query,
                defaultValue: "没有匹配「{{query}}」的能力。",
              })
            : t("settings.capabilities.empty", { defaultValue: "没有符合此筛选的能力。" })}
        </div>
      )}

      <CapabilityDeleteDialog
        cap={deleteTarget}
        open={deleteTarget !== null}
        deleting={deleting}
        error={deleteError}
        onOpenChange={(open) => {
          if (!open) {
            setDeleteTarget(null);
            setDeleteError(null);
          }
        }}
        onConfirm={handleConfirmDelete}
      />

      <CapabilityDetailSheet
        cap={selected}
        open={selected !== null}
        onOpenChange={(open) => {
          if (!open) setSelected(null);
        }}
      />
    </div>
  );
}

function CapabilityRow({
  cap,
  acting,
  onSelect,
  onDelete,
  onProcessAction,
  onMcpAction,
}: {
  cap: CapabilityInfo;
  acting: string | null;
  onSelect: (cap: CapabilityInfo) => void;
  onDelete: (cap: CapabilityInfo) => void;
  onProcessAction: (cap: CapabilityInfo, action: "install" | "uninstall" | "test") => void;
  onMcpAction: (cap: CapabilityInfo, action: "enable" | "remove" | "test") => void;
}) {
  const { t } = useTranslation();
  const StatusIcon = cap.available ? Check : CircleAlert;
  const canDelete = cap.runtime === "prompt" && cap.source === "workspace";
  const canInstallProcess = cap.runtime === "process" && cap.install_supported && !cap.installed;
  const canRunProcess = cap.runtime === "process" && cap.installed;
  const canEnableMcp = cap.runtime === "mcp" && !cap.installed;
  const canRemoveMcp = cap.runtime === "mcp" && cap.installed;
  const busy = acting !== null && acting.startsWith(`${cap.id}:`);

  return (
    <div
      className={cn(
        "group flex min-w-0 items-center gap-3 rounded-[16px] px-3 py-3 text-left transition-colors",
        "hover:bg-muted/45",
        !cap.available && "opacity-65",
      )}
    >
      <button
        type="button"
        aria-label={t("settings.capabilities.openDetails", {
          name: cap.display_name,
          defaultValue: "Open details for {{name}}",
        })}
        onClick={() => onSelect(cap)}
        className="flex min-w-0 flex-1 items-center gap-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-[16px]"
      >
        <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-[14px] bg-muted/70 text-muted-foreground">
          <CapabilityIcon cap={cap} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <h3 className="truncate text-[15px] font-semibold leading-5 text-foreground">
              {cap.display_name}
            </h3>
            <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold leading-none text-muted-foreground">
              {sourceLabel(cap.source, t)}
            </span>
            <span className={cn("shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-semibold leading-none", runtimeBadgeClass(cap.runtime))}>
              {runtimeLabel(cap.runtime, t)}
            </span>
            {cap.tier ? (
              <span
                className={cn("shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-semibold leading-none", tierBadgeClass(cap.tier))}
                title={tierLabel(cap.tier, t)}
              >
                {tierLabel(cap.tier, t)}
              </span>
            ) : null}
          </div>
          <p className="mt-1 line-clamp-2 text-[13px] leading-5 text-muted-foreground">
            {cap.description || cap.id}
          </p>
          {!cap.available && cap.unavailable_reason ? (
            <p className="mt-1 truncate text-[12px] leading-4 text-muted-foreground/80">
              {t("settings.capabilities.unavailableReason", {
                reason: cap.unavailable_reason,
                defaultValue: "Missing: {{reason}}",
              })}
            </p>
          ) : null}
        </div>
      </button>
      <div className="flex shrink-0 items-center gap-2">
        <span
          title={!cap.available && cap.unavailable_reason ? cap.unavailable_reason : undefined}
          className={cn(
            "hidden items-center gap-1 rounded-full px-2.5 py-1 text-[12px] font-medium sm:inline-flex",
            cap.available ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" : "bg-muted text-muted-foreground",
          )}
        >
          <StatusIcon className="h-3.5 w-3.5" aria-hidden />
          {cap.available
            ? t("settings.capabilities.available", { defaultValue: "Available" })
            : t("settings.capabilities.unavailable", { defaultValue: "Unavailable" })}
        </span>
        {canInstallProcess ? (
          <ActionIconButton
            busy={busy}
            disabled={Boolean(acting)}
            label={t("settings.capabilities.install", { defaultValue: "Install" })}
            onClick={() => onProcessAction(cap, "install")}
          >
            <Download className="h-4 w-4" aria-hidden />
          </ActionIconButton>
        ) : null}
        {canRunProcess ? (
          <>
            <ActionIconButton
              busy={busy}
              disabled={Boolean(acting)}
              label={t("settings.capabilities.test", { defaultValue: "Test" })}
              onClick={() => onProcessAction(cap, "test")}
            >
              <Play className="h-4 w-4" aria-hidden />
            </ActionIconButton>
            <ActionIconButton
              busy={busy}
              disabled={Boolean(acting)}
              label={t("settings.capabilities.uninstall", { defaultValue: "Uninstall" })}
              onClick={() => onProcessAction(cap, "uninstall")}
            >
              <X className="h-4 w-4" aria-hidden />
            </ActionIconButton>
          </>
        ) : null}
        {canEnableMcp ? (
          <ActionIconButton
            busy={busy}
            disabled={Boolean(acting)}
            label={t("settings.capabilities.enable", { defaultValue: "Enable" })}
            onClick={() => onMcpAction(cap, "enable")}
          >
            <Download className="h-4 w-4" aria-hidden />
          </ActionIconButton>
        ) : null}
        {canRemoveMcp ? (
          <ActionIconButton
            busy={busy}
            disabled={Boolean(acting)}
            label={t("settings.capabilities.remove", { defaultValue: "Remove" })}
            onClick={() => onMcpAction(cap, "remove")}
          >
            <X className="h-4 w-4" aria-hidden />
          </ActionIconButton>
        ) : null}
        {canDelete ? (
          <ActionIconButton
            busy={busy}
            disabled={Boolean(acting)}
            label={t("settings.capabilities.delete", { defaultValue: "Delete" })}
            onClick={(e) => {
              e.stopPropagation();
              onDelete(cap);
            }}
          >
            <Trash2 className="h-4 w-4" aria-hidden />
          </ActionIconButton>
        ) : null}
      </div>
    </div>
  );
}

function ActionIconButton({
  label,
  onClick,
  busy,
  disabled,
  children,
}: {
  label: string;
  onClick: (e: React.MouseEvent) => void;
  busy: boolean;
  disabled: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
    >
      {busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : children}
    </button>
  );
}

function CapabilityDetailSheet({
  cap,
  open,
  onOpenChange,
}: {
  cap: CapabilityInfo | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { token } = useClient();
  const { t } = useTranslation();
  const [detail, setDetail] = useState<CapabilityDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);

  useEffect(() => {
    if (!open || !cap) return;
    let cancelled = false;
    setDetail(null);
    setLoading(true);
    setLoadFailed(false);
    fetchCapabilityDetail(token, cap.id)
      .then((payload) => {
        if (!cancelled) setDetail(payload);
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
  }, [open, cap, token]);

  if (!cap) return null;

  const active = detail ?? cap;
  const requirements = active.requirements ?? cap.requirements;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-[min(34rem,calc(100vw-1rem))] max-w-none gap-0 overflow-hidden p-0 sm:max-w-none"
      >
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
          <div className="flex items-start gap-3 pr-8">
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-[15px] bg-muted/70 text-muted-foreground">
              <CapabilityIcon cap={active} />
            </div>
            <div className="min-w-0">
              <SheetTitle className="truncate text-[20px] font-semibold">{active.display_name}</SheetTitle>
              <SheetDescription className="sr-only">
                {t("settings.capabilities.detailDescription", {
                  name: active.display_name,
                  defaultValue: "Details for {{name}}.",
                })}
              </SheetDescription>
              <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[12px] text-muted-foreground">
                <Pill>{sourceLabel(active.source, t)}</Pill>
                <Pill tone={active.runtime === "mcp" ? "accent" : "muted"}>{runtimeLabel(active.runtime, t)}</Pill>
                <Pill tone={active.available ? "success" : "muted"}>
                  {active.available
                    ? t("settings.capabilities.available", { defaultValue: "Available" })
                    : t("settings.capabilities.unavailable", { defaultValue: "Unavailable" })}
                </Pill>
              </div>
            </div>
          </div>

          {loading ? (
            <div className="mt-8 flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              {t("settings.capabilities.loadingDetail", { defaultValue: "Loading details..." })}
            </div>
          ) : loadFailed ? (
            <div className="mt-8 rounded-[16px] bg-destructive/10 px-3 py-3 text-sm text-destructive">
              {t("settings.capabilities.loadFailed", { defaultValue: "Could not load capability details." })}
            </div>
          ) : (
            <div className="mt-7 space-y-6">
              <DetailSection title={t("settings.capabilities.descriptionTitle", { defaultValue: "Description" })}>
                <p className="text-[14px] leading-6 text-muted-foreground">{active.description || active.id}</p>
              </DetailSection>

              {active.requires ? (
                <DetailSection title={t("settings.capabilities.requirements", { defaultValue: "Requirements" })}>
                  <p className="text-[13px] leading-5 text-muted-foreground">{active.requires}</p>
                </DetailSection>
              ) : null}

              {requirements && (requirements.bins?.length || requirements.env?.length) ? (
                <RequirementsBlocks requirements={requirements} />
              ) : null}

              {active.runtime === "prompt" && detail?.raw_markdown != null ? (
                <RawInstructionsBlock markdown={detail.raw_markdown} />
              ) : null}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function RequirementsBlocks({ requirements }: { requirements: NonNullable<CapabilityInfo["requirements"]> }) {
  const { t } = useTranslation();
  const bins = requirements.bins ?? [];
  const env = requirements.env ?? [];
  return (
    <div className="space-y-3">
      {bins.length ? (
        <RequirementLine
          title={t("settings.capabilities.commands", { defaultValue: "Commands" })}
          items={bins}
          icon={<Terminal className="h-3.5 w-3.5" aria-hidden />}
        />
      ) : null}
      {env.length ? (
        <RequirementLine
          title={t("settings.capabilities.environment", { defaultValue: "Environment variables" })}
          items={env}
          icon={<KeyRound className="h-3.5 w-3.5" aria-hidden />}
        />
      ) : null}
    </div>
  );
}

function RawInstructionsBlock({ markdown }: { markdown: string }) {
  const { t } = useTranslation();
  const content =
    markdown || t("settings.capabilities.rawInstructionsEmpty", { defaultValue: "No raw instructions." });
  return (
    <details className="group rounded-[18px] border border-border/45 bg-muted/20 px-3 py-3">
      <summary className="cursor-pointer select-none text-[13px] font-medium text-foreground/90 transition-colors hover:text-foreground">
        {t("settings.capabilities.rawInstructions", { defaultValue: "Raw SKILL.md" })}
      </summary>
      <div className="mt-3 overflow-hidden rounded-[14px] border border-border/35 bg-background/70">
        <pre
          className={cn(
            "max-h-[min(42vh,32rem)] overflow-auto overscroll-contain px-3.5 py-3 pr-4",
            "whitespace-pre-wrap break-words font-mono text-[12px] leading-[1.7] text-foreground/62",
            "scrollbar-thin scrollbar-track-transparent",
            "[&::-webkit-scrollbar]:h-1.5 [&::-webkit-scrollbar]:w-1.5",
            "[&::-webkit-scrollbar-thumb]:bg-muted-foreground/25",
          )}
        >
          {content}
        </pre>
      </div>
    </details>
  );
}

function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-[12px] font-medium text-muted-foreground">{title}</h3>
      {children}
    </section>
  );
}

function RequirementLine({
  title,
  items,
  icon,
}: {
  title: string;
  items: string[];
  icon: ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
        {icon}
        {title}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {items.map((item) => (
          <Pill key={item}>{item}</Pill>
        ))}
      </div>
    </div>
  );
}

function Pill({
  children,
  tone = "muted",
}: {
  children: ReactNode;
  tone?: "muted" | "success" | "accent";
}) {
  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center rounded-full px-2 py-0.5 text-[11px] font-medium",
        tone === "success"
          ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
          : tone === "accent"
            ? "bg-sky-500/12 text-sky-700 dark:text-sky-300"
            : "bg-muted text-muted-foreground",
      )}
    >
      {children}
    </span>
  );
}

function CapabilityDeleteDialog({
  cap,
  open,
  deleting,
  error,
  onOpenChange,
  onConfirm,
}: {
  cap: CapabilityInfo | null;
  open: boolean;
  deleting: boolean;
  error: string | null;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  const { t } = useTranslation();
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            {t("settings.capabilities.deleteTitle", { defaultValue: "Delete capability" })}
          </AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-2 text-[13px] leading-5 text-muted-foreground">
              <p>
                {t("settings.capabilities.deleteConfirm", {
                  name: cap?.display_name ?? "",
                  defaultValue: 'Are you sure you want to delete the capability "{{name}}"? This action cannot be undone.',
                })}
              </p>
              {error ? (
                <p className="rounded-[10px] bg-destructive/10 px-2.5 py-1.5 text-[12px] text-destructive">
                  {error}
                </p>
              ) : null}
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={deleting}>
            {t("settings.capabilities.deleteCancel", { defaultValue: "Cancel" })}
          </AlertDialogCancel>
          <AlertDialogAction
            disabled={deleting}
            onClick={(e) => {
              e.preventDefault();
              onConfirm();
            }}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            {deleting ? (
              <span className="flex items-center gap-1.5">
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                {t("settings.capabilities.deleting", { defaultValue: "Deleting..." })}
              </span>
            ) : (
              t("settings.capabilities.deleteConfirmButton", { defaultValue: "Delete" })
            )}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function RuntimeIcon({ runtime }: { runtime: string }) {
  if (runtime === "mcp") return <Boxes className="h-5 w-5" strokeWidth={1.8} aria-hidden />;
  if (runtime === "process") return <Terminal className="h-5 w-5" strokeWidth={1.8} aria-hidden />;
  return <Brain className="h-5 w-5" strokeWidth={1.8} aria-hidden />;
}

/** 品牌色首字母标记：favicon 加载失败时的离线兜底。 */
function BrandMonogram({
  color,
  name,
}: {
  color?: string | null;
  name: string;
}) {
  return (
    <span
      className="flex h-6 w-6 items-center justify-center rounded text-[12px] font-bold leading-none text-white"
      style={{ backgroundColor: color || "#52525b" }}
      aria-hidden
    >
      {(name.trim().charAt(0) || "?").toUpperCase()}
    </span>
  );
}

/** 能力图标：优先 logo_url（应用/MCP，加载失败回退品牌首字母），其次 emoji icon（技能），否则按 runtime 用默认图标。 */
function CapabilityIcon({ cap }: { cap: Pick<CapabilityInfo, "icon" | "logo_url" | "brand_color" | "display_name" | "runtime"> }) {
  const [imgFailed, setImgFailed] = useState(false);
  if (cap.logo_url && !imgFailed) {
    return (
      <img
        src={cap.logo_url}
        alt=""
        referrerPolicy="no-referrer"
        className="h-6 w-6 rounded object-contain"
        onError={() => setImgFailed(true)}
        aria-hidden
      />
    );
  }
  if (cap.icon) {
    return (
      <span className="text-[22px] leading-none" aria-hidden>
        {cap.icon}
      </span>
    );
  }
  if (cap.brand_color || cap.display_name) {
    return <BrandMonogram color={cap.brand_color} name={cap.display_name} />;
  }
  return <RuntimeIcon runtime={cap.runtime} />;
}

function kindFilterLabel(kind: KindFilter, t: TFunction): string {
  switch (kind) {
    case "prompt":
      return t("settings.capabilities.kindSkill", { defaultValue: "技能" });
    case "process":
      return t("settings.capabilities.kindApp", { defaultValue: "应用" });
    case "mcp":
      return t("settings.capabilities.kindMcp", { defaultValue: "MCP" });
    default:
      return t("settings.capabilities.kindAll", { defaultValue: "全部" });
  }
}

function runtimeLabel(runtime: string, t: TFunction): string {
  if (runtime === "mcp") return t("settings.capabilities.runtimeMcp", { defaultValue: "MCP" });
  if (runtime === "process") return t("settings.capabilities.runtimeProcess", { defaultValue: "应用" });
  return t("settings.capabilities.runtimePrompt", { defaultValue: "技能" });
}

function runtimeBadgeClass(runtime: string): string {
  if (runtime === "mcp") return "bg-sky-500/12 text-sky-700 dark:text-sky-300";
  if (runtime === "process") return "bg-violet-500/12 text-violet-700 dark:text-violet-300";
  return "bg-amber-500/12 text-amber-700 dark:text-amber-300";
}

function sourceLabel(source: string, t: TFunction): string {
  if (source === "workspace") {
    return t("settings.capabilities.sourceWorkspace", { defaultValue: "Custom" });
  }
  if (source === "builtin") {
    return t("settings.capabilities.sourceBuiltin", { defaultValue: "Built-in" });
  }
  if (source === "cli-anything" || source === "harness") {
    return t("settings.capabilities.sourceCliAnything", { defaultValue: "CLI-Anything" });
  }
  if (source === "mcp-preset") {
    return t("settings.capabilities.sourceMcpPreset", { defaultValue: "MCP Preset" });
  }
  // 自定义目录来源（如 `custom`、`harness+custom`）保留原始来源名
  return source;
}

/** 来源筛选匹配：已知来源精确匹配，其余来源名归入「自定义来源」。 */
function matchesSource(source: string, filter: SourceFilter): boolean {
  if (filter === "custom") return !KNOWN_SOURCES.has(source);
  return source === filter;
}

function tierFilterLabel(value: TierFilter, t: TFunction): string {
  if (value === "system") return t("settings.capabilities.tierSystem", { defaultValue: "系统" });
  if (value === "agent") return t("settings.capabilities.tierAgent", { defaultValue: "Agent" });
  if (value === "user") return t("settings.capabilities.tierUser", { defaultValue: "用户" });
  return t("settings.capabilities.filterAll", { defaultValue: "全部" });
}

function availabilityFilterLabel(value: AvailabilityFilter, t: TFunction): string {
  if (value === "available") return t("settings.capabilities.available", { defaultValue: "可用" });
  if (value === "unavailable") return t("settings.capabilities.unavailable", { defaultValue: "不可用" });
  return t("settings.capabilities.filterAll", { defaultValue: "全部" });
}

function sourceFilterLabel(value: SourceFilter, t: TFunction): string {
  switch (value) {
    case "builtin":
      return t("settings.capabilities.sourceBuiltin", { defaultValue: "内置" });
    case "workspace":
      return t("settings.capabilities.sourceWorkspace", { defaultValue: "自定义" });
    case "cli-anything":
      return t("settings.capabilities.sourceCliAnything", { defaultValue: "CLI-Anything" });
    case "mcp-preset":
      return t("settings.capabilities.sourceMcpPreset", { defaultValue: "MCP 预设" });
    case "custom":
      return t("settings.capabilities.sourceCustom", { defaultValue: "自定义来源" });
    default:
      return t("settings.capabilities.filterAll", { defaultValue: "全部" });
  }
}

/** 具体层级值的展示标签（行内徽标用）。 */
function tierLabel(tier: string, t: TFunction): string {
  if (tier === "system") return t("settings.capabilities.tierSystem", { defaultValue: "系统" });
  if (tier === "agent") return t("settings.capabilities.tierAgent", { defaultValue: "Agent" });
  return t("settings.capabilities.tierUser", { defaultValue: "用户" });
}

function tierBadgeClass(tier: string): string {
  if (tier === "system") return "bg-rose-500/12 text-rose-700 dark:text-rose-300";
  if (tier === "agent") return "bg-teal-500/12 text-teal-700 dark:text-teal-300";
  return "bg-zinc-500/12 text-zinc-700 dark:text-zinc-300";
}

/** 单选下拉筛选：选中非「全部」时在触发器上高亮并显示当前值。 */
function FilterMenu({
  label,
  value,
  options,
  onValueChange,
}: {
  label: string;
  value: string;
  options: Array<{ value: string; label: string }>;
  onValueChange: (value: string) => void;
}) {
  const active = value !== "all";
  const current = options.find((o) => o.value === value);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className={cn(
            "flex h-9 items-center gap-1.5 rounded-full border px-3 text-[12px] font-medium transition-colors",
            active
              ? "border-foreground/35 bg-foreground/[0.04] text-foreground"
              : "border-border/60 text-muted-foreground hover:bg-muted/50",
          )}
        >
          {label}
          {active && current ? (
            <span className="rounded-full bg-foreground/[0.07] px-1.5 py-0.5 text-[11px] font-semibold">
              {current.label}
            </span>
          ) : null}
          <ChevronDown className="h-3.5 w-3.5 opacity-70" aria-hidden />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-44">
        <DropdownMenuRadioGroup value={value} onValueChange={(v) => onValueChange(v)}>
          {options.map((o) => (
            <DropdownMenuRadioItem key={o.value} value={o.value} className="text-[13px]">
              {o.label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
