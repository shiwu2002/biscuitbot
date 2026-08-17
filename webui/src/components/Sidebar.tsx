import { useState, type ReactNode } from "react";
import {
  Archive,
  BookOpen,
  CalendarClock,
  LayoutDashboard,
  Menu,
  Search,
  Settings,
  Sparkles,
  SquarePen,
  UsersRound,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { ChatList } from "@/components/ChatList";
import { ConnectionBadge } from "@/components/ConnectionBadge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import type {
  ChatSummary,
  Employee,
  SidebarViewState,
} from "@/lib/types";
import { cn } from "@/lib/utils";

interface SidebarProps {
  sessions: ChatSummary[];
  activeKey: string | null;
  loading: boolean;
  onNewChat: () => void;
  onSelect: (key: string) => void;
  onRequestDelete: (key: string, label: string) => void;
  onTogglePin: (key: string) => void;
  onRequestRename: (key: string, label: string) => void;
  onToggleArchive: (key: string) => void;
  onToggleGroup: (groupId: string) => void;
  onRequestRenameProject: (projectKey: string, label: string) => void;
  onNewChatInProject: (projectPath: string, projectName: string) => void;
  onOpenSettings: () => void;
  onOpenCapabilities: () => void;
  onOpenAutomations: () => void;
  onOpenSearch: () => void;
  /** 打开仪表盘视图（Token 用量 / 图表分析）。 */
  onOpenDashboard: () => void;
  /** 打开数字人员工视图（卡面展示与管理，原设置里的员工 tab 迁移至此）。 */
  onOpenEmployees: () => void;
  /** 打开个人知识库视图（上传/检索文档与图片）。 */
  onOpenKnowledge: () => void;
  /** 数字人员工目录：用于会话行显示绑定的员工头像/代号（ChatList）。 */
  employees?: Employee[];
  activeUtility?: "skills" | "capabilities" | "automations" | "employees" | "dashboard" | "knowledge" | null;
  onToggleArchived: () => void;
  onCollapse: () => void;
  onExpand?: () => void;
  containActionMenus?: boolean;
  collapsed?: boolean;
  pinnedKeys?: string[];
  archivedKeys?: string[];
  titleOverrides?: Record<string, string>;
  projectNameOverrides?: Record<string, string>;
  collapsedGroups?: Record<string, boolean>;
  runningChatIds?: string[];
  updatedChatIds?: string[];
  viewState?: SidebarViewState;
  showArchived?: boolean;
  archivedCount?: number;
  defaultWorkspacePath?: string | null;
  hostChromeInset?: boolean;
}

type NavigatorWithUserAgentData = Navigator & {
  userAgentData?: { platform?: string };
};

function isApplePlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const platform = navigator.platform || "";
  const userAgentPlatform =
    (navigator as NavigatorWithUserAgentData).userAgentData?.platform || "";
  return /mac|iphone|ipad|ipod/i.test(`${platform} ${userAgentPlatform}`);
}

function newChatShortcutLabel(): string {
  return isApplePlatform() ? "⌘⇧O" : "Ctrl+Shift+O";
}

export function Sidebar(props: SidebarProps) {
  const { t } = useTranslation();
  const [menuPortalContainer, setMenuPortalContainer] =
    useState<HTMLElement | null>(null);
  const collapsed = Boolean(props.collapsed);
  const toggleLabel = t("thread.header.toggleSidebar");
  const newChatShortcut = newChatShortcutLabel();

  return (
    <nav
      ref={props.containActionMenus ? setMenuPortalContainer : undefined}
      aria-label={t("sidebar.navigation")}
      className={cn(
        "flex h-full w-full min-w-0 flex-col text-sidebar-foreground",
        props.hostChromeInset ? "bg-transparent" : "cyber-sidebar",
        !props.hostChromeInset && "border-r border-[hsl(var(--cyber-panel-border)/0.22)]",
      )}
    >
      <div
        className={cn(
          "flex items-center gap-1 px-3 pb-2.5",
          props.hostChromeInset ? "pt-[2.85rem]" : "pt-3",
          collapsed ? "w-14 justify-start" : "justify-start",
        )}
      >
        {!collapsed && !props.hostChromeInset && (
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("sidebar.collapse")}
            onClick={props.onCollapse}
            className="h-7 w-7 shrink-0 rounded-lg text-muted-foreground/85 hover:bg-sidebar-accent/75 hover:text-sidebar-foreground"
          >
            <Menu className="h-3.5 w-3.5" />
          </Button>
        )}
        <button
          type="button"
          aria-label={collapsed ? toggleLabel : undefined}
          aria-hidden={collapsed ? undefined : true}
          title={collapsed ? toggleLabel : undefined}
          onClick={collapsed ? props.onExpand : undefined}
          tabIndex={collapsed ? 0 : -1}
          className={cn(
            "flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-xl transition-colors",
            collapsed
              ? "-ml-0.5 hover:bg-sidebar-accent/75"
              : "pointer-events-none",
          )}
        >
          <img
            src="/brand/biscuitbot_icon.png"
            alt=""
            className="h-8 w-8 select-none object-contain"
            draggable={false}
          />
        </button>
      </div>

      <div
        className={cn(
          "space-y-1.5 px-2 pb-2",
          collapsed && "flex w-14 flex-col items-center px-0",
        )}
      >
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.dashboard", { defaultValue: "数据概览" })}
          onClick={props.onOpenDashboard}
          active={props.activeUtility === "dashboard"}
          icon={<LayoutDashboard className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.newChat")}
          onClick={props.onNewChat}
          icon={<SquarePen className="h-4 w-4" />}
          shortcut={newChatShortcut}
          ariaKeyShortcuts="Meta+Shift+O Control+Shift+O"
        />
        {collapsed ? (
          <SidebarActionButton
            collapsed={collapsed}
            label={t("sidebar.searchAria")}
            onClick={props.onOpenSearch}
            icon={<Search className="h-4 w-4" />}
          />
        ) : null}
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.capabilities.title", { defaultValue: "能力中心" })}
          onClick={props.onOpenCapabilities}
          active={props.activeUtility === "capabilities"}
          icon={<Sparkles className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.automations", { defaultValue: "Automations" })}
          onClick={props.onOpenAutomations}
          active={props.activeUtility === "automations"}
          icon={<CalendarClock className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.employees.title", { defaultValue: "数字员工" })}
          onClick={props.onOpenEmployees}
          active={props.activeUtility === "employees"}
          icon={<UsersRound className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.knowledge", { defaultValue: "个人知识" })}
          onClick={props.onOpenKnowledge}
          active={props.activeUtility === "knowledge"}
          icon={<BookOpen className="h-4 w-4" />}
        />
        {props.archivedCount ? (
          <SidebarActionButton
            collapsed={collapsed}
            label={props.showArchived ? t("chat.hideArchived") : t("chat.showArchived")}
            onClick={props.onToggleArchived}
            icon={<Archive className="h-4 w-4" />}
          />
        ) : null}
      </div>
      <div
        className={cn(
          "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden transition-opacity duration-200",
          collapsed && "pointer-events-none opacity-0",
        )}
      >
        {!collapsed && (
          <>
            <div className="px-2 pt-1.5">
              <SidebarActionButton
                collapsed={false}
                label={t("sidebar.searchAria")}
                onClick={props.onOpenSearch}
                icon={<Search className="h-4 w-4" />}
              />
            </div>
            <ChatList
            sessions={props.sessions}
            activeKey={props.activeKey}
            loading={props.loading}
            emptyLabel={t("chat.noSessions")}
            onSelect={props.onSelect}
            onRequestDelete={props.onRequestDelete}
            onTogglePin={props.onTogglePin}
            onRequestRename={props.onRequestRename}
            onToggleArchive={props.onToggleArchive}
            onToggleGroup={props.onToggleGroup}
            onRequestRenameProject={props.onRequestRenameProject}
            onNewChatInProject={props.onNewChatInProject}
            pinnedKeys={props.pinnedKeys}
            archivedKeys={props.archivedKeys}
            titleOverrides={props.titleOverrides}
            projectNameOverrides={props.projectNameOverrides}
            collapsedGroups={props.collapsedGroups}
            runningChatIds={props.runningChatIds}
            updatedChatIds={props.updatedChatIds}
            density={props.viewState?.density}
            showPreviews={props.viewState?.show_previews}
            showTimestamps={props.viewState?.show_timestamps}
            sort={props.viewState?.sort}
            showArchived={props.showArchived}
            defaultWorkspacePath={props.defaultWorkspacePath}
            employees={props.employees}
            actionMenuPortalContainer={
              props.containActionMenus ? menuPortalContainer : undefined
            }
          />
            </>
        )}
      </div>
      <Separator className="bg-sidebar-border/50" />
      <div
        className={cn(
          "flex items-center gap-1 px-2.5 py-2.5 text-xs",
          collapsed && "w-14 flex-col px-0",
        )}
      >
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.settings")}
          onClick={props.onOpenSettings}
          className={collapsed ? undefined : "flex-1"}
          icon={<Settings className="h-4 w-4" />}
        />
        <ConnectionBadge />
      </div>
    </nav>
  );
}

function SidebarActionButton({
  collapsed,
  label,
  icon,
  onClick,
  active = false,
  className,
  shortcut,
  ariaKeyShortcuts,
}: {
  collapsed: boolean;
  label: string;
  icon: ReactNode;
  onClick: () => void;
  active?: boolean;
  className?: string;
  shortcut?: string;
  ariaKeyShortcuts?: string;
}) {
  const title = shortcut ? `${label} (${shortcut})` : collapsed ? label : undefined;

  return (
    <Button
      type="button"
      variant="ghost"
      aria-label={label}
      aria-current={active ? "page" : undefined}
      aria-keyshortcuts={ariaKeyShortcuts}
      title={title}
      onClick={() => onClick()}
      className={cn(
        "group relative h-8 min-w-0 gap-2 overflow-hidden rounded-full font-medium text-sidebar-foreground/85 hover:bg-sidebar-accent/75 hover:text-sidebar-foreground",
        "transition-[width,padding,border-radius,color,background-color] duration-300 ease-out",
        collapsed
          ? "w-9 justify-center gap-0 rounded-xl px-0"
          : "w-full justify-start gap-2 px-3 text-[12.5px]",
        active && "cyber-nav-active shadow-none",
        !active && "hover:bg-sidebar-accent/75 hover:text-sidebar-foreground",
        className,
      )}
    >
      {active ? (
        <span
          aria-hidden
          className="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-r-full bg-[hsl(var(--cyber-glow)/0.95)] shadow-[0_0_8px_hsl(var(--cyber-glow)/0.7)]"
        />
      ) : null}
      <span
        className={cn(
          "flex w-4 shrink-0 items-center justify-center transition-transform duration-300 ease-out",
          active && "drop-shadow-[0_0_5px_hsl(var(--cyber-glow-soft)/0.85)]",
          collapsed ? "translate-x-0" : "translate-x-0",
        )}
        aria-hidden
      >
        {icon}
      </span>
      <span
        className={cn(
          "min-w-0 overflow-hidden truncate whitespace-nowrap tracking-wide transition-[max-width,opacity,transform] duration-200 ease-out",
          collapsed
            ? "max-w-0 -translate-x-1 opacity-0"
            : "max-w-[12rem] translate-x-0 opacity-100",
        )}
      >
        {label}
      </span>
    </Button>
  );
}
