import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Sidebar } from "@/components/Sidebar";
import { ClientProvider } from "@/providers/ClientProvider";
import type { Employee } from "@/lib/types";

const CLIP_MASTER: Employee = {
  id: "clip-master",
  name: "剪辑高手",
  avatar: "🎬",
  system_prompt: "你是一名剪辑高手数字人员工。",
  skills: ["jianying-editor"],
  enabled: true,
  created_at: "2026-08-13T00:00:00Z",
};

function renderSidebar(overrides: {
  employees?: Employee[];
  onOpenTalentMarket?: () => void;
  employeeSectionCollapsed?: boolean;
  onToggleEmployeeSection?: () => void;
  onOpenEmployee?: (employee: Employee) => void;
} = {}) {
  const mockClient = {
    status: "idle",
    onStatus: () => () => {},
  };
  const props = {
    sessions: [],
    activeKey: null,
    loading: false,
    onNewChat: vi.fn(),
    onSelect: vi.fn(),
    onRequestDelete: vi.fn(),
    onTogglePin: vi.fn(),
    onRequestRename: vi.fn(),
    onToggleArchive: vi.fn(),
    onToggleGroup: vi.fn(),
    onRequestRenameProject: vi.fn(),
    onNewChatInProject: vi.fn(),
    onOpenSettings: vi.fn(),
    onOpenApps: vi.fn(),
    onOpenSkills: vi.fn(),
    onOpenAutomations: vi.fn(),
    onOpenSearch: vi.fn(),
    onToggleArchived: vi.fn(),
    onCollapse: vi.fn(),
    employees: overrides.employees,
    onOpenEmployee: overrides.onOpenEmployee,
    // 显式传 undefined 可关闭员工分组（模拟旧行为）
    onOpenTalentMarket:
      "onOpenTalentMarket" in overrides ? overrides.onOpenTalentMarket : vi.fn(),
    employeeSectionCollapsed: overrides.employeeSectionCollapsed,
    onToggleEmployeeSection: overrides.onToggleEmployeeSection ?? vi.fn(),
  };
  return render(
    <ClientProvider client={mockClient as never} token="tok">
      <Sidebar {...props} />
    </ClientProvider>,
  );
}

describe("Sidebar 数字人员工分组", () => {
  it("renders the 数字人员工 title even with zero employees", () => {
    renderSidebar({ employees: [] });

    expect(screen.getByText("数字人员工")).toBeInTheDocument();
    expect(screen.getByText("暂无员工")).toBeInTheDocument();
  });

  it("keeps the title visible when the section is collapsed", () => {
    renderSidebar({ employees: [CLIP_MASTER], employeeSectionCollapsed: true });

    expect(screen.getByText("数字人员工")).toBeInTheDocument();
    expect(screen.queryByText("剪辑高手")).not.toBeInTheDocument();
    expect(screen.getByText("人才市场")).toBeInTheDocument();
  });

  it("shows enabled employees inside the section", () => {
    renderSidebar({
      employees: [
        CLIP_MASTER,
        { ...CLIP_MASTER, id: "paused", name: "停用员工", enabled: false },
      ],
    });

    expect(screen.getByText("剪辑高手")).toBeInTheDocument();
    expect(screen.queryByText("停用员工")).not.toBeInTheDocument();
  });

  it("toggles the section via the collapse button", () => {
    const onToggleEmployeeSection = vi.fn();
    renderSidebar({ employees: [CLIP_MASTER], onToggleEmployeeSection });

    const titleButton = screen.getByText("数字人员工").closest("button");
    expect(titleButton).not.toBeNull();
    expect(titleButton).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(titleButton as HTMLElement);
    expect(onToggleEmployeeSection).toHaveBeenCalledTimes(1);
  });

  it("opens the talent market from the 人才市场 button", () => {
    const onOpenTalentMarket = vi.fn();
    renderSidebar({ employees: [], onOpenTalentMarket });

    fireEvent.click(screen.getByText("人才市场"));
    expect(onOpenTalentMarket).toHaveBeenCalledTimes(1);
  });

  it("starts a chat bound to the employee persona", () => {
    const onOpenEmployee = vi.fn();
    renderSidebar({ employees: [CLIP_MASTER], onOpenEmployee });

    fireEvent.click(screen.getByText("剪辑高手"));
    expect(onOpenEmployee).toHaveBeenCalledWith(CLIP_MASTER);
  });

  it("does not render the group when onOpenTalentMarket is absent", () => {
    // 显式传 undefined 关闭员工分组
    renderSidebar({ employees: [CLIP_MASTER], onOpenTalentMarket: undefined });
    expect(screen.queryByText("数字人员工")).not.toBeInTheDocument();
    expect(screen.queryByText("人才市场")).not.toBeInTheDocument();
  });
});
