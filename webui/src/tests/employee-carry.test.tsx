import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "@/i18n";
import type { ChatSummary, Employee } from "@/lib/types";

const connectSpy = vi.fn();
const createChatSpy = vi.fn().mockResolvedValue("chat-1");
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

vi.mock("@/hooks/useSessions", async (importOriginal) => {
  const React = await import("react");
  const actual = await importOriginal<typeof import("@/hooks/useSessions")>();
  return {
    ...actual,
    useSessions: () => {
      const [sessions] = React.useState<ChatSummary[]>([]);
      return {
        sessions,
        loading: false,
        error: null,
        refresh: vi.fn(),
        createChat: createChatSpy,
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

describe("和 TA 对话自动携带数字员工", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("zh-CN");
    connectSpy.mockClear();
    createChatSpy.mockClear().mockResolvedValue("chat-1");
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

  it("从「和 TA 对话」进入员工专属页，点「开始新对话」后 hero 绑定员工，首发消息自动绑定", async () => {
    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());

    // 侧边栏 → 数字人员工 tab
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "数字员工" }));
    await screen.findByRole("heading", { name: "数字人员工" });

    // 点「和 TA 对话」→ 跳转到该员工的专属对话页（技能 + 历史会话 + 开始新对话）
    fireEvent.click(screen.getByText("和 TA 对话"));
    expect(await screen.findByText("掌握的技能")).toBeInTheDocument();
    expect(screen.getByText("历史对话")).toBeInTheDocument();

    // 点「开始新对话」→ 新会话 hero 显示「正在与 🎬 阿伟 对话」徽标
    fireEvent.click(screen.getAllByText("开始新对话")[0]);

    // hashchange 的 applyRoute 不得清掉预选员工 → hero 显示「正在与 🎬 阿伟 对话」徽标
    await waitFor(() =>
      expect(screen.getByText("正在与 🎬 阿伟 对话")).toBeInTheDocument(),
    );

    // 首发消息 → createChat(scope, employee.id)
    // 主聊天 ThreadShell（不可见）与内嵌 ThreadShell 同时渲染输入框，限定到员工专属页内
    const pageMain = screen.getAllByRole("main").slice(-1)[0];
    fireEvent.change(within(pageMain).getByRole("textbox", { name: "消息输入框" }), {
      target: { value: "你好，剪影" },
    });
    fireEvent.click(within(pageMain).getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(createChatSpy).toHaveBeenCalledTimes(1));
    expect(createChatSpy.mock.calls[0][1]).toBe("clip-master");
  });
});
