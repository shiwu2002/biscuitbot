import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function CyberPageFrame({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex min-h-0 flex-1 flex-col overflow-hidden",
        className,
      )}
    >
      <div className="min-h-0 flex-1 overflow-y-auto scrollbar-thin scrollbar-track-transparent">
        <div className="mx-auto w-full max-w-[88rem] px-4 py-5 sm:px-6 sm:py-6">
          {children}
        </div>
      </div>
    </div>
  );
}

export function CyberPanel({
  children,
  className,
  title,
  icon,
}: {
  children: ReactNode;
  className?: string;
  title?: string;
  icon?: ReactNode;
}) {
  return (
    <section
      className={cn(
        "cyber-glass-panel cyber-scanline relative overflow-hidden rounded-xl p-4 sm:p-5",
        className,
      )}
    >
      {title ? (
        <header className="mb-4 flex items-center gap-2">
          {icon ? (
            <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-primary/12 text-primary shadow-[0_0_14px_hsl(var(--cyber-glow-soft)/0.35)]">
              {icon}
            </span>
          ) : null}
          <h2 className="cyber-mono-label text-muted-foreground">
            {title}
          </h2>
        </header>
      ) : null}
      {children}
    </section>
  );
}
