import { Menu, Moon, Sun } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface ThreadHeaderProps {
  title: string;
  onToggleSidebar: () => void;
  theme: "light" | "dark";
  onToggleTheme: () => void;
  hideSidebarToggleForHostChrome?: boolean;
  hostChromeTitleInset?: boolean;
  hideThemeButton?: boolean;
  minimal?: boolean;
  promptNavigatorAction?: ReactNode;
  sessionInfoAction?: ReactNode;
  traceAction?: ReactNode;
}

export function ThreadHeader({
  title,
  onToggleSidebar,
  theme,
  onToggleTheme,
  hideSidebarToggleForHostChrome = false,
  hostChromeTitleInset = false,
  hideThemeButton = false,
  minimal = false,
  promptNavigatorAction,
  sessionInfoAction,
  traceAction,
}: ThreadHeaderProps) {
  const { t } = useTranslation();

  return (
    <div
      className={cn(
        "cyber-glass-panel cyber-scanline relative z-10 flex items-center justify-between gap-3 border-b-0 px-3 py-2",
        minimal && "h-11",
        !minimal && hostChromeTitleInset && "lg:pl-[128px]",
        hideSidebarToggleForHostChrome && "lg:pr-[136px]",
      )}
      style={{ borderRadius: 0 }}
    >
      <div className="relative z-10 flex min-w-0 items-center gap-2">
        {!hideSidebarToggleForHostChrome ? (
          <div className="flex h-7 w-7 shrink-0 select-none items-center justify-center rounded-lg bg-[hsl(var(--cyber-glow)/0.15)] shadow-[0_0_14px_hsl(var(--cyber-glow-soft)/0.35)]">
            <img
              src="/brand/biscuitbot_icon.png?v=20260903"
              alt=""
              className="h-6 w-6 select-none object-contain"
              draggable={false}
            />
          </div>
        ) : null}
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("thread.header.toggleSidebar")}
          onClick={onToggleSidebar}
          className={cn(
            "h-7 w-7 rounded-md text-muted-foreground hover:bg-[hsl(var(--cyber-glow)/0.14)] hover:text-foreground hover:shadow-[0_0_0_1px_hsl(var(--cyber-glow)/0.35)] cyber-btn-glow",
            hideSidebarToggleForHostChrome && "lg:hidden",
          )}
        >
          <Menu className="h-3.5 w-3.5" />
        </Button>
        {!minimal ? (
          <div className="cyber-mono-label flex min-w-0 items-center rounded-md border border-[hsl(var(--cyber-panel-border)/0.35)] bg-[hsl(var(--background)/0.4)] px-2 py-1 text-muted-foreground shadow-[inset_0_0_0_1px_hsl(var(--cyber-glow)/0.08)]">
            <span className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full bg-[hsl(var(--cyber-glow))] shadow-[0_0_6px_hsl(var(--cyber-glow)/0.85)]" />
            <span className="max-w-[min(60vw,32rem)] truncate">{title}</span>
          </div>
        ) : null}
      </div>

      <div className="relative z-10 ml-auto flex shrink-0 items-center gap-1">
        {sessionInfoAction}
        {promptNavigatorAction}
        {traceAction}
        {!hideThemeButton ? (
          <ThemeButton
            theme={theme}
            onToggleTheme={onToggleTheme}
            label={t("thread.header.toggleTheme")}
          />
        ) : null}
      </div>

      {!minimal ? (
        <div aria-hidden className="pointer-events-none absolute inset-x-0 top-full h-4" />
      ) : null}
    </div>
  );
}

function ThemeButton({
  theme,
  onToggleTheme,
  label,
  className,
}: {
  theme: "light" | "dark";
  onToggleTheme: () => void;
  label: string;
  className?: string;
}) {
  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label={label}
      onClick={onToggleTheme}
      className={cn(
        "host-no-drag h-8 w-8 rounded-full text-muted-foreground/85 hover:bg-accent/40 hover:text-foreground",
        className,
      )}
    >
      {theme === "dark" ? (
        <Sun className="h-4 w-4" />
      ) : (
        <Moon className="h-4 w-4" />
      )}
    </Button>
  );
}
