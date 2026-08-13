import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { EmployeeChatView } from "@/components/employees/EmployeeChatView";
import type {
  ChatSummary,
  Employee,
  SkillSummary,
  WorkspaceScopePayload,
} from "@/lib/types";

const CLIP_MASTER: Employee = {
  id: "clip-master",
  name: "阿伟",
  title: "剪辑",
  avatar: "🎬",
  system_prompt: "你是「阿伟」，团队里的剪辑高手。",
  skills: ["jianying-editor", "seedance"],
  enabled: true,
  created_at: "2026-08-13T00:00:00Z",
};

const VIDEO_MASTER: Employee = {
  id: "video-master",
  name: "沐辰",
  title: "视频",
  avatar: "🎥",
  system_prompt: "你是「沐辰」，视频专家。",
  skills: [],
  enabled: true,
  created_at: "2026-08-13T00:00:00Z",
};

const SKILLS: SkillSummary[] = [
  {
    name: "jianying-editor",
    description: "剪映自动化剪辑",
    source: "builtin",
    tier: "agent",
    available: true,
  },
  {
    name: "seedance",
    description: "可灵视频生成",
    source: "builtin",
    tier: "agent",
    available: true,
  },
];

function summary(key: string, overrides: Partial<ChatSummary> = {}): ChatSummary {
  return {
    key,
    channel: "websocket",
    chatId: key.replace(/^websocket:/, ""),
    createdAt: null,
    updatedAt: "2026-08-12T00:00:00Z",
    preview: "帮我剪一支宣传片",
    employee: "clip-master",
    ...overrides,
  };
}

const clipHistory = [
  summary("websocket:clip-1", { updatedAt: "2026-08-13T10:00:00Z", title: "产品宣传片" }),
  summary("websocket:clip-2", { updatedAt: "2026-08-11T10:00:00Z", preview: "口播视频成片" }),
];

const videoHistory = [
  summary("websocket:video-1", {
    employee: "video-master",
    updatedAt: "2026-08-13T09:00:00Z",
    preview: "沐辰的会话",
  }),
];

// 内嵌 ThreadShell 打桩：记录关键 props，暴露 mock-create / mock-new / mock-scope 动作。
vi.mock("@/components/thread/ThreadShell", () => ({
  ThreadShell: ({
    session,
    title,
    employeeMode,
    draftEmployee,
    onCreateChat,
    onNewChat,
    workspaceScope,
    workspaceScopeDisabled,
    onWorkspaceScopeChange,
  }: {
    session: ChatSummary | null;
    title: string;
    employeeMode?: string;
    draftEmployee?: Employee | null;
    onCreateChat?: (
      workspaceScope?: WorkspaceScopePayload | null,
      employeeId?: string | null,
    ) => Promise<string | null>;
    onNewChat?: () => void;
    workspaceScope?: WorkspaceScopePayload | null;
    workspaceScopeDisabled?: boolean;
    onWorkspaceScopeChange?: (scope: WorkspaceScopePayload) => void;
  }) => (
    <div
      data-testid="mock-shell"
      data-session={session?.key ?? ""}
      data-title={title ?? ""}
      data-mode={employeeMode ?? ""}
      data-employee={draftEmployee?.id ?? ""}
      data-scope={workspaceScope ? JSON.stringify(workspaceScope) : ""}
      data-disabled={workspaceScopeDisabled ? "true" : ""}
    >
      <button
        type="button"
        onClick={() =>
          onCreateChat?.({ project_path: "/mock", access_mode: "restricted" }, "clip-master")
        }
      >
        mock-create
      </button>
      <button type="button" onClick={() => onNewChat?.()}>
        mock-new
      </button>
      <button
        type="button"
        onClick={() => onWorkspaceScopeChange?.({ project_path: "/changed", access_mode: "full" })}
      >
        mock-scope
      </button>
    </div>
  ),
}));

function TestHarness({
  employeeId,
  sessions,
  createChatMock,
  workspaceOverrides = {},
  draftWorkspaceScope = null,
  runningChatIds = new Set<string>(),
  onWorkspaceScopeChangeForChat = vi.fn(),
}: {
  employeeId: string | null;
  sessions: ChatSummary[];
  createChatMock: ReturnType<typeof vi.fn>;
  workspaceOverrides?: Record<string, WorkspaceScopePayload>;
  draftWorkspaceScope?: WorkspaceScopePayload | null;
  runningChatIds?: Set<string>;
  onWorkspaceScopeChangeForChat?: (chatId: string | null, scope: WorkspaceScopePayload) => void;
}) {
  const [list, setList] = useState<ChatSummary[]>(sessions);
  // 模拟 useSessions.createChat 的乐观插入
  const handleCreate = async (
    scope?: WorkspaceScopePayload | null,
    employee?: string | null,
  ): Promise<string> => {
    const id = await createChatMock(scope, employee);
    setList((prev) => [
      {
        key: `websocket:${id}`,
        channel: "websocket",
        chatId: id,
        createdAt: null,
        updatedAt: "2026-08-13T12:00:00Z",
        title: "",
        preview: "",
        workspaceScope: scope ?? null,
        employee: employee ?? null,
      },
      ...prev,
    ]);
    return id;
  };
  return (
    <EmployeeChatView
      employeeId={employeeId}
      employees={[CLIP_MASTER, VIDEO_MASTER]}
      skills={SKILLS}
      sessions={list}
      runningChatIds={runningChatIds}
      workspaceOverrides={workspaceOverrides}
      draftWorkspaceScope={draftWorkspaceScope}
      titleOverrides={{}}
      onWorkspaceScopeChangeForChat={onWorkspaceScopeChangeForChat}
      createChat={handleCreate}
      shellHostProps={{ onToggleSidebar: vi.fn() }}
      onBackToChat={vi.fn()}
    />
  );
}

describe("EmployeeChatView 员工专属对话页（对话框式布局）", () => {
  it("展示员工身份与掌握的技能", () => {
    render(
      <TestHarness employeeId="clip-master" sessions={clipHistory} createChatMock={vi.fn()} />,
    );
    expect(screen.getByRole("heading", { name: "阿伟" })).toBeInTheDocument();
    // 标题同时出现在桌面身份区与移动端压缩身份条
    expect(screen.getAllByText("剪辑").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("掌握的技能")).toBeInTheDocument();
    // 技能名同时出现在桌面技能区与移动端身份条
    expect(screen.getAllByText("jianying-editor").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("seedance").length).toBeGreaterThanOrEqual(1);
  });

  it("进入专属页自动打开最近一条历史会话并带 ▸ 游标高亮", () => {
    render(
      <TestHarness employeeId="clip-master" sessions={clipHistory} createChatMock={vi.fn()} />,
    );
    const shell = screen.getByTestId("mock-shell");
    // 自动打开最近一条 clip-1
    expect(shell).toHaveAttribute("data-session", "websocket:clip-1");
    // 内嵌 ThreadShell 为只读员工模式并预选本页员工
    expect(shell).toHaveAttribute("data-mode", "readonly");
    expect(shell).toHaveAttribute("data-employee", "clip-master");
    // 历史列表中 clip-1 带游标与 aria-current
    const clip1 = screen.getByRole("button", { name: /产品宣传片/ });
    expect(clip1).toHaveAttribute("aria-current", "true");
    expect(within(clip1).getByText("▸")).toBeInTheDocument();
    // clip-2 不高亮
    const clip2 = screen.getByRole("button", { name: /口播视频成片/ });
    expect(clip2).not.toHaveAttribute("aria-current");
  });

  it("点击历史会话原地切换（不跳回主聊天）", async () => {
    render(
      <TestHarness employeeId="clip-master" sessions={clipHistory} createChatMock={vi.fn()} />,
    );
    const shell = screen.getByTestId("mock-shell");
    expect(shell).toHaveAttribute("data-session", "websocket:clip-1");

    fireEvent.click(screen.getByRole("button", { name: /口播视频成片/ }));

    await waitFor(() => expect(shell).toHaveAttribute("data-session", "websocket:clip-2"));
    expect(screen.getByRole("button", { name: /口播视频成片/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(screen.getByRole("button", { name: /产品宣传片/ })).not.toHaveAttribute("aria-current");
  });

  it("「开始新对话」回到新建空态（hero），不再自动打开历史", async () => {
    render(
      <TestHarness employeeId="clip-master" sessions={clipHistory} createChatMock={vi.fn()} />,
    );
    const shell = screen.getByTestId("mock-shell");
    expect(shell).toHaveAttribute("data-session", "websocket:clip-1");

    fireEvent.click(screen.getByRole("button", { name: "开始新对话" }));

    await waitFor(() => expect(shell).toHaveAttribute("data-session", ""));
    // hero 态仍为只读员工模式，开始新对话按钮处于当前态
    expect(screen.getByRole("button", { name: "开始新对话" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    // 历史列表不再高亮任何会话
    expect(screen.getByRole("button", { name: /产品宣传片/ })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("内嵌新建会话绑定本页员工并停留在本页选中新会话", async () => {
    const createChatMock = vi.fn().mockResolvedValue("new-1");
    render(
      <TestHarness employeeId="clip-master" sessions={clipHistory} createChatMock={createChatMock} />,
    );
    const shell = screen.getByTestId("mock-shell");

    fireEvent.click(screen.getByRole("button", { name: "mock-create" }));

    await waitFor(() =>
      expect(createChatMock).toHaveBeenCalledWith(
        expect.objectContaining({ project_path: "/mock" }),
        "clip-master",
      ),
    );
    // 乐观插入后自动选中新会话（不跳回主聊天视图）
    await waitFor(() => expect(shell).toHaveAttribute("data-session", "websocket:new-1"));
  });

  it("切换员工后重置选择并自动打开新员工最近会话", async () => {
    const createChatMock = vi.fn();
    const { rerender } = render(
      <TestHarness
        employeeId="clip-master"
        sessions={[...clipHistory, ...videoHistory]}
        createChatMock={createChatMock}
      />,
    );
    expect(screen.getByTestId("mock-shell")).toHaveAttribute("data-session", "websocket:clip-1");

    rerender(
      <TestHarness
        employeeId="video-master"
        sessions={[...clipHistory, ...videoHistory]}
        createChatMock={createChatMock}
      />,
    );
    expect(screen.getByTestId("mock-shell")).toHaveAttribute("data-session", "websocket:video-1");
    // 新员工历史列表高亮 video-1
    expect(screen.getByRole("button", { name: /沐辰的会话/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("无历史时进入新建空态并展示空态提示", () => {
    render(<TestHarness employeeId="clip-master" sessions={[]} createChatMock={vi.fn()} />);
    expect(screen.getByTestId("mock-shell")).toHaveAttribute("data-session", "");
    expect(screen.getByText(/还没有与该员工的对话记录/)).toBeInTheDocument();
  });

  it("内嵌会话使用该会话的 workspace 覆盖值；hero 使用草稿作用域", async () => {
    const { rerender } = render(
      <TestHarness
        employeeId="clip-master"
        sessions={clipHistory}
        createChatMock={vi.fn()}
        workspaceOverrides={{
          "clip-1": { project_path: "/clip-override", access_mode: "full" },
        }}
      />,
    );
    // 选中 clip-1 → 使用 override
    expect(screen.getByTestId("mock-shell").getAttribute("data-scope")).toContain(
      "/clip-override",
    );

    rerender(
      <TestHarness
        employeeId="clip-master"
        sessions={clipHistory}
        createChatMock={vi.fn()}
        draftWorkspaceScope={{ project_path: "/draft", access_mode: "restricted" }}
      />,
    );
    // 开始新对话 → hero 使用草稿作用域
    fireEvent.click(screen.getByRole("button", { name: "开始新对话" }));
    await waitFor(() =>
      expect(screen.getByTestId("mock-shell").getAttribute("data-scope")).toContain("/draft"),
    );
  });

  it("内嵌 workspace 变更委托给 onWorkspaceScopeChangeForChat(选中会话)", () => {
    const onWorkspaceScopeChangeForChat = vi.fn();
    render(
      <TestHarness
        employeeId="clip-master"
        sessions={clipHistory}
        createChatMock={vi.fn()}
        onWorkspaceScopeChangeForChat={onWorkspaceScopeChangeForChat}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "mock-scope" }));
    expect(onWorkspaceScopeChangeForChat).toHaveBeenCalledWith(
      "clip-1",
      expect.objectContaining({ project_path: "/changed" }),
    );
  });

  it("运行中的会话禁用 workspace 切换", () => {
    render(
      <TestHarness
        employeeId="clip-master"
        sessions={clipHistory}
        createChatMock={vi.fn()}
        runningChatIds={new Set(["clip-1"])}
      />,
    );
    expect(screen.getByTestId("mock-shell")).toHaveAttribute("data-disabled", "true");
  });

  it("移动端历史菜单可打开并选择会话", async () => {
    render(
      <TestHarness employeeId="clip-master" sessions={clipHistory} createChatMock={vi.fn()} />,
    );
    const shell = screen.getByTestId("mock-shell");
    // 打开移动端历史菜单
    fireEvent.click(screen.getByRole("button", { name: "历史对话" }));
    const menu = within(screen.getByTestId("mobile-history-menu"));
    expect(menu.getByRole("button", { name: "开始新对话" })).toBeInTheDocument();
    // 选择另一条会话
    fireEvent.click(menu.getByRole("button", { name: /口播视频成片/ }));
    await waitFor(() => expect(shell).toHaveAttribute("data-session", "websocket:clip-2"));
  });

  it("员工不存在时展示空态", () => {
    render(<TestHarness employeeId="ghost" sessions={[]} createChatMock={vi.fn()} />);
    expect(screen.getByText("该员工不存在或已被删除。")).toBeInTheDocument();
  });
});
