import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronLeft, History, Plus, UsersRound } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ThreadShell, type ThreadShellProps } from "@/components/thread/ThreadShell";
import { displayTitle } from "@/lib/chat-groups";
import { relativeTime } from "@/lib/format";
import type { ChatSummary, Employee, SkillSummary, WorkspaceScopePayload } from "@/lib/types";
import { cn } from "@/lib/utils";

/** 技能展示条目（员工 skills 中的技能 id 结合技能目录的描述）。 */
interface SkillPill {
  name: string;
  description: string;
}

/** 历史会话条目按钮：当前会话带 ▸ 游标高亮。 */
function HistoryItemButton({
  session,
  title,
  time,
  preview,
  active,
  onSelect,
}: {
  session: ChatSummary;
  title: string;
  time: string | null;
  preview?: string | null;
  active: boolean;
  onSelect: (key: string) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(session.key)}
      aria-current={active ? "true" : undefined}
      className={cn(
        "flex min-w-0 items-center gap-2.5 rounded-xl px-2.5 py-2.5 text-left transition-colors",
        active ? "bg-muted/60" : "hover:bg-muted/40",
      )}
    >
      <span
        className={cn(
          "shrink-0 text-[11px] leading-none",
          active ? "text-sky-600 dark:text-sky-400" : "text-transparent",
        )}
        aria-hidden
      >
        ▸
      </span>
      <span className="min-w-0 flex-1">
        <span
          className={cn(
            "block truncate text-[13px]",
            active ? "font-semibold text-foreground" : "font-medium text-foreground/90",
          )}
        >
          {title}
        </span>
        {preview ? (
          <span className="mt-0.5 block truncate text-[11.5px] text-muted-foreground">
            {preview}
          </span>
        ) : null}
      </span>
      {time ? (
        <span className="shrink-0 text-[10.5px] text-muted-foreground/70">{time}</span>
      ) : null}
    </button>
  );
}

/**
 * 数字人员工专属对话页：由「和 TA 对话」进入。
 *
 * 布局：左侧为真实对话框（内嵌 ThreadShell，自动打开最近一次历史会话，无历史时进入新建
 * 空态）；右侧（桌面）上方为智能体名称与掌握的技能、下方为聊天历史记录，当前会话带 ▸
 * 游标高亮。选择历史会话、新建会话都停留在本页，不再跳回主聊天。
 */
export function EmployeeChatView({
  employeeId,
  employees,
  skills,
  sessions,
  runningChatIds,
  workspaceOverrides,
  draftWorkspaceScope,
  titleOverrides,
  onWorkspaceScopeChangeForChat,
  createChat,
  shellHostProps,
  onBackToChat,
  hostChromeInset,
}: {
  employeeId: string | null;
  employees: Employee[];
  skills: SkillSummary[];
  sessions: ChatSummary[];
  runningChatIds: Set<string>;
  workspaceOverrides: Record<string, WorkspaceScopePayload>;
  draftWorkspaceScope: WorkspaceScopePayload | null;
  titleOverrides: Record<string, string>;
  onWorkspaceScopeChangeForChat: (
    chatId: string | null,
    scope: WorkspaceScopePayload,
  ) => void;
  createChat: (
    workspaceScope?: WorkspaceScopePayload | null,
    employee?: string | null,
  ) => Promise<string>;
  shellHostProps: Omit<ThreadShellProps, "session" | "title">;
  onBackToChat: () => void;
  hostChromeInset?: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const employee = employees.find((e) => e.id === employeeId) ?? null;

  const skillPills = useMemo<SkillPill[]>(() => {
    if (!employee) return [];
    const byName = new Map(skills.map((s) => [s.name, s]));
    return employee.skills.map((id) => {
      const found = byName.get(id);
      return { name: id, description: found?.description ?? "" };
    });
  }, [employee, skills]);

  const history = useMemo<ChatSummary[]>(() => {
    if (!employeeId) return [];
    return sessions
      .filter((s) => s.employee === employeeId)
      .sort((a, b) =>
        (b.updatedAt ?? b.createdAt ?? "").localeCompare(a.updatedAt ?? a.createdAt ?? ""),
      );
  }, [employeeId, sessions]);

  const latestKey = history[0]?.key ?? null;

  // —— 选中会话状态：进入自动打开最近一条历史；用户主动操作后不再自动跳转 ——
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [mobileHistoryOpen, setMobileHistoryOpen] = useState(false);
  const userChoseNewChatRef = useRef(false);
  const selectedEmployeeIdRef = useRef(employeeId ?? "");
  const autoOpenedRef = useRef(false);

  useEffect(() => {
    if (selectedEmployeeIdRef.current !== employeeId) {
      selectedEmployeeIdRef.current = employeeId ?? "";
      userChoseNewChatRef.current = false;
      autoOpenedRef.current = false;
    }
  }, [employeeId]);

  useEffect(() => {
    if (!employeeId) {
      setSelectedKey(null);
      return;
    }
    if (autoOpenedRef.current) return;
    if (userChoseNewChatRef.current) return;
    autoOpenedRef.current = true;
    setSelectedKey(latestKey);
  }, [employeeId, latestKey]);

  const selectedSession = history.find((h) => h.key === selectedKey) ?? null;
  const selectedChatId = selectedSession?.chatId ?? null;

  const embeddedWorkspaceScope = useMemo<WorkspaceScopePayload | null>(
    () =>
      selectedSession
        ? (workspaceOverrides[selectedChatId ?? ""] ?? selectedSession.workspaceScope ?? null)
        : draftWorkspaceScope,
    [selectedSession, workspaceOverrides, selectedChatId, draftWorkspaceScope],
  );
  const embeddedWorkspaceScopeDisabled = selectedSession
    ? runningChatIds.has(selectedChatId ?? "")
    : false;

  const handleStartNewChat = useCallback(() => {
    userChoseNewChatRef.current = true;
    autoOpenedRef.current = true;
    setSelectedKey(null);
    setMobileHistoryOpen(false);
  }, []);

  const handleSelectHistory = useCallback((key: string) => {
    userChoseNewChatRef.current = false;
    setSelectedKey(key);
    setMobileHistoryOpen(false);
  }, []);

  /** 内嵌 ThreadShell 新建会话：绑定本页员工，创建后停留在本页并选中新会话。 */
  const handleEmbeddedCreateChat = useCallback(
    async (
      workspaceScope?: WorkspaceScopePayload | null,
      // eslint-disable-next-line @typescript-eslint/no-unused-vars -- 保持 onCreateChat 接口签名
      _employeeId?: string | null,
    ): Promise<string | null> => {
      if (!employeeId) return null;
      userChoseNewChatRef.current = true;
      autoOpenedRef.current = true;
      const chatId = await createChat(workspaceScope ?? embeddedWorkspaceScope, employeeId);
      setSelectedKey(`websocket:${chatId}`);
      return chatId;
    },
    [createChat, embeddedWorkspaceScope, employeeId],
  );

  const handleEmbeddedWorkspaceScopeChange = useCallback(
    (scope: WorkspaceScopePayload) => {
      onWorkspaceScopeChangeForChat(selectedChatId, scope);
    },
    [onWorkspaceScopeChangeForChat, selectedChatId],
  );

  const embeddedTitle = selectedSession
    ? displayTitle(selectedSession, titleOverrides, tx("chat.newChat", "新对话"))
    : employee
      ? t("employeeChat.title", { name: employee.name, defaultValue: "与 {{name}} 对话" })
      : "";

  if (!employee) {
    return (
      <main className="relative flex h-full min-w-0 flex-1 items-center justify-center p-6">
        <div className="flex h-48 w-full max-w-[400px] items-center justify-center rounded-[24px] border border-dashed border-border/60 bg-card/40 text-sm text-muted-foreground">
          <UsersRound className="mr-2 h-4 w-4" aria-hidden />
          {tx("employeeChat.missing", "该员工不存在或已被删除。")}
        </div>
      </main>
    );
  }

  const renderHistoryList = (compact = false) =>
    history.length ? (
      <ul className={cn(compact ? "space-y-0.5" : "mt-3 min-h-0 flex-1 space-y-1 overflow-y-auto pr-1 [scrollbar-gutter:stable]")}>
        {history.map((session) => {
          const title = displayTitle(session, titleOverrides, tx("chat.newChat", "新对话"));
          return (
            <li key={session.key}>
              <HistoryItemButton
                session={session}
                title={title}
                time={relativeTime(session.updatedAt ?? session.createdAt)}
                preview={session.preview}
                active={session.key === selectedKey}
                onSelect={handleSelectHistory}
              />
            </li>
          );
        })}
      </ul>
    ) : (
      <p className="mt-3 rounded-2xl border border-dashed border-border/60 bg-card/40 px-4 py-8 text-center text-[12.5px] text-muted-foreground">
        {tx(
          "employeeChat.noHistory",
          "还没有与该员工的对话记录，点击「开始新对话」发起首次对话。",
        )}
      </p>
    );

  return (
    <main className="relative flex h-full min-w-0 flex-1 flex-col lg:flex-row">
      {/* 左侧：内嵌对话框 */}
      <div className="relative flex min-w-0 flex-1 flex-col">
        {/* 移动端压缩身份条 + 历史菜单 */}
        <div className="cyber-glass-panel relative overflow-hidden lg:hidden">
          <div className="flex items-center gap-2 px-3 py-2">
            <button
              type="button"
              onClick={onBackToChat}
              aria-label={tx("settings.backToChat", "回到聊天")}
              className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
            >
              <ChevronLeft className="h-4 w-4" aria-hidden />
            </button>
            <span
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] bg-muted/70 text-[15px] leading-none"
              aria-hidden
            >
              {employee.avatar || "🧑‍💼"}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-center gap-1.5">
                <span className="truncate text-[13px] font-semibold text-foreground">
                  {employee.name}
                </span>
                {employee.title ? (
                  <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium text-muted-foreground/80">
                    {employee.title}
                  </span>
                ) : null}
              </div>
              <div className="mt-0.5 flex flex-wrap items-center gap-1">
                {employee.enabled ? (
                  <span className="text-[10px] text-emerald-700 dark:text-emerald-300">
                    {tx("employeesView.enabled", "启用")}
                  </span>
                ) : (
                  <span className="text-[10px] text-muted-foreground">
                    {tx("employeesView.disabled", "停用")}
                  </span>
                )}
                {skillPills.slice(0, 2).map((pill) => (
                  <span
                    key={pill.name}
                    className="max-w-[6rem] truncate rounded-full bg-muted/60 px-1.5 py-0.5 text-[10px] text-foreground/70"
                  >
                    {pill.name}
                  </span>
                ))}
                {skillPills.length > 2 ? (
                  <span className="text-[10px] text-muted-foreground/70">
                    +{skillPills.length - 2}
                  </span>
                ) : null}
              </div>
            </div>
            <div className="relative shrink-0">
              <button
                type="button"
                onClick={() => setMobileHistoryOpen((v) => !v)}
                aria-expanded={mobileHistoryOpen}
                aria-label={tx("employeeChat.history", "历史对话")}
                className="inline-flex h-8 items-center gap-1 rounded-lg px-2 text-[12px] font-medium text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
              >
                <History className="h-4 w-4" aria-hidden />
                <ChevronDown
                  className={cn("h-3.5 w-3.5 transition-transform", mobileHistoryOpen && "rotate-180")}
                  aria-hidden
                />
              </button>
              {mobileHistoryOpen ? (
                <>
                  <div
                    className="fixed inset-0 z-20"
                    onClick={() => setMobileHistoryOpen(false)}
                    aria-hidden
                  />
                  <div
                    data-testid="mobile-history-menu"
                    className="absolute right-0 top-full z-30 mt-1 max-h-[min(60vh,28rem)] w-[min(20rem,calc(100vw-1.5rem))] overflow-y-auto rounded-2xl border border-border/60 bg-card p-2 shadow-xl"
                  >
                    <button
                      type="button"
                      onClick={handleStartNewChat}
                      aria-current={selectedKey === null ? "true" : undefined}
                      className={cn(
                        "flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-[12.5px] font-medium transition-colors",
                        selectedKey === null
                          ? "bg-sky-500/10 text-sky-700 dark:text-sky-300"
                          : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
                      )}
                    >
                      <Plus className="h-3.5 w-3.5 shrink-0" aria-hidden />
                      {tx("employeeChat.startChat", "开始新对话")}
                    </button>
                    <div className="mt-1 border-t border-border/50 pt-1">
                      {renderHistoryList(true)}
                    </div>
                  </div>
                </>
              ) : null}
            </div>
          </div>
        </div>

        {/* 内嵌 ThreadShell：复用主聊天宿主 props，覆盖会话/标题/员工只读/新建留在本页 */}
        <div className="relative flex min-h-0 flex-1 flex-col">
          <ThreadShell
            {...shellHostProps}
            session={selectedSession}
            title={embeddedTitle}
            employeeMode="readonly"
            draftEmployee={employee}
            onNewChat={handleStartNewChat}
            onCreateChat={handleEmbeddedCreateChat}
            workspaceScope={embeddedWorkspaceScope}
            workspaceScopeDisabled={embeddedWorkspaceScopeDisabled}
            onWorkspaceScopeChange={handleEmbeddedWorkspaceScopeChange}
            onOpenEmployee={undefined}
          />
        </div>
      </div>

      {/* 右侧面板（桌面）：身份 + 技能 + 历史 */}
      <aside
        className={cn(
          "cyber-glass-panel relative hidden w-[300px] shrink-0 flex-col overflow-hidden lg:flex",
          hostChromeInset && "lg:pt-[4.25rem]",
        )}
      >
        <div className="px-5 pb-4 pt-5">
          <div className="flex items-center gap-3">
            <span
              className="flex h-12 w-12 shrink-0 items-center justify-center rounded-[16px] bg-muted/70 text-[24px] leading-none"
              aria-hidden
            >
              {employee.avatar || "🧑‍💼"}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-center gap-1.5">
                <h2 className="truncate text-[15px] font-semibold text-foreground">
                  {employee.name}
                </h2>
                {employee.enabled ? (
                  <span className="shrink-0 rounded-full bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-semibold leading-none text-emerald-700 dark:text-emerald-300">
                    {tx("employeesView.enabled", "启用")}
                  </span>
                ) : (
                  <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold leading-none text-muted-foreground">
                    {tx("employeesView.disabled", "停用")}
                  </span>
                )}
              </div>
              {employee.title ? (
                <span className="mt-1 block truncate rounded-full bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground/80">
                  {employee.title}
                </span>
              ) : null}
            </div>
          </div>
          {employee.system_prompt?.trim() ? (
            <p className="mt-3 line-clamp-3 whitespace-pre-line text-[12px] leading-5 text-muted-foreground/90">
              {employee.system_prompt}
            </p>
          ) : null}
        </div>

        <div className="border-t border-border/55 px-5 py-4">
          <h3 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
            {tx("employeeChat.skills", "掌握的技能")}
          </h3>
          {skillPills.length ? (
            <div className="mt-2.5 flex flex-wrap gap-1.5">
              {skillPills.map((pill) => (
                <span
                  key={pill.name}
                  title={pill.description || pill.name}
                  className="inline-flex items-center gap-1 rounded-full border border-border/60 bg-muted/50 px-2.5 py-1 text-[12px] font-medium text-foreground/85"
                >
                  <span className="text-[11px] leading-none" aria-hidden>
                    🧩
                  </span>
                  <span className="truncate">{pill.name}</span>
                </span>
              ))}
            </div>
          ) : (
            <p className="mt-2 text-[12.5px] text-muted-foreground">
              {tx("employeeChat.noSkills", "该员工暂无绑定技能。")}
            </p>
          )}
        </div>

        <div className="flex min-h-0 flex-1 flex-col border-t border-border/55 px-5 py-4">
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
              {tx("employeeChat.history", "历史对话")}
            </h3>
            <button
              type="button"
              onClick={handleStartNewChat}
              aria-current={selectedKey === null ? "true" : undefined}
              className={cn(
                "inline-flex items-center gap-1 rounded-full px-2 py-1 text-[11.5px] font-medium transition-colors",
                selectedKey === null
                  ? "bg-sky-500/10 text-sky-700 dark:text-sky-300"
                  : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
              )}
            >
              <Plus className="h-3 w-3" aria-hidden />
              {tx("employeeChat.startChat", "开始新对话")}
            </button>
          </div>
          {renderHistoryList()}
        </div>
      </aside>
    </main>
  );
}
