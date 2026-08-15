import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "@/i18n";
import type { ChatSummary, Employee } from "@/lib/types";

const connectSpy = vi.fn();
const sessionUpdateHandlers = new Set<(chatId: string, scope?: string) => void>();
const CLIP_MASTER: Employee = {
  id: "clip-master",
  name: "阿伟",
  title: "剪辑",
  avatar: "🎬",
  system_prompt: "你是「阿伟」，团队里的剪辑高手，精通剪映自动化剪辑。",
  skills: [],
  enabled: true,
  created_at: "2026-08-13T00:00:00Z",
};

const CLIP_SESSION: ChatSummary = {
  key: "websocket:clip-1",
  channel: "websocket",
  chatId: "clip-1",
  createdAt: null,
  updatedAt: "2026-08-13T10:00:00Z",
  preview: "帮我剪一支宣传片",
  title: "产品宣传片",
  employee: "clip-master",
};

const MAIN_SESSION: ChatSummary = {
  key: "websocket:main-1",
  channel: "websocket",
  chatId: "main-1",
  createdAt: null,
  updatedAt: "2026-08-13T09:00:00Z",
  preview: "整理一下今天的待办",
  title: "待办整理",
};

vi.mock("@/hooks/useSessions", async (importOriginal) => {
  const React = await import("react");
  const actual = await importOriginal<typeof import("@/hooks/useSessions")>();
  return {
    ...actual,
    useSessions: () => {
      const [sessions] = React.useState<ChatSummary[]>([CLIP_SESSION, MAIN_SESSION]);
      return {
        sessions,
        loading: false,
        error: null,
        refresh: vi.fn(),
        createChat: async () => "chat-1",
        forkChat: async () => "fork-chat",
        getSessionAutomations: async () => [],
        deleteChat: async () => ({ deleted: true }),
      };
    },
  };
});

vi.mock("@/hooks/useEmployees", () => ({
  useEmployees: () => ({
    employees: [CLIP_MASTER],
    reload: vi.fn(),
  }),
}));

vi.mock("@/hooks/useTheme", async () => {
  const React = await import("react");
  return {
    ThemeProvider: ({ children }: { children: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    useTheme: () => ({ theme: "light" as const, toggle: vi.fn() }),
    useThemeValue: () => "light" as const,
  };
});

vi.mock("@/lib/bootstrap", () => ({
  fetchBootstrap: vi.fn().mockResolvedValue({
    token: "tok",
    ws_path: "/",
    expires_in: 300,
  }),
  deriveWsUrl: vi.fn(() => "ws://test"),
  loadSavedSecret: vi.fn(() => ""),
  saveSecret: vi.fn(),
  clearSavedSecret: vi.fn(),
}));

vi.mock("@/lib/biscuitbot-client", () => {
  class MockClient {
    status = "idle" as const;
    defaultChatId: string | null = null;
    connect = connectSpy;
    onStatus = () => () => {};
    onRuntimeModelUpdate = () => () => {};
    onError = () => () => {};
    onChat = () => () => {};
    onSessionUpdate = (handler: (chatId: string, scope?: string) => void) => {
      sessionUpdateHandlers.add(handler);
      return () => sessionUpdateHandlers.delete(handler);
    };
    onRunStatus = () => () => {};
    getRunStartedAt = () => null;
    getGoalState = () => undefined;
    sendMessage = vi.fn();
    newChat = vi.fn();
    attach = vi.fn();
    close = vi.fn();
    updateUrl = vi.fn();
  }
  return { BiscuitbotClient: MockClient };
});

import App from "@/App";

describe("侧边栏历史会话点击跳转到对应员工专属页", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("zh-CN");
    connectSpy.mockClear();
    sessionUpdateHandlers.clear();
    window.history.replaceState(null, "", "/");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 404 }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("点击绑定员工的会话 → 跳到该员工的专属对话页", async () => {
    render(<App />);
    await waitFor(() => expect(connectSpy).toHaveBeenCalled());

    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });

    // 侧边栏里点击绑定 clip-master 的会话「产品宣传片」
    fireEvent.click(within(sidebar).getByText("产品宣传片"));

    // 应跳转到阿伟的专属页：名字标题 + 掌握的技能
    expect(await screen.findByRole("heading", { name: "阿伟" })).toBeInTheDocument();
    expect(screen.getByText("掌握的技能")).toBeInTheDocument();
    // 专属页历史列表也展示该会话：侧边栏 1 处 + 专属页历史 1 处 + 内嵌对话头部标题 1 处
    expect(await screen.findAllByText("产品宣传片")).toHaveLength(3);
    // 自动打开最近会话并在专属页历史列表中带 ▸ 游标高亮（侧边栏项不带 aria-current）
    const clipButtons = screen.getAllByRole("button", { name: /产品宣传片/ });
    expect(clipButtons.length).toBeGreaterThanOrEqual(2);
    expect(clipButtons.some((el) => el.getAttribute("aria-current") === "true")).toBe(true);
  });

  it("点击未绑定员工的会话 → 仍打开普通聊天", async () => {
    render(<App />);
    await waitFor(() => expect(connectSpy).toHaveBeenCalled());

    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    fireEvent.click(within(sidebar).getByText("待办整理"));

    // 未绑定员工 → 打开普通聊天：侧边栏 + 聊天头部各出现一次
    expect(await screen.findAllByText("待办整理")).toHaveLength(2);
    expect(screen.queryByText("掌握的技能")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "阿伟" })).not.toBeInTheDocument();
  });

  it("员工视图点击员工卡片 → 跳转到该员工专属页", async () => {
    render(<App />);
    await waitFor(() => expect(connectSpy).toHaveBeenCalled());

    // 侧边栏进入员工视图
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    fireEvent.click(within(sidebar).getByText("数字员工"));

    // 员工视图卡片出现，点击卡片本体
    expect(await screen.findByText("阿伟")).toBeInTheDocument();
    fireEvent.click(screen.getByText("阿伟"));

    // 应跳转到阿伟的专属页（真实 navigate 链路：onPick → onOpenEmployee → employee-chat）
    expect(await screen.findByRole("heading", { name: "阿伟" })).toBeInTheDocument();
    expect(screen.getByText("掌握的技能")).toBeInTheDocument();
  });
});
