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

/** Hero 态的数字人员工选择器：预选员工后显示「正在与 <员工> 对话」徽标，
 * 未预选时提供一个下拉入口选择要对话的员工。仅 hero 态显示。 */
export function EmployeePicker({
  isHero,
  employees,
  selected,
  onChange,
}: {
  isHero: boolean;
  employees: Employee[];
  selected: Employee | null;
  onChange?: (employee: Employee | null) => void;
}) {
  const { t } = useTranslation();
  if (!isHero || employees.length === 0 || !onChange) return null;

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
