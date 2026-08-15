import { useState } from "react";
import { ChevronLeft, Loader2, MessageCircle, Pencil, Plus, Sparkles, Trash2, UsersRound } from "lucide-react";
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
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  createEmployee,
  deleteEmployee,
  updateEmployee,
  type EmployeeValues,
} from "@/lib/api";
import type { Employee } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

const EMPTY_FORM: EmployeeValues = {
  name: "",
  title: "",
  avatar: "",
  system_prompt: "",
  enabled: true,
};

/**
 * 数字人员工视图（由侧边栏「数字人员工」tab 打开）。
 *
 * 卡面式展示每位员工：头像、代号、职位、角色提示词（成果）与启停状态；
 * 技能不再手动分配，由员工自主选用，因此卡面与编辑器均不出现技能。
 * 支持「和 TA 对话」（绑定该员工的新会话）、编辑、删除与新建。
 */
export function EmployeesView({
  employees,
  onChanged,
  onPick,
  onOpenTalentMarket,
  onBackToChat,
  hostChromeInset,
}: {
  employees: Employee[];
  onChanged?: () => void;
  /** 和某位员工对话（复用打开绑定该员工的新会话的逻辑）。 */
  onPick?: (employee: Employee) => void;
  /** 打开人才市场（注册表目录页）。 */
  onOpenTalentMarket?: () => void;
  onBackToChat: () => void;
  hostChromeInset?: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const { token } = useClient();
  const [editor, setEditor] = useState<{ employee: Employee | null } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Employee | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const handleConfirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteEmployee(token, deleteTarget.id);
      setDeleteTarget(null);
      onChanged?.();
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeleting(false);
    }
  };

  const ordered = [...employees].sort((a, b) => {
    if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
    return 0;
  });

  return (
    <main className="min-w-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]">
      <div
        className={cn(
          "mx-auto w-full max-w-[920px] px-4 py-6 sm:px-8 sm:py-8 lg:py-12",
          hostChromeInset && "pt-[4.25rem] sm:pt-[4.25rem] lg:pt-[4.75rem]",
        )}
      >
        <div className="mb-7">
          <button
            type="button"
            onClick={onBackToChat}
            className="mb-4 inline-flex items-center gap-1.5 rounded-full px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground lg:hidden"
          >
            <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
            {t("settings.backToChat")}
          </button>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
            <div className="min-w-0">
              <h1 className="text-[24px] font-normal leading-tight tracking-tight text-foreground sm:text-[28px]">
                {tx("employeesView.title", "数字人员工")}
              </h1>
              <p className="mt-2 max-w-[680px] text-[13px] leading-5 text-muted-foreground">
                {tx(
                  "employeesView.description",
                  "每位员工都有自己的代号、职位与角色提示词（成果）。技能不手动分配，由员工自主选用。可与某位员工直接对话，主智能体也能在对话中点名请 TA 协助。",
                )}
              </p>
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              {onOpenTalentMarket ? (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={onOpenTalentMarket}
                  className="h-9 rounded-[10px] px-4"
                >
                  <Sparkles className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                  {tx("employeesView.talentMarket", "人才市场")}
                </Button>
              ) : null}
              <Button
                type="button"
                size="sm"
                onClick={() => setEditor({ employee: null })}
                className="h-9 shrink-0 rounded-[10px] px-4"
              >
                <Plus className="mr-1.5 h-4 w-4" aria-hidden />
                {tx("employeesView.create", "新建员工")}
              </Button>
            </div>
          </div>
        </div>

        {ordered.length === 0 ? (
          <div className="cyber-glass-panel relative flex h-48 items-center justify-center overflow-hidden rounded-[24px] border border-dashed border-border/60 text-sm text-muted-foreground">
            <UsersRound className="mr-2 h-4 w-4" aria-hidden />
            {tx("employeesView.empty", "还没有数字人员工。")}
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-4 md:gap-5 lg:grid-cols-3">
            {ordered.map((employee) => (
              <article
                key={employee.id}
                onClick={() => onPick?.(employee)}
                title={t("employeesView.openPage", {
                  defaultValue: "点击查看{{name}}的专属对话页",
                  name: employee.name,
                })}
                className={cn(
                  "cyber-glass-panel group flex min-w-0 cursor-pointer flex-col overflow-hidden rounded-2xl p-4 transition-colors group-hover:border-cyan-400/40 sm:aspect-square",
                  !employee.enabled && "opacity-70",
                )}
              >
                <div className="flex min-w-0 items-center gap-3">
                  <span
                    className="flex h-11 w-11 shrink-0 items-center justify-center rounded-[14px] bg-muted/70 text-[22px] leading-none"
                    aria-hidden
                  >
                    {employee.avatar || "🧑‍💼"}
                  </span>
                  <div className="min-w-0 flex-1">
                    <h3 className="truncate text-[15px] font-semibold leading-6 text-foreground">
                      {employee.name}
                    </h3>
                    {employee.title ? (
                      <p className="truncate text-[11px] leading-4 text-muted-foreground/80">
                        {employee.title}
                      </p>
                    ) : null}
                  </div>
                </div>

                <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                  {employee.builtin ? (
                    <span className="rounded-full bg-cyan-500/10 px-1.5 py-0.5 text-[10px] font-semibold leading-none text-cyan-700 dark:text-cyan-300">
                      {tx("employeesView.builtin", "内置")}
                    </span>
                  ) : null}
                  {employee.enabled ? (
                    <span className="rounded-full bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-semibold leading-none text-emerald-700 dark:text-emerald-300">
                      {tx("employeesView.enabled", "启用")}
                    </span>
                  ) : (
                    <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold leading-none text-muted-foreground">
                      {tx("employeesView.disabled", "停用")}
                    </span>
                  )}
                </div>

                <p className="mt-2.5 line-clamp-4 whitespace-pre-line text-[12.5px] leading-5 text-muted-foreground">
                  {employee.system_prompt?.trim()
                    ? employee.system_prompt
                    : tx("employeesView.noPersona", "该员工暂无角色提示词")}
                </p>

                <div className="mt-auto flex items-center justify-end gap-1.5 pt-3">
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    disabled={!onPick}
                    onClick={(e) => {
                      e.stopPropagation();
                      onPick?.(employee);
                    }}
                    className="h-8 rounded-[9px] px-3 text-[12.5px]"
                  >
                    <MessageCircle className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                    {tx("employeesView.chat", "和 TA 对话")}
                  </Button>
                  {!employee.builtin ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={(e) => {
                        e.stopPropagation();
                        setEditor({ employee });
                      }}
                      className="h-8 rounded-[9px] px-3 text-[12.5px]"
                    >
                      <Pencil className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                      {tx("employeesView.edit", "编辑")}
                    </Button>
                  ) : null}
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    title={t("settings.employees.delete", {
                      name: employee.name,
                      defaultValue: "删除员工 {{name}}",
                    })}
                    onClick={(e) => {
                      e.stopPropagation();
                      setDeleteTarget(employee);
                    }}
                    className="h-8 rounded-[9px] px-3 text-[12.5px] text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                  >
                    <Trash2 className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                    {tx("employeesView.delete", "删除")}
                  </Button>
                </div>
              </article>
            ))}
          </div>
        )}

        <EmployeeDeleteDialog
          employee={deleteTarget}
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

        {editor ? (
          <EmployeeEditorDialog
            employee={editor.employee}
            open
            onOpenChange={() => setEditor(null)}
            onSaved={() => {
              setEditor(null);
              onChanged?.();
            }}
          />
        ) : null}
      </div>
    </main>
  );
}

function EmployeeEditorDialog({
  employee,
  open,
  onOpenChange,
  onSaved,
}: {
  employee: Employee | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const { token } = useClient();
  const [form, setForm] = useState<EmployeeValues>(() => {
    if (employee) {
      return {
        name: employee.name,
        title: employee.title ?? "",
        avatar: employee.avatar ?? "",
        system_prompt: employee.system_prompt,
        enabled: employee.enabled,
      };
    }
    return EMPTY_FORM;
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const set = (patch: Partial<EmployeeValues>) => setForm((current) => ({ ...current, ...patch }));

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      if (employee) {
        await updateEmployee(token, employee.id, form);
      } else {
        await createEmployee(token, form);
      }
      onSaved();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const status = (err as { status?: number })?.status;
      setError(
        status === 409
          ? tx("employeesView.duplicateId", "员工 id 已存在，请更换名称或稍后再试。")
          : message,
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[min(34rem,calc(100vw-1rem))] max-w-none sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {employee
              ? tx("employeesView.editTitle", "编辑员工 {{name}}").replace("{{name}}", employee.name)
              : tx("employeesView.createTitle", "新建数字人员工")}
          </DialogTitle>
          <DialogDescription className="sr-only">
            {tx("employeesView.editorDescription", "配置员工的代号、职位、头像与角色提示词。")}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <label className="block space-y-1.5">
            <span className="text-[12.5px] font-medium text-foreground/85">
              {tx("employeesView.name", "代号")}
            </span>
            <Input
              value={form.name ?? ""}
              onChange={(e) => set({ name: e.target.value })}
              placeholder={tx("employeesView.namePlaceholder", "例如：剪影")}
            />
          </label>

          <label className="block space-y-1.5">
            <span className="text-[12.5px] font-medium text-foreground/85">
              {tx("employeesView.title", "职位")}
            </span>
            <Input
              value={form.title ?? ""}
              onChange={(e) => set({ title: e.target.value })}
              placeholder={tx("employeesView.titlePlaceholder", "例如：剪辑")}
              maxLength={24}
            />
          </label>

          <label className="block space-y-1.5">
            <span className="text-[12.5px] font-medium text-foreground/85">
              {tx("employeesView.avatar", "头像（emoji）")}
            </span>
            <Input
              value={form.avatar ?? ""}
              onChange={(e) => set({ avatar: e.target.value })}
              placeholder="🎬"
              maxLength={8}
            />
          </label>

          <label className="block space-y-1.5">
            <span className="text-[12.5px] font-medium text-foreground/85">
              {tx("employeesView.systemPrompt", "角色提示词（成果）")}
            </span>
            <Textarea
              value={form.system_prompt ?? ""}
              onChange={(e) => set({ system_prompt: e.target.value })}
              rows={6}
              placeholder={tx("employeesView.systemPromptPlaceholder", "你是「{{name}}」数字人员工……")}
            />
          </label>

          <label className="flex cursor-pointer items-center gap-2 text-[13px]">
            <input
              type="checkbox"
              checked={form.enabled !== false}
              onChange={(e) => set({ enabled: e.target.checked })}
              className="h-3.5 w-3.5 accent-foreground"
            />
            {tx("employeesView.enable", "启用该员工")}
          </label>

          {error ? (
            <p className="rounded-[10px] bg-destructive/10 px-2.5 py-1.5 text-[12px] text-destructive">
              {error}
            </p>
          ) : null}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="ghost"
            disabled={saving}
            onClick={() => onOpenChange(false)}
          >
            {tx("employeesView.cancel", "取消")}
          </Button>
          <Button
            type="button"
            disabled={saving || !form.name?.trim() || !form.system_prompt?.trim()}
            onClick={handleSave}
          >
            {saving ? (
              <span className="flex items-center gap-1.5">
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                {tx("employeesView.saving", "保存中…")}
              </span>
            ) : (
              tx("employeesView.save", "保存")
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function EmployeeDeleteDialog({
  employee,
  open,
  deleting,
  error,
  onOpenChange,
  onConfirm,
}: {
  employee: Employee | null;
  open: boolean;
  deleting: boolean;
  error: string | null;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle className="flex items-center gap-2">
            <UsersRound className="h-4 w-4" aria-hidden />
            {tx("employeesView.deleteTitle", "删除员工")}
          </AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-2 text-[13px] leading-5 text-muted-foreground">
              <p>
                {tx("employeesView.deleteConfirm", "确定删除员工「{{name}}」吗？该员工的历史会话不受影响，将回退为主智能体行为。").replace(
                  "{{name}}",
                  employee?.name ?? "",
                )}
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
            {tx("employeesView.cancel", "取消")}
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
                {tx("employeesView.deleting", "删除中…")}
              </span>
            ) : (
              tx("employeesView.delete", "删除")
            )}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
