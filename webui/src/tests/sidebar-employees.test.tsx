import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Sidebar } from "@/components/Sidebar";
import { ClientProvider } from "@/providers/ClientProvider";
import type { Employee } from "@/lib/types";

function renderSidebar(overrides: {
  employees?: Employee[];
  onOpenEmployees?: () => void;
  activeUtility?: "apps" | "skills" | "automations" | "employees" | null;
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
    onOpenEmployees: overrides.onOpenEmployees ?? vi.fn(),
    onToggleArchived: vi.fn(),
    onCollapse: vi.fn(),
    employees: overrides.employees,
    activeUtility: overrides.activeUtility ?? null,
  };
  return render(
    <ClientProvider client={mockClient as never} token="tok">
      <Sidebar {...props} />
    </ClientProvider>,
  );
}

describe("Sidebar 数字人员工入口", () => {
  it("renders the 数字人员工 tab", () => {
    renderSidebar();
    expect(
      screen.getByRole("button", { name: "数字员工" }),
    ).toBeInTheDocument();
  });

  it("opens the employees view via the 数字人员工 tab", () => {
    const onOpenEmployees = vi.fn();
    renderSidebar({ onOpenEmployees });

    fireEvent.click(screen.getByRole("button", { name: "数字员工" }));
    expect(onOpenEmployees).toHaveBeenCalledTimes(1);
  });

  it("marks the tab active when the employees view is open", () => {
    renderSidebar({ activeUtility: "employees" });
    const button = screen.getByRole("button", { name: "数字员工" });
    expect(button).toHaveAttribute("aria-current", "page");
  });
});
