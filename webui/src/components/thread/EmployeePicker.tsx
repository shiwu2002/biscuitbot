import { UserRound, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { Employee } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 数字人员工选择器。
 *
 * - ``select``：新建会话前预选员工（hero 态），可下拉选择、可清除。
 * - ``readonly``：已有会话只读展示绑定的员工（历史对话中员工固定不可切换），
 *   未绑定员工时不显示任何标记（即为默认主智能体）。
 */
export function EmployeePicker({
  mode,
  employees,
  selected,
  onChange,
  onOpenEmployee,
}: {
  mode: "select" | "readonly";
  employees: Employee[];
  selected: Employee | null;
  onChange?: (employee: Employee | null) => void;
  /** readonly 模式下点击徽标跳转到员工专属页（查看员工，不切换绑定）。 */
  onOpenEmployee?: (employee: Employee) => void;
}) {
  const { t } = useTranslation();

  if (mode === "readonly") {
    if (!selected) return null;
    const badgeText = t("thread.composer.employee.talkingWith", {
      name: `${selected.avatar ? `${selected.avatar} ` : ""}${selected.name}`,
      defaultValue: "正在与 {{name}} 对话",
    });
    const badgeClass = cn(
      "inline-flex max-w-full items-center gap-1.5 rounded-full border border-blue-400/30 bg-blue-500/8 py-1 pl-2 pr-2 text-[12px] font-medium text-blue-700 dark:text-blue-300",
    );
    const fixedTitle = t("thread.composer.employee.fixed", {
      name: selected.name,
      defaultValue: "该会话固定与 {{name}} 对话",
    });
    return (
      <div className="flex items-center gap-2 px-3 pb-1.5 sm:px-4">
        {onOpenEmployee ? (
          <button
            type="button"
            title={t("thread.composer.employee.openPage", {
              name: selected.name,
              defaultValue: "查看{{name}}的专属对话页（会话固定不可切换）",
            })}
            aria-label={badgeText}
            onClick={() => onOpenEmployee(selected)}
            className={cn(
              badgeClass,
              "cursor-pointer transition-colors hover:bg-blue-500/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            )}
          >
            <span className="truncate">{badgeText}</span>
          </button>
        ) : (
          <span className={badgeClass} title={fixedTitle}>
            <span className="truncate">{badgeText}</span>
          </span>
        )}
      </div>
    );
  }

  if (employees.length === 0 || !onChange) return null;

  const enabled = employees.filter((e) => e.enabled);

  if (selected) {
    return (
      <div className="flex items-center gap-2 px-3 pb-1.5 sm:px-4">
        <button
          type="button"
          title={t("thread.composer.employee.clear", { defaultValue: "改为与主智能体对话" })}
          aria-label={t("thread.composer.employee.clear", { defaultValue: "改为与主智能体对话" })}
          onClick={() => onChange(null)}
          className={cn(
            "inline-flex max-w-full items-center gap-1.5 rounded-full border border-blue-400/30 bg-blue-500/8 py-1 pl-2 pr-1.5 text-[12px] font-medium text-blue-700 dark:text-blue-300",
            "transition-colors hover:bg-blue-500/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          )}
        >
          <span className="truncate">
            {t("thread.composer.employee.talkingWith", {
              name: `${selected.avatar ? `${selected.avatar} ` : ""}${selected.name}`,
              defaultValue: "正在与 {{name}} 对话",
            })}
          </span>
          <X className="h-3.5 w-3.5 shrink-0" aria-hidden />
        </button>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2 px-3 pb-1.5 sm:px-4">
      <DropdownMenu>
        <DropdownMenuTrigger
          asChild
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border border-border/60 px-2.5 py-1 text-[12px] font-medium text-muted-foreground",
            "transition-colors hover:bg-muted/55 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          )}
        >
          <button type="button">
            <UserRound className="h-3.5 w-3.5" aria-hidden />
            {t("thread.composer.employee.pick", { defaultValue: "数字人员工" })}
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-56">
          <DropdownMenuLabel className="text-[12px]">
            {t("thread.composer.employee.section", { defaultValue: "选择数字人员工" })}
          </DropdownMenuLabel>
          {enabled.length ? (
            enabled.map((employee) => (
              <DropdownMenuItem
                key={employee.id}
                onSelect={() => onChange(employee)}
                className="gap-2"
              >
                <span className="text-[15px] leading-none" aria-hidden>
                  {employee.avatar || "🧑‍💼"}
                </span>
                <span className="truncate">{employee.name}</span>
              </DropdownMenuItem>
            ))
          ) : (
            <DropdownMenuItem disabled className="text-[12.5px]">
              {t("thread.composer.employee.empty", { defaultValue: "暂无可用员工" })}
            </DropdownMenuItem>
          )}
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => onChange(null)} className="text-[12.5px]">
            {t("thread.composer.employee.none", { defaultValue: "主智能体（不绑定员工）" })}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
