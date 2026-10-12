import { useEffect, useState } from "react";
import { AlertCircle, CheckCircle2, Info } from "lucide-react";

import { subscribeToasts, type AppToast } from "@/lib/toast";
import { cn } from "@/lib/utils";

const TOAST_DURATION_MS = 3_500;
const MAX_VISIBLE_TOASTS = 3;

const TOAST_ICON: Record<AppToast["kind"], typeof Info> = {
  success: CheckCircle2,
  error: AlertCircle,
  info: Info,
};

/**
 * 全局 toast 出口：订阅 lib/toast 的事件并渲染顶部居中的气泡，
 * 自动 3.5s 消失。挂在 App 根部一次即可。
 */
export function ToastHost() {
  const [toasts, setToasts] = useState<AppToast[]>([]);

  useEffect(
    () =>
      subscribeToasts((toast) => {
        setToasts((prev) => [...prev.slice(-(MAX_VISIBLE_TOASTS - 1)), toast]);
        window.setTimeout(() => {
          setToasts((prev) => prev.filter((item) => item.id !== toast.id));
        }, TOAST_DURATION_MS);
      }),
    [],
  );

  if (toasts.length === 0) return null;

  return (
    <div className="pointer-events-none fixed left-1/2 top-[calc(0.75rem+env(safe-area-inset-top))] z-[70] flex w-full max-w-[calc(100vw-1rem)] -translate-x-1/2 flex-col items-center gap-2">
      {toasts.map((toast) => {
        const Icon = TOAST_ICON[toast.kind];
        return (
          <div
            key={toast.id}
            role="status"
            className={cn(
              "pointer-events-auto flex max-w-full items-center gap-2 rounded-full border px-4 py-2",
              "bg-popover text-sm font-medium text-popover-foreground shadow-lg",
              "animate-in fade-in-0 slide-in-from-top-2 duration-200",
              toast.kind === "success" && "border-emerald-500/40",
              toast.kind === "error" && "border-destructive/50",
              toast.kind === "info" && "border-border/70",
            )}
          >
            <Icon
              aria-hidden
              className={cn(
                "h-4 w-4 shrink-0",
                toast.kind === "success" && "text-emerald-500",
                toast.kind === "error" && "text-destructive",
                toast.kind === "info" && "text-muted-foreground",
              )}
            />
            <span className="min-w-0 truncate">{toast.message}</span>
          </div>
        );
      })}
    </div>
  );
}
