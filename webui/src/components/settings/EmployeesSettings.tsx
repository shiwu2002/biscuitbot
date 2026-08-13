import { useState } from "react";
import { Loader2, Plus, Trash2, UsersRound } from "lucide-react";
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
  avatar: "",
  system_prompt: "",
  skills: [],
  enabled: true,
};

/** 数字人员工管理：列表 + 新建/编辑（绑定技能多选）+ 删除。 */
export function EmployeesSettings({
  employees,
  skills,
  onChanged,
}: {
  employees: Employee[];
  /** 可用于绑定的技能名列表（来自技能目录）。 */
  skills: string[];
  onChanged?: () => void;
}) {
  const { t } = useTranslation();
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

  return (
    <div className="space-y-7">
      <section className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <p className="max-w-[680px] text-[13px] leading-5 text-muted-foreground">
          {t("settings.employees.description", {
            defaultValue:
              "数字人员工是带专属 persona 的拟人化代理：与其对话时智能体沉浸在该角色中，并只加载其绑定的技能。",
          })}
        </p>
        <Button
          type="button"
          size="sm"
          onClick={() => setEditor({ employee: null })}
          className="shrink-0"
        >
          <Plus className="mr-1.5 h-4 w-4" aria-hidden />
          {t("settings.employees.create", { defaultValue: "新建员工" })}
        </Button>
      </section>

      <section>
        <div className="flex items-center justify-between border-b border-border/45 pb-3">
          <h2 className="mb-2 px-1 text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
            {t("settings.employees.list", { defaultValue: "数字人员工" })}
          </h2>
          <span className="rounded-full bg-muted px-2.5 py-1 text-[12px] font-medium text-muted-foreground">
            {employees.length}
          </span>
        </div>
        {employees.length ? (
          <div className="grid gap-x-10 gap-y-1 py-3 md:grid-cols-2">
            {employees.map((employee) => (
              <EmployeeCatalogRow
                key={employee.id}
                employee={employee}
                onSelect={(emp) => setEditor({ employee: emp })}
                onDelete={setDeleteTarget}
              />
            ))}
          </div>
        ) : (
          <div className="px-3 py-12 text-center text-sm text-muted-foreground">
            {t("settings.employees.empty", { defaultValue: "还没有数字人员工。" })}
          </div>
        )}
      </section>

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
          skills={skills}
          open
          onOpenChange={() => setEditor(null)}
          onSaved={() => {
            setEditor(null);
            onChanged?.();
          }}
        />
      ) : null}
    </div>
  );
}

function EmployeeCatalogRow({
  employee,
  onSelect,
  onDelete,
}: {
  employee: Employee;
  onSelect: (employee: Employee) => void;
  onDelete: (employee: Employee) => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      className={cn(
        "group flex min-w-0 items-center gap-3 rounded-[16px] px-3 py-3 text-left transition-colors",
        "hover:bg-muted/45",
        !employee.enabled && "opacity-65",
      )}
    >
      <button
        type="button"
        aria-label={t("settings.employees.openEditor", {
          name: employee.name,
          defaultValue: "编辑员工 {{name}}",
        })}
        onClick={() => onSelect(employee)}
        className="flex min-w-0 flex-1 items-center gap-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-[16px]"
      >
        <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-[14px] bg-muted/70 text-[22px] leading-none">
          {employee.avatar || "🧑‍💼"}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <h3 className="truncate text-[15px] font-semibold leading-5 text-foreground">
              {employee.name}
            </h3>
            {employee.enabled ? (
              <span className="shrink-0 rounded-full bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-semibold leading-none text-emerald-700 dark:text-emerald-300">
                {t("settings.employees.enabled", { defaultValue: "启用" })}
              </span>
            ) : (
              <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold leading-none text-muted-foreground">
                {t("settings.employees.disabled", { defaultValue: "停用" })}
              </span>
            )}
          </div>
          <p className="mt-1 line-clamp-2 text-[13px] leading-5 text-muted-foreground">
            {employee.system_prompt}
          </p>
          {employee.skills.length ? (
            <p className="mt-1 truncate text-[12px] leading-4 text-muted-foreground/80">
              {t("settings.employees.boundSkills", {
                skills: employee.skills.join(" · "),
                defaultValue: "绑定技能：{{skills}}",
              })}
            </p>
          ) : null}
        </div>
      </button>
      <button
        type="button"
        aria-label={t("settings.employees.delete", {
          name: employee.name,
          defaultValue: "删除员工 {{name}}",
        })}
        title={t("settings.employees.deleteTitle", { defaultValue: "删除员工" })}
        onClick={(e) => {
          e.stopPropagation();
          onDelete(employee);
        }}
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Trash2 className="h-4 w-4" aria-hidden />
      </button>
    </div>
  );
}

function EmployeeEditorDialog({
  employee,
  skills,
  open,
  onOpenChange,
  onSaved,
}: {
  employee: Employee | null;
  skills: string[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [form, setForm] = useState<EmployeeValues>(() => {
    if (employee) {
      return {
        name: employee.name,
        avatar: employee.avatar ?? "",
        system_prompt: employee.system_prompt,
        skills: employee.skills,
        enabled: employee.enabled,
      };
    }
    return EMPTY_FORM;
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const set = (patch: Partial<EmployeeValues>) => setForm((current) => ({ ...current, ...patch }));

  const toggleSkill = (skill: string) => {
    const current = form.skills ?? [];
    set({
      skills: current.includes(skill)
        ? current.filter((item) => item !== skill)
        : [...current, skill],
    });
  };

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
          ? t("settings.employees.duplicateId", {
              defaultValue: "员工 id 已存在，请更换名称或稍后再试。",
            })
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
              ? t("settings.employees.editTitle", {
                  name: employee.name,
                  defaultValue: "编辑员工 {{name}}",
                })
              : t("settings.employees.createTitle", { defaultValue: "新建数字人员工" })}
          </DialogTitle>
          <DialogDescription className="sr-only">
            {t("settings.employees.editorDescription", {
              defaultValue: "配置员工的名称、头像、角色提示词与绑定技能。",
            })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <label className="block space-y-1.5">
            <span className="text-[12.5px] font-medium text-foreground/85">
              {t("settings.employees.name", { defaultValue: "名称" })}
            </span>
            <Input
              value={form.name ?? ""}
              onChange={(e) => set({ name: e.target.value })}
              placeholder={t("settings.employees.namePlaceholder", {
                defaultValue: "例如：剪辑高手",
              })}
            />
          </label>

          <label className="block space-y-1.5">
            <span className="text-[12.5px] font-medium text-foreground/85">
              {t("settings.employees.avatar", { defaultValue: "头像（emoji）" })}
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
              {t("settings.employees.systemPrompt", { defaultValue: "角色提示词（persona）" })}
            </span>
            <Textarea
              value={form.system_prompt ?? ""}
              onChange={(e) => set({ system_prompt: e.target.value })}
              rows={6}
              placeholder={t("settings.employees.systemPromptPlaceholder", {
                defaultValue: "你是一名「{{name}}」数字人员工……",
              })}
            />
          </label>

          <div className="space-y-1.5">
            <span className="text-[12.5px] font-medium text-foreground/85">
              {t("settings.employees.boundSkills", {
                defaultValue: "绑定技能",
              })}
            </span>
            {skills.length ? (
              <div className="grid max-h-44 grid-cols-2 gap-x-3 gap-y-1 overflow-y-auto rounded-[14px] border border-border/45 bg-muted/20 p-2.5">
                {skills.map((skill) => {
                  const checked = (form.skills ?? []).includes(skill);
                  return (
                    <label
                      key={skill}
                      className={cn(
                        "flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-[13px]",
                        "transition-colors hover:bg-muted/60",
                      )}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleSkill(skill)}
                        className="h-3.5 w-3.5 accent-foreground"
                      />
                      <span className="truncate">{skill}</span>
                    </label>
                  );
                })}
              </div>
            ) : (
              <p className="text-[12.5px] text-muted-foreground">
                {t("settings.employees.noSkills", {
                  defaultValue: "暂无可用技能可绑定。",
                })}
              </p>
            )}
          </div>

          <label className="flex cursor-pointer items-center gap-2 text-[13px]">
            <input
              type="checkbox"
              checked={form.enabled !== false}
              onChange={(e) => set({ enabled: e.target.checked })}
              className="h-3.5 w-3.5 accent-foreground"
            />
            {t("settings.employees.enable", { defaultValue: "启用该员工" })}
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
            {t("settings.employees.cancel", { defaultValue: "取消" })}
          </Button>
          <Button
            type="button"
            disabled={saving || !form.name?.trim() || !form.system_prompt?.trim()}
            onClick={handleSave}
          >
            {saving ? (
              <span className="flex items-center gap-1.5">
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                {t("settings.employees.saving", { defaultValue: "保存中…" })}
              </span>
            ) : (
              t("settings.employees.save", { defaultValue: "保存" })
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
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle className="flex items-center gap-2">
            <UsersRound className="h-4 w-4" aria-hidden />
            {t("settings.employees.deleteTitle", { defaultValue: "删除员工" })}
          </AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-2 text-[13px] leading-5 text-muted-foreground">
              <p>
                {t("settings.employees.deleteConfirm", {
                  name: employee?.name ?? "",
                  defaultValue: "确定删除员工「{{name}}」吗？该员工的历史会话不受影响，将回退为主智能体行为。",
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
            {t("settings.employees.cancel", { defaultValue: "取消" })}
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
                {t("settings.employees.deleting", { defaultValue: "删除中…" })}
              </span>
            ) : (
              t("settings.employees.delete", { defaultValue: "删除" })
            )}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
