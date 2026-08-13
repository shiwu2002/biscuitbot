/**
 * 全链路追踪日志抽屉面板。
 *
 * 接收来自 WebSocket 的 `agent_trace` 事件，以时间线视图展示
 * 智能体每轮对话的完整执行链路：状态机步骤、工具调用、LLM 调用。
 */

import { useMemo, useState } from "react";
import {
  Activity,
  ChevronDown,
  ChevronRight,
  Circle,
  Clock,
  Cpu,
  Wrench,
  XCircle,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";

// ── 类型 ────────────────────────────────────────────────────────────

export interface TraceEntry {
  turn_id: string;
  phase: "turn_state" | "tool_call" | "llm_call" | "error" | string;
  step: string;
  status: "started" | "completed" | "failed" | string;
  duration_ms?: number | null;
  detail?: Record<string, unknown>;
  timestamp: number;
}

interface TraceLogPanelProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  traces: TraceEntry[];
  onClear: () => void;
}

// ── phase 样式映射 ──────────────────────────────────────────────────

const PHASE_CONFIG: Record<
  string,
  { icon: typeof Activity; color: string; label: string }
> = {
  turn_state: { icon: Circle, color: "text-cyan-500", label: "状态" },
  tool_call: { icon: Wrench, color: "text-purple-500", label: "工具" },
  llm_call: { icon: Cpu, color: "text-blue-500", label: "LLM" },
  error: { icon: XCircle, color: "text-red-500", label: "错误" },
};

const STATUS_COLOR: Record<string, string> = {
  started: "text-yellow-500",
  completed: "text-green-500",
  failed: "text-red-500",
};

// ── 工具函数 ────────────────────────────────────────────────────────

function formatDuration(ms?: number | null): string {
  if (ms == null) return "";
  if (ms < 1) return `${ms.toFixed(1)}ms`;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

function formatDetail(detail: Record<string, unknown> | undefined, phase: string): string {
  if (!detail) return "";
  const parts: string[] = [];
  if (phase === "tool_call") {
    const args = detail.arguments as string | undefined;
    if (args) parts.push(`args=${args}`);
    const error = detail.error as string | undefined;
    if (error) parts.push(`error=${error}`);
    const d = detail.detail as string | undefined;
    if (d && d !== "(empty)") parts.push(d);
  } else if (phase === "llm_call") {
    const prompt = detail.prompt_tokens as number | undefined;
    const completion = detail.completion_tokens as number | undefined;
    if (prompt || completion) parts.push(`tokens=in:${prompt ?? 0} out:${completion ?? 0}`);
    const iter = detail.iteration as number | undefined;
    if (iter != null) parts.push(`iter=${iter}`);
    if (detail.has_tool_calls) parts.push("tool_calls=yes");
  } else if (phase === "turn_state") {
    const event = detail.event as string | undefined;
    if (event) parts.push(`event=${event}`);
    const error = detail.error as string | undefined;
    if (error) parts.push(`error=${error}`);
  }
  return parts.join(" ");
}

// ── 单条 trace 行 ───────────────────────────────────────────────────

function TraceRow({ entry }: { entry: TraceEntry }) {
  const [expanded, setExpanded] = useState(false);
  const config = PHASE_CONFIG[entry.phase] ?? PHASE_CONFIG.error;
  const Icon = config.icon;
  const indent = entry.phase === "tool_call" || entry.phase === "llm_call" ? "ml-4" : "";
  const detailStr = formatDetail(entry.detail, entry.phase);
  const hasDetail = detailStr.length > 0;

  return (
    <div className={cn("flex flex-col py-0.5", indent)}>
      <div className="flex items-center gap-2 text-xs font-mono">
        <Icon className={cn("h-3 w-3 shrink-0", config.color)} />
        <span className="font-medium text-foreground">{entry.step}</span>
        <span className={cn("font-medium", STATUS_COLOR[entry.status] ?? "text-muted-foreground")}>
          {entry.status}
        </span>
        {entry.duration_ms != null && (
          <span className="flex items-center gap-0.5 text-muted-foreground">
            <Clock className="h-3 w-3" />
            {formatDuration(entry.duration_ms)}
          </span>
        )}
        {hasDetail && (
          <button
            type="button"
            onClick={() => setExpanded(!expanded)}
            className="text-muted-foreground hover:text-foreground"
          >
            {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          </button>
        )}
      </div>
      {expanded && hasDetail && (
        <div className="ml-5 mt-0.5 text-xs text-muted-foreground font-mono break-all">
          {detailStr}
        </div>
      )}
    </div>
  );
}

// ── 按 turn 分组 ────────────────────────────────────────────────────

interface TurnGroup {
  turnId: string;
  entries: TraceEntry[];
  totalMs: number;
  startTime: number;
}

function groupByTurn(traces: TraceEntry[]): TurnGroup[] {
  const groups = new Map<string, TraceEntry[]>();
  for (const t of traces) {
    const list = groups.get(t.turn_id) ?? [];
    list.push(t);
    groups.set(t.turn_id, list);
  }
  const result: TurnGroup[] = [];
  for (const [turnId, entries] of groups) {
    entries.sort((a, b) => a.timestamp - b.timestamp);
    const totalMs = entries
      .filter((e) => e.phase === "turn_state")
      .reduce((sum, e) => sum + (e.duration_ms ?? 0), 0);
    result.push({
      turnId,
      entries,
      totalMs,
      startTime: entries[0]?.timestamp ?? 0,
    });
  }
  result.sort((a, b) => b.startTime - a.startTime);
  return result;
}

// ── 主面板 ──────────────────────────────────────────────────────────

export function TraceLogPanel({ open, onOpenChange, traces, onClear }: TraceLogPanelProps) {
  const turnGroups = useMemo(() => groupByTurn(traces), [traces]);
  const [collapsedTurns, setCollapsedTurns] = useState<Set<string>>(new Set());

  const toggleTurn = (turnId: string) => {
    setCollapsedTurns((prev) => {
      const next = new Set(prev);
      if (next.has(turnId)) next.delete(turnId);
      else next.add(turnId);
      return next;
    });
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg flex flex-col">
        <div className="flex items-center justify-between px-4 py-3 pr-12 border-b">
          <SheetTitle className="flex items-center gap-2">
            <Activity className="h-4 w-4" />
            全链路追踪
          </SheetTitle>
          <Button variant="ghost" size="sm" onClick={onClear} className="text-xs">
            清空
          </Button>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-2">
          {turnGroups.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-muted-foreground gap-2">
              <Activity className="h-6 w-6 opacity-50" />
              <p className="text-sm">暂无追踪记录</p>
              <p className="text-xs px-2 text-center">
                发送新消息后，这里会实时显示智能体的完整执行链路
              </p>
            </div>
          ) : (
            <div className="space-y-3">
              {turnGroups.map((group) => {
                const collapsed = collapsedTurns.has(group.turnId);
                const turnLabel = group.turnId.split(":").pop()?.slice(-8) ?? group.turnId;
                return (
                  <div
                    key={group.turnId}
                    className="rounded-lg border border-border/50 overflow-hidden"
                  >
                    <button
                      type="button"
                      onClick={() => toggleTurn(group.turnId)}
                      className="w-full flex items-center gap-2 px-3 py-2 bg-muted/30 hover:bg-muted/50 transition-colors"
                    >
                      {collapsed ? (
                        <ChevronRight className="h-3 w-3" />
                      ) : (
                        <ChevronDown className="h-3 w-3" />
                      )}
                      <span className="text-xs font-medium font-mono">turn {turnLabel}</span>
                      <span className="text-xs text-muted-foreground">
                        {group.entries.length} steps
                      </span>
                      {group.totalMs > 0 && (
                        <span className="flex items-center gap-0.5 text-xs text-muted-foreground ml-auto">
                          <Clock className="h-3 w-3" />
                          {formatDuration(group.totalMs)}
                        </span>
                      )}
                    </button>
                    {!collapsed && (
                      <div className="px-3 py-1.5 space-y-0.5">
                        {group.entries.map((entry, i) => (
                          <TraceRow key={`${entry.turn_id}-${i}`} entry={entry} />
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
