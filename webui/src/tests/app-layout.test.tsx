import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "@/i18n";
import type { ChatSummary, SessionAutomationJob } from "@/lib/types";

const connectSpy = vi.fn();
const refreshSpy = vi.fn();
const createChatSpy = vi.fn().mockResolvedValue("chat-1");
const deleteChatSpy = vi.fn();
const getSessionAutomationsSpy = vi.fn<(key: string) => Promise<SessionAutomationJob[]>>();
const toggleThemeSpy = vi.fn();
const updateUrlSpy = vi.fn();
const attachSpy = vi.fn();
const runStatusHandlers = new Set<(chatId: string, startedAt: number | null) => void>();
const sessionUpdateHandlers = new Set<(chatId: string, scope?: string) => void>();
let mockSessions: ChatSummary[] = [];
const HERO_GREETING_PATTERN =
  /我们要一起做点什么？|今天从哪里开始？|今天一起构建什么？|我们要一起解决什么？/;

function setNavigatorPlatform(platform: string): void {
  Object.defineProperty(window.navigator, "platform", {
    configurable: true,
    value: platform,
  });
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response;
}

function mockFetchRoutes(routes: Record<string, unknown>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const body = routes[String(input)];
      return body === undefined
        ? ({ ok: false, status: 404, json: async () => ({}) } as Response)
        : jsonResponse(body);
    }),
  );
}

function baseSettingsPayload() {
  return {
    agent: {
      model: "openai/gpt-4o",
      provider: "auto",
      resolved_provider: "openai",
      has_api_key: true,
      model_preset: "default",
      max_tokens: 8192,
      context_window_tokens: 65536,
      temperature: 0.1,
      reasoning_effort: null,
      timezone: "UTC",
      bot_name: "biscuitbot",
      bot_icon: "nb",
      tool_hint_max_length: 40,
    },
    model_presets: [{
      name: "default",
      label: "Default",
      active: true,
      is_default: true,
      model: "openai/gpt-4o",
      provider: "auto",
      max_tokens: 8192,
      context_window_tokens: 65536,
      temperature: 0.1,
      reasoning_effort: null,
    }],
    providers: [],
    web_search: {
      provider: "duckduckgo",
      api_key_hint: null,
      base_url: null,
      max_results: 5,
      timeout: 30,
      providers: [{ name: "duckduckgo", label: "DuckDuckGo", credential: "none" }],
    },
    web: {
      enable: true,
      proxy: null,
      user_agent: null,
      search: { max_results: 5, timeout: 30 },
      fetch: { use_jina_reader: true },
    },
    image_generation: {
      enabled: false,
      provider: "openrouter",
      provider_configured: false,
      model: "openai/gpt-5.4-image-2",
      default_aspect_ratio: "1:1",
      default_image_size: "1K",
      max_images_per_turn: 4,
      save_dir: "generated",
      providers: [],
    },
    video_generation: {
      enabled: false,
      api_key_configured: false,
      model: "doubao-seedance",
      default_ratio: "16:9",
      default_duration: 6,
      default_resolution: null,
      generate_audio: true,
      watermark: false,
      save_dir: "generated/videos",
    },
    screenshot: {
      enabled: false,
      max_width: 1920,
      max_height: 1080,
      quality: 70,
      vision_model: null,
      vision_model_override: null,
      vision_model_configured: false,
      resolved_model: null,
      available_providers: [],
    },
    system_io: {
      enabled: false,
      allow_actions: [],
      available_actions: [],
    },
    runtime: {
      config_path: "/tmp/config.json",
      workspace_path: "/tmp/workspace",
      gateway_host: "127.0.0.1",
      gateway_port: 18790,
      heartbeat: {
        enabled: true,
        interval_s: 1800,
        keep_recent_messages: 8,
      },
      dream: {
        schedule: "every 2h",
        max_batch_size: 20,
        max_iterations: 15,
        annotate_line_ages: true,
      },
      unified_session: false,
    },
    advanced: {
      restrict_to_workspace: false,
      webui_allow_local_service_access: true,
      webui_default_access_mode: "default",
      private_service_protection_enabled: true,
      ssrf_whitelist_count: 0,
      mcp_server_count: 0,
      exec_enabled: true,
      exec_sandbox: null,
      exec_path_prepend_set: false,
      exec_path_append_set: false,
    },
    requires_restart: false,
  };
}

vi.mock("@/hooks/useSessions", async (importOriginal) => {
  const React = await import("react");
  const actual = await importOriginal<typeof import("@/hooks/useSessions")>();
  return {
    ...actual,
    useSessions: () => {
      const [sessions, setSessions] = React.useState(mockSessions);
      return {
        sessions,
        loading: false,
        error: null,
        refresh: refreshSpy,
        createChat: createChatSpy,
        forkChat: async () => "fork-chat",
        getSessionAutomations: getSessionAutomationsSpy,
        deleteChat: async (key: string, options?: { deleteAutomations?: boolean }) => {
          if (options === undefined) await deleteChatSpy(key);
          else await deleteChatSpy(key, options);
          setSessions((prev: ChatSummary[]) => prev.filter((s) => s.key !== key));
          return { deleted: true };
        },
      };
    },
  };
});

vi.mock("@/hooks/useTheme", async () => {
  const React = await import("react");
  return {
    ThemeProvider: ({ children }: { children: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    useTheme: () => ({
      theme: "light" as const,
      toggle: toggleThemeSpy,
    }),
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
    onRunStatus = (handler: (chatId: string, startedAt: number | null) => void) => {
      runStatusHandlers.add(handler);
      return () => runStatusHandlers.delete(handler);
    };
    getRunStartedAt = () => null;
    getGoalState = () => undefined;
    sendMessage = vi.fn();
    newChat = vi.fn();
    attach = attachSpy;
    close = vi.fn();
    updateUrl = updateUrlSpy;
  }

  return { BiscuitbotClient: MockClient };
});

import { deriveWsUrl, fetchBootstrap } from "@/lib/bootstrap";
import App from "@/App";

describe("App layout", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("zh-CN");
    mockSessions = [];
    connectSpy.mockClear();
    updateUrlSpy.mockClear();
    refreshSpy.mockReset();
    createChatSpy.mockClear();
    deleteChatSpy.mockReset();
    getSessionAutomationsSpy.mockReset().mockResolvedValue([]);
    toggleThemeSpy.mockReset();
    attachSpy.mockReset();
    runStatusHandlers.clear();
    sessionUpdateHandlers.clear();
    window.history.replaceState(null, "", "/");
    setNavigatorPlatform("Linux x86_64");
    localStorage.removeItem("biscuitbot-webui.sidebar");
    localStorage.removeItem("biscuitbot-webui.sidebar.completed-runs.v1");
    localStorage.removeItem("biscuitbot-webui.sidebar.session-updates.v1");
    vi.mocked(fetchBootstrap).mockReset().mockResolvedValue({
      token: "tok",
      ws_path: "/",
      expires_in: 300,
    });
    vi.mocked(deriveWsUrl).mockReset().mockReturnValue("ws://test");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
      }),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps sidebar layout out of the main thread width contract", async () => {
    const { container } = render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());

    const main = container.querySelector("main");
    expect(main).toBeInTheDocument();
    expect(main).not.toHaveAttribute("style");

    const asideClassNames = Array.from(container.querySelectorAll("aside")).map(
      (el) => el.className,
    );
    expect(asideClassNames.some((cls) => cls.includes("lg:block"))).toBe(true);
  });

  it("places Automations after Capabilities in the main sidebar", async () => {
    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    const capabilitiesButton = within(sidebar).getByRole("button", { name: "能力" });
    const automationsButton = within(sidebar).getByRole("button", { name: "自动任务" });

    expect(
      capabilitiesButton.compareDocumentPosition(automationsButton) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("opens Capabilities from the main sidebar", async () => {
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/skills": { skills: [] },
      "/api/webui/capabilities": {
        capabilities: [
          {
            id: "cron",
            name: "cron",
            display_name: "cron",
            description: "Schedule reminders.",
            source: "builtin",
            runtime: "prompt",
            installed: true,
            available: true,
            unavailable_reason: "",
            install_supported: false,
            skill_installed: true,
          },
          {
            id: "github",
            name: "github",
            display_name: "github",
            description: "Work with GitHub.",
            source: "builtin",
            runtime: "prompt",
            installed: true,
            available: false,
            unavailable_reason: "CLI: gh",
            install_supported: false,
            skill_installed: true,
          },
        ],
        installed_count: 2,
      },
      "/api/webui/capabilities/github": {
        id: "github",
        name: "github",
        display_name: "github",
        description: "Work with GitHub.",
        source: "builtin",
        runtime: "prompt",
        installed: true,
        available: false,
        unavailable_reason: "CLI: gh",
        install_supported: false,
        skill_installed: true,
        requires: "CLI: gh",
        requirements: {
          bins: ["gh"],
          env: [],
          pkgs: [],
          missing_bins: ["gh"],
          missing_env: [],
        },
        raw_markdown: "---\nname: github\n---\nUse GitHub CLI.",
      },
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    const capabilitiesButton = within(sidebar).getByRole("button", { name: "能力" });

    fireEvent.click(capabilitiesButton);

    expect(await screen.findByRole("heading", { name: "能力" })).toBeInTheDocument();
    expect(await screen.findByText("cron")).toBeInTheDocument();
    expect(screen.getByText("github")).toBeInTheDocument();
    expect(screen.getByText("缺少：CLI: gh")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "侧边栏导航" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "设置分区" })).not.toBeInTheDocument();
    expect(within(sidebar).getByRole("button", { name: "能力" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(document.title).toBe("能力 · biscuitbot");

    fireEvent.click(screen.getByRole("button", { name: "返回聊天" }));
    expect(await screen.findByText(HERO_GREETING_PATTERN)).toBeInTheDocument();

    fireEvent.click(within(sidebar).getByRole("button", { name: "能力" }));
    expect(await screen.findByRole("heading", { name: "能力" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "查看 github 详情" }));

    expect(await screen.findByText("原始 SKILL.md")).toBeInTheDocument();
    fireEvent.click(screen.getByText("原始 SKILL.md"));
    expect(await screen.findByText(/Use GitHub CLI/)).toBeInTheDocument();
  });

  it("opens Automations from the main sidebar", async () => {
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/automations": {
        jobs: [
          {
            id: "job-1",
            name: "Daily repo check",
            enabled: true,
            protected: false,
            delete_after_run: false,
            schedule: { kind: "every", every_ms: 86_400_000 },
            payload: {
              message: "Check the repo status",
              kind: "agent_turn",
            },
            state: {
              next_run_at_ms: Date.UTC(2026, 3, 17, 10, 0, 0),
              last_status: "ok",
              pending: false,
              run_history: [],
            },
            origin: {
              session_key: "websocket:chat-a",
              channel: "websocket",
              chat_id: "chat-a",
              title: "Release prep",
              preview: "Check release blockers",
            },
          },
          {
            id: "external-quiz",
            name: "WeChat quiz",
            enabled: true,
            protected: false,
            delete_after_run: false,
            schedule: { kind: "cron", expr: "30 9-23 * * *", tz: "Asia/Shanghai" },
            payload: {
              message: "Send a quiz",
              kind: "agent_turn",
            },
            state: {
              next_run_at_ms: Date.UTC(2026, 3, 17, 11, 30, 0),
              last_status: "ok",
              pending: false,
              run_history: [],
            },
            origin: {
              channel: "weixin",
              title: "",
              preview: "",
            },
          },
          {
            id: "heartbeat",
            name: "heartbeat",
            enabled: true,
            protected: true,
            schedule: { kind: "every", every_ms: 60_000 },
            payload: { message: "", kind: "system_event" },
            state: { next_run_at_ms: null, pending: false, run_history: [] },
            origin: null,
          },
        ],
      },
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    const automationsButton = within(sidebar).getByRole("button", {
      name: "自动任务",
    });

    fireEvent.click(automationsButton);

    const heading = await screen.findByRole("heading", { name: "自动任务" });
    expect(heading).toBeInTheDocument();
    const automationsMain = heading.closest("main");
    expect(automationsMain).not.toBeNull();
    expect(within(automationsMain as HTMLElement).queryByText("设置")).not.toBeInTheDocument();
    expect(screen.getAllByText("Daily repo check").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Check the repo status").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Release prep").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("WeChat quiz")).toBeInTheDocument();
    expect(screen.getByText("微信")).toBeInTheDocument();
    expect(screen.queryByText("weixin:wx-chat")).not.toBeInTheDocument();
    expect(screen.queryByText("memory with dream state")).not.toBeInTheDocument();
    expect(screen.getByText("heartbeat")).toBeInTheDocument();
    expect(within(sidebar).getByRole("button", { name: "自动任务" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(document.title).toBe("自动任务 · biscuitbot");

    const searchInput = within(automationsMain as HTMLElement).getByPlaceholderText(
      "搜索任务、消息、关联会话或计划",
    );
    fireEvent.change(searchInput, { target: { value: "WeChat" } });
    await waitFor(() => expect(screen.queryByText("Daily repo check")).not.toBeInTheDocument());
    expect(screen.getAllByText("WeChat quiz").length).toBeGreaterThanOrEqual(1);

    fireEvent.change(searchInput, { target: { value: "09-23" } });
    await waitFor(() => expect(screen.queryByText("Daily repo check")).not.toBeInTheDocument());
    expect(screen.getAllByText("WeChat quiz").length).toBeGreaterThanOrEqual(1);
  });

  it("edits a past one-time automation without resubmitting its old schedule", async () => {
    const pastOneShot = {
      id: "past-one-shot",
      name: "Past one-shot",
      enabled: true,
      protected: false,
      delete_after_run: true,
      schedule: { kind: "at", at_ms: 1 },
      payload: {
        message: "Old one-shot message",
        kind: "agent_turn",
      },
      state: {
        next_run_at_ms: null,
        last_status: "ok",
        pending: false,
        run_history: [],
      },
      origin: {
        session_key: "websocket:chat-a",
        channel: "websocket",
        chat_id: "chat-a",
        title: "Release prep",
        preview: "Check release blockers",
      },
    };
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/automations": { jobs: [pastOneShot] },
      "/api/webui/automations/update?id=past-one-shot": {
        jobs: [
          {
            ...pastOneShot,
            payload: { ...pastOneShot.payload, message: "Updated one-shot message" },
          },
        ],
      },
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "自动任务" }));

    expect((await screen.findAllByText("Past one-shot")).length).toBeGreaterThanOrEqual(1);
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    expect(screen.queryByText("Run time must be in the future.")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Update the prompt and schedule. The linked chat stays unchanged."),
    ).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("Old one-shot message")).toHaveClass(
      "min-h-[160px]",
      "resize-none",
    );

    fireEvent.change(screen.getByDisplayValue("Old one-shot message"), {
      target: { value: "Updated one-shot message" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        "/api/webui/automations/update?id=past-one-shot",
        expect.any(Object),
      );
    });
    const updateCall = vi.mocked(fetch).mock.calls.find(
      ([url]) => String(url) === "/api/webui/automations/update?id=past-one-shot",
    );
    expect(updateCall).toBeTruthy();
    const headers = updateCall?.[1]?.headers as Record<string, string>;
    expect(JSON.parse(decodeURIComponent(headers["X-Biscuitbot-Automation-Values"]))).toEqual({
      name: "Past one-shot",
      message: "Updated one-shot message",
    });
  });

  it("keeps long automation details expandable without nested scrolling", async () => {
    const longMessage = [
      "Review the release plan and prepare a concise status update for the channel.",
      "Include blockers, owners, follow-up dates, and any risky assumptions that changed since yesterday.",
      "Keep the output actionable and avoid repeating context that the team already confirmed in the thread.",
      "If a dependency looks stale, call it out explicitly and ask for a fresh owner update.",
      "This message is intentionally long enough to require progressive disclosure in the automation details panel.",
      "The full content should remain available without forcing the user into a small nested scroll area.",
    ].join("\n");
    const history = [
      { run_at_ms: Date.UTC(2026, 3, 12, 10, 0, 0), status: "error", duration_ms: 900, error: "oldest failure" },
      { run_at_ms: Date.UTC(2026, 3, 13, 10, 0, 0), status: "error", duration_ms: 800, error: "second oldest failure" },
      { run_at_ms: Date.UTC(2026, 3, 14, 10, 0, 0), status: "ok", duration_ms: 700 },
      { run_at_ms: Date.UTC(2026, 3, 15, 10, 0, 0), status: "ok", duration_ms: 600 },
      { run_at_ms: Date.UTC(2026, 3, 16, 10, 0, 0), status: "ok", duration_ms: 500 },
      { run_at_ms: Date.UTC(2026, 3, 17, 10, 0, 0), status: "ok", duration_ms: 400 },
    ];
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/automations": {
        jobs: [
          {
            id: "long-details",
            name: "Long detail automation",
            enabled: true,
            protected: false,
            delete_after_run: false,
            schedule: { kind: "every", every_ms: 3_600_000 },
            payload: {
              message: longMessage,
              kind: "agent_turn",
            },
            state: {
              next_run_at_ms: Date.UTC(2026, 3, 18, 10, 0, 0),
              last_status: "ok",
              pending: false,
              run_history: history,
            },
            origin: {
              session_key: "websocket:chat-a",
              channel: "websocket",
              chat_id: "chat-a",
              title: "Release prep",
              preview: "Check release blockers",
            },
          },
        ],
      },
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "自动任务" }));

    const detailHeading = await screen.findByRole("heading", { name: "Long detail automation" });
    const detailPanel = detailHeading.closest("article") as HTMLElement;
    expect(detailPanel).not.toBeNull();
    const message = Array.from(detailPanel.querySelectorAll("section div")).find(
      (node) => node.textContent === longMessage,
    ) as HTMLElement | undefined;
    expect(message).toBeTruthy();
    expect(message!).toHaveClass("line-clamp-6");

    fireEvent.click(within(detailPanel).getByRole("button", { name: "查看完整消息" }));
    expect(within(detailPanel).getByRole("button", { name: "收起消息" })).toBeInTheDocument();
    expect(message!).not.toHaveClass("line-clamp-6");

    expect(within(detailPanel).queryByText("Recent health")).not.toBeInTheDocument();
    expect(within(detailPanel).queryByRole("button", { name: /Run history/ })).not.toBeInTheDocument();
    expect(within(detailPanel).queryByText(/oldest failure/)).not.toBeInTheDocument();
    expect(within(detailPanel).queryByText("No error recorded")).not.toBeInTheDocument();
  });

  it("localizes the Automations surface", async () => {
    await i18n.changeLanguage("zh-CN");
    mockFetchRoutes({
      "/api/settings": baseSettingsPayload(),
      "/api/webui/automations": {
        jobs: [
          {
            id: "job-zh",
            name: "每日检查",
            enabled: true,
            protected: false,
            delete_after_run: false,
            schedule: { kind: "every", every_ms: 86_400_000 },
            payload: {
              message: "检查仓库状态",
              kind: "agent_turn",
            },
            state: {
              next_run_at_ms: Date.UTC(2026, 3, 17, 10, 0, 0),
              last_run_at_ms: Date.UTC(2026, 3, 16, 10, 0, 0),
              last_status: "ok",
              pending: false,
              run_history: [
                {
                  run_at_ms: Date.UTC(2026, 3, 16, 10, 0, 0),
                  status: "ok",
                  duration_ms: 500,
                },
              ],
            },
            origin: {
              session_key: "websocket:chat-a",
              channel: "websocket",
              chat_id: "chat-a",
              title: "发布准备",
              preview: "检查发布阻塞项",
            },
          },
        ],
      },
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "自动任务" }));

    const heading = await screen.findByRole("heading", { name: "自动任务" });
    expect(heading).toBeInTheDocument();
    const automationsMain = heading.closest("main");
    expect(automationsMain).not.toBeNull();
    expect(within(automationsMain as HTMLElement).queryByText("设置")).not.toBeInTheDocument();
    expect(screen.getByText("任务队列")).toBeInTheDocument();
    expect(screen.getAllByText("每日检查").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("检查仓库状态").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("每 1天")).toBeInTheDocument();
    expect(screen.queryByText("最近健康状态")).not.toBeInTheDocument();
    expect(screen.queryByText("近期无问题")).not.toBeInTheDocument();
    expect(screen.queryByText("Workspace automations")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "刷新" })).not.toBeInTheDocument();
    expect(document.title).toBe("自动任务 · biscuitbot");
  });

  it("fully collapses the native host sidebar and previews it on hover", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Desktop chat",
      },
    ];
    vi.mocked(fetchBootstrap).mockResolvedValue({
      token: "tok",
      ws_path: "/",
      expires_in: 300,
      runtime_surface: "native",
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const flowSidebar = screen.getByTestId("host-sidebar-flow");
    const toggle = screen.getByTestId("host-sidebar-toggle");
    expect(flowSidebar).toHaveStyle({ width: "272px" });
    expect(
      screen.getByRole("navigation", { name: "侧边栏导航" }),
    ).toBeInTheDocument();

    fireEvent.click(toggle);
    await waitFor(() => expect(flowSidebar).toHaveStyle({ width: "0px" }));
    expect(
      screen.queryByRole("navigation", { name: "侧边栏导航" }),
    ).not.toBeInTheDocument();

    fireEvent.mouseEnter(toggle);
    const previewSidebar = await screen.findByTestId("host-sidebar-preview");
    expect(flowSidebar).toHaveStyle({ width: "0px" });
    expect(previewSidebar).toHaveStyle({ width: "272px" });
    expect(
      within(previewSidebar).getByRole("navigation", {
        name: "侧边栏导航",
      }),
    ).toBeInTheDocument();

    fireEvent.click(toggle);
    await waitFor(() =>
      expect(screen.queryByTestId("host-sidebar-preview")).not.toBeInTheDocument(),
    );
    expect(flowSidebar).toHaveStyle({ width: "272px" });
    expect(
      screen.getByRole("navigation", { name: "侧边栏导航" }),
    ).toBeInTheDocument();
  });

  it("switches to the next session when deleting the active chat", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "First chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Second chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    await waitFor(() =>
      expect(
        within(sidebar).getByRole("button", { name: /^First chat$/ }),
      ).toBeInTheDocument(),
    );

    fireEvent.pointerDown(screen.getByLabelText("“First chat” 的会话操作"), {
      button: 0,
    });
    fireEvent.click(await screen.findByRole("menuitem", { name: "删除" }));

    await waitFor(() =>
      expect(screen.getByText("删除这个对话？")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() =>
      expect(deleteChatSpy).toHaveBeenCalledWith("websocket:chat-a"),
    );
    await waitFor(() =>
      expect(
        within(sidebar).getByRole("button", { name: /^Second chat$/ }),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("删除这个对话？")).not.toBeInTheDocument();
    expect(document.body.style.pointerEvents).not.toBe("none");
  }, 15_000);

  it("shows localized bound automations in the first delete confirmation", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "First chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Second chat",
      },
    ];
    getSessionAutomationsSpy.mockResolvedValue([
      {
        id: "job-1",
        name: "Daily repo check",
        enabled: true,
        schedule: { kind: "every", every_ms: 86_400_000 },
        payload: { message: "Check the repo" },
        state: { next_run_at_ms: Date.UTC(2026, 3, 17, 10, 0, 0) },
      },
    ]);
    await i18n.changeLanguage("zh-CN");

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    await waitFor(() =>
      expect(
        within(sidebar).getByRole("button", { name: /^First chat$/ }),
      ).toBeInTheDocument(),
    );

    fireEvent.pointerDown(screen.getByLabelText(/First chat.*会话操作/), {
      button: 0,
    });
    fireEvent.click(await screen.findByRole("menuitem", { name: "删除" }));

    await waitFor(() =>
      expect(screen.getByText("Daily repo check")).toBeInTheDocument(),
    );
    expect(getSessionAutomationsSpy).toHaveBeenCalledWith("websocket:chat-a");
    expect(
      screen.getByText("这个对话有关联的自动任务。删除对话也会删除这些自动任务。"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("This chat has scheduled automations. Deleting it will also delete them."),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() =>
      expect(deleteChatSpy).toHaveBeenCalledWith("websocket:chat-a", {
        deleteAutomations: true,
      }),
    );
    expect(deleteChatSpy).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Daily repo check")).not.toBeInTheDocument();
  }, 15_000);

  it("keeps the mobile session action menu inside the sidebar sheet", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        matches: !query.includes("1024px"),
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "切换侧边栏" }));

    const sheet = await screen.findByRole("dialog");
    const mobileSidebar = within(sheet).getByRole("navigation", {
      name: "侧边栏导航",
    });
    await waitFor(() =>
      expect(
        within(mobileSidebar).getByRole("button", { name: /^Existing chat$/ }),
      ).toBeInTheDocument(),
    );

    fireEvent.pointerDown(
      within(mobileSidebar).getByLabelText("“Existing chat” 的会话操作"),
      { button: 0 },
    );

    const deleteItem = await within(sheet).findByRole("menuitem", {
      name: "删除",
    });
    expect(deleteItem).toBeInTheDocument();

    fireEvent.click(deleteItem);
    await waitFor(() =>
      expect(screen.getByText("删除这个对话？")).toBeInTheDocument(),
    );
  }, 15_000);

  it("applies persisted sidebar workspace state from the gateway", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "First chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Second chat",
      },
    ];
    const initialState = {
      schema_version: 1,
      pinned_keys: ["websocket:chat-b"],
      archived_keys: ["websocket:chat-a"],
      title_overrides: { "websocket:chat-b": "Roadmap" },
      tags_by_key: {},
      collapsed_groups: {},
      view: {
        density: "comfortable",
        show_previews: false,
        show_timestamps: false,
        show_archived: false,
        sort: "updated_desc",
      },
      updated_at: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (url: string | URL | Request) => {
        const href = String(url);
        if (href === "/api/webui/sidebar-state") {
          return { ok: true, json: async () => initialState };
        }
        if (href.startsWith("/api/webui/sidebar-state/update?")) {
          const encoded = new URLSearchParams(href.split("?", 2)[1]).get("state");
          return {
            ok: true,
            json: async () => JSON.parse(encoded ?? "{}"),
          };
        }
        return { ok: false, status: 404 };
      }),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    await waitFor(() =>
      expect(within(sidebar).getByText("置顶")).toBeInTheDocument(),
    );
    expect(within(sidebar).getByRole("button", { name: /^Roadmap$/ })).toBeInTheDocument();
    expect(within(sidebar).queryByRole("button", { name: /^First chat$/ })).not.toBeInTheDocument();

    fireEvent.click(within(sidebar).getByRole("button", { name: "显示归档" }));
    await waitFor(() =>
      expect(within(sidebar).getByText("已归档")).toBeInTheDocument(),
    );
    expect(within(sidebar).getByRole("button", { name: /^First chat$/ })).toBeInTheDocument();
    const updateUrl = vi.mocked(fetch).mock.calls
      .map(([url]) => String(url))
      .find((url) => url.startsWith("/api/webui/sidebar-state/update?"));
    expect(updateUrl).toBeTruthy();
    const encoded = new URLSearchParams(updateUrl?.split("?", 2)[1]).get("state");
    expect(JSON.parse(encoded ?? "{}").view.show_archived).toBe(true);

    expect(within(sidebar).queryByRole("button", { name: "View" })).not.toBeInTheDocument();
  });

  it("sorts chats by displayed title when A-Z is persisted", async () => {
    mockSessions = [
      {
        key: "websocket:zulu",
        channel: "websocket",
        chatId: "zulu",
        createdAt: "2026-04-16T12:00:00Z",
        updatedAt: "2026-04-16T12:00:00Z",
        title: "Zulu work",
        preview: "later",
      },
      {
        key: "websocket:new",
        channel: "websocket",
        chatId: "new",
        createdAt: "2026-04-15T12:00:00Z",
        updatedAt: "2026-04-15T12:00:00Z",
        preview: "hi biscuitbot",
      },
      {
        key: "websocket:alpha",
        channel: "websocket",
        chatId: "alpha",
        createdAt: "2026-04-14T12:00:00Z",
        updatedAt: "2026-04-14T12:00:00Z",
        title: "Alpha plan",
        preview: "earlier",
      },
    ];
    const initialState = {
      schema_version: 1,
      pinned_keys: [],
      archived_keys: [],
      title_overrides: {},
      tags_by_key: {},
      collapsed_groups: {},
      view: {
        density: "comfortable",
        show_previews: false,
        show_timestamps: false,
        show_archived: false,
        sort: "title_asc",
      },
      updated_at: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (url: string | URL | Request) => {
        const href = String(url);
        if (href === "/api/webui/sidebar-state") {
          return { ok: true, json: async () => initialState };
        }
        return { ok: false, status: 404 };
      }),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    await waitFor(() =>
      expect(within(sidebar).getByText("对话")).toBeInTheDocument(),
    );
    const group = within(sidebar).getByText("对话").closest("section");
    expect(group).toBeTruthy();
    const labels = within(group as HTMLElement)
      .getAllByRole("button")
      .map((button) => button.textContent?.trim())
      .filter(Boolean);

    expect(labels).toEqual(["Alpha plan", "新建对话", "Zulu work"]);
  });

  it("shows running and completed session indicators in the sidebar", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Working chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Quiet chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    await waitFor(() =>
      expect(
        within(sidebar).getByRole("button", { name: /^Working chat$/ }),
      ).toBeInTheDocument(),
    );

    act(() => {
      for (const handler of runStatusHandlers) handler("chat-a", 12_345);
    });
    expect(within(sidebar).getByTitle("Agent 正在运行")).toBeInTheDocument();

    act(() => {
      for (const handler of runStatusHandlers) handler("chat-a", null);
    });
    expect(within(sidebar).queryByTitle("Agent 正在运行")).not.toBeInTheDocument();
    expect(within(sidebar).getByTitle("有新内容")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(within(sidebar).getByRole("button", { name: /^Working chat$/ }));
    });
    expect(within(sidebar).queryByTitle("有新内容")).not.toBeInTheDocument();
  });

  it("does not show an updated dot later when the active session finishes", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Active work",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Other chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    await waitFor(() =>
      expect(
        within(sidebar).getByRole("button", { name: /^Active work$/ }),
      ).toBeInTheDocument(),
    );

    await act(async () => {
      fireEvent.click(within(sidebar).getByRole("button", { name: /^Active work$/ }));
    });
    await waitFor(() => expect(document.title).toContain("Active work"));

    act(() => {
      for (const handler of runStatusHandlers) handler("chat-a", 12_345);
    });
    expect(within(sidebar).getByTitle("Agent 正在运行")).toBeInTheDocument();

    act(() => {
      for (const handler of runStatusHandlers) handler("chat-a", null);
    });
    expect(within(sidebar).queryByTitle("Agent 正在运行")).not.toBeInTheDocument();
    expect(within(sidebar).queryByTitle("有新内容")).not.toBeInTheDocument();

    await act(async () => {
      fireEvent.click(within(sidebar).getByRole("button", { name: /^Other chat$/ }));
    });
    expect(within(sidebar).queryByTitle("有新内容")).not.toBeInTheDocument();
  });

  it("marks inactive sessions when a thread update arrives", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Open chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Scheduled update target",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    await act(async () => {
      fireEvent.click(within(sidebar).getByRole("button", { name: /^Open chat$/ }));
    });

    act(() => {
      for (const handler of sessionUpdateHandlers) handler("chat-b", "thread");
    });

    expect(within(sidebar).getByTitle("有新内容")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(within(sidebar).getByRole("button", { name: /^Scheduled update target$/ }));
    });

    expect(within(sidebar).queryByTitle("有新内容")).not.toBeInTheDocument();
  });

  it("restores sidebar run indicators after a page reload", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Running after reload",
        runStartedAt: 12_345,
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Completed after reload",
      },
    ];
    localStorage.setItem(
      "biscuitbot-webui.sidebar.session-updates.v1",
      JSON.stringify(["chat-b"]),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    await waitFor(() =>
      expect(within(sidebar).getByTitle("Agent 正在运行")).toBeInTheDocument(),
    );
    expect(within(sidebar).getByTitle("有新内容")).toBeInTheDocument();
    expect(attachSpy).toHaveBeenCalledWith("chat-a");
  });

  it("restores the active chat from the URL hash after a page reload", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Active after reload",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Other chat",
      },
    ];
    window.history.replaceState(
      null,
      "",
      `/#/chat/${encodeURIComponent("websocket:chat-a")}`,
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    await waitFor(() => expect(document.title).toBe("Active after reload · biscuitbot"));
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    expect(
      within(sidebar).getByRole("button", { name: /^Active after reload$/ }),
    ).toBeInTheDocument();
    expect(window.location.hash).toBe(
      `#/chat/${encodeURIComponent("websocket:chat-a")}`,
    );
  });

  it("opens the settings view from the sidebar footer", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const href = String(input);
        if (href === "/api/settings/provider-models?provider=openai") {
          return jsonResponse({
            provider: "openai",
            label: "OpenAI",
            status: "available",
            catalog_kind: "official",
            models: [
              { id: "openai/gpt-4o", owned_by: "openai", context_window: 128000 },
              { id: "openai/gpt-4o-mini", owned_by: "openai", context_window: 128000 },
            ],
            model_count: 2,
            fetched_at: 1,
          });
        }
        if (href.includes("/api/settings")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              agent: {
                model: "openai/gpt-4o",
                provider: "auto",
                resolved_provider: "openai",
                has_api_key: true,
                model_preset: "default",
                max_tokens: 8192,
                context_window_tokens: 65536,
                temperature: 0.1,
                reasoning_effort: null,
                timezone: "UTC",
                bot_name: "biscuitbot",
                bot_icon: "nb",
                tool_hint_max_length: 40,
              },
              model_presets: [
                {
                  name: "default",
                  label: "Default",
                  active: true,
                  is_default: true,
                  model: "openai/gpt-4o",
                  provider: "auto",
                  max_tokens: 8192,
                  context_window_tokens: 65536,
                  temperature: 0.1,
                  reasoning_effort: null,
                },
                {
                  name: "deep",
                  label: "deep",
                  active: false,
                  is_default: false,
                  model: "anthropic/claude-opus-4-5",
                  provider: "anthropic",
                  max_tokens: 8192,
                  context_window_tokens: 200000,
                  temperature: 0.1,
                  reasoning_effort: "high",
                },
              ],
              providers: [
                {
                  name: "openai",
                  label: "OpenAI",
                  configured: true,
                  api_key_hint: "open••••-key",
                },
                {
                  name: "openrouter",
                  label: "OpenRouter",
                  configured: false,
                  api_key_required: true,
                  default_api_base: "https://openrouter.ai/api/v1",
                },
                {
                  name: "ant_ling",
                  label: "Ant Ling",
                  configured: false,
                  api_key_required: true,
                  default_api_base: "https://api.ant-ling.com/v1",
                },
                {
                  name: "azure_openai",
                  label: "Azure OpenAI",
                  configured: false,
                  api_key_required: true,
                },
                {
                  name: "huggingface",
                  label: "Hugging Face",
                  configured: false,
                  api_key_required: true,
                },
                {
                  name: "siliconflow",
                  label: "SiliconFlow",
                  configured: false,
                  api_key_required: true,
                },
                {
                  name: "volcengine",
                  label: "VolcEngine",
                  configured: false,
                  api_key_required: true,
                },
                {
                  name: "byteplus",
                  label: "BytePlus",
                  configured: false,
                  api_key_required: true,
                },
                {
                  name: "qianfan",
                  label: "Qianfan",
                  configured: false,
                  api_key_required: true,
                },
                {
                  name: "atomic_chat",
                  label: "Atomic Chat",
                  configured: false,
                  api_key_required: false,
                  default_api_base: "http://localhost:1337/v1",
                },
              ],
              web_search: {
                provider: "brave",
                api_key_hint: "BSAo••••ew20",
                base_url: null,
                max_results: 5,
                timeout: 30,
                providers: [
                  { name: "duckduckgo", label: "DuckDuckGo", credential: "none" },
                  { name: "brave", label: "Brave Search", credential: "api_key" },
                  { name: "tavily", label: "Tavily", credential: "api_key" },
                ],
              },
              web: {
                enable: true,
                proxy: null,
                user_agent: null,
                search: { max_results: 5, timeout: 30 },
                fetch: { use_jina_reader: true },
              },
              image_generation: {
                enabled: false,
                provider: "openrouter",
                provider_configured: true,
                model: "openai/gpt-5.4-image-2",
                default_aspect_ratio: "1:1",
                default_image_size: "1K",
                max_images_per_turn: 4,
                save_dir: "generated",
                providers: [
                  {
                    name: "openrouter",
                    label: "OpenRouter",
                    configured: true,
                    api_key_hint: "sk-o••••test",
                    api_base: "https://openrouter.ai/api/v1",
                    default_api_base: "https://openrouter.ai/api/v1",
                  },
                  {
                    name: "gemini",
                    label: "Gemini",
                    configured: false,
                    api_key_hint: null,
                    api_base: null,
                    default_api_base: "https://generativelanguage.googleapis.com/v1beta/openai/",
                  },
                ],
              },
              video_generation: {
                enabled: false,
                api_key_configured: false,
                model: "doubao-seedance",
                default_ratio: "16:9",
                default_duration: 6,
                default_resolution: null,
                generate_audio: true,
                watermark: false,
                save_dir: "generated/videos",
              },
              screenshot: {
                enabled: false,
                max_width: 1920,
                max_height: 1080,
                quality: 70,
                vision_model: null,
                vision_model_override: null,
                vision_model_configured: false,
                resolved_model: null,
                available_providers: [],
              },
              system_io: {
                enabled: false,
                allow_actions: [],
                available_actions: [],
              },
              runtime: {
                config_path: "/tmp/config.json",
                workspace_path: "/tmp/workspace",
                gateway_host: "127.0.0.1",
                gateway_port: 18790,
                heartbeat: {
                  enabled: true,
                  interval_s: 1800,
                  keep_recent_messages: 8,
                },
                dream: {
                  schedule: "every 2h",
                },
                unified_session: false,
              },
              advanced: {
                restrict_to_workspace: false,
                webui_allow_local_service_access: true,
                webui_default_access_mode: "default",
                private_service_protection_enabled: true,
                ssrf_whitelist_count: 0,
                mcp_server_count: 0,
                exec_enabled: true,
                exec_sandbox: null,
                exec_path_prepend_set: false,
                exec_path_append_set: false,
              },
              requires_restart: false,
            }),
          };
        }
        return { ok: false, status: 404, json: async () => ({}) };
      }),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    const searchButton = within(sidebar).getByRole("button", { name: "搜索" });
    const employeesButton = within(sidebar).getByRole("button", { name: "数字员工" });
    // 搜索已移入历史对话容器（导航组下方），故应位于「数字员工」之后
    expect(employeesButton.compareDocumentPosition(searchButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(within(sidebar).getByRole("button", { name: "设置" }));

    expect(await screen.findByRole("heading", { name: "概览" })).toBeInTheDocument();
    expect(document.title).toBe("设置 · biscuitbot");
    expect(screen.getByTestId("overview-logo-openai")).toBeInTheDocument();
    expect(screen.queryByTestId("overview-logo-brave")).not.toBeInTheDocument();
    expect(screen.queryByTestId("overview-logo-openrouter")).not.toBeInTheDocument();
    expect(screen.queryByTestId("overview-logo-biscuitbot-gateway")).not.toBeInTheDocument();
    expect(screen.queryByTestId("overview-logo-biscuitbot-workspace")).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "侧边栏导航" })).not.toBeInTheDocument();
    const settingsNav = screen.getByRole("navigation", { name: "设置分区" });
    expect(settingsNav.className).toContain("overflow-x-auto");
    expect(settingsNav.className).not.toContain("grid-cols-2");
    expect(within(settingsNav).getByRole("button", { name: "概览" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(settingsNav).getByRole("button", { name: "模型" })).toBeInTheDocument();
    expect(within(settingsNav).queryByRole("button", { name: "提供商" })).not.toBeInTheDocument();
    // 侧边栏顶层标签：概览/外观/模型/网页/应用/渠道/系统；图片、安全等并入二级子区
    expect(within(settingsNav).getByRole("button", { name: "系统" })).toBeInTheDocument();
    expect(within(settingsNav).getByRole("button", { name: "网页" })).toBeInTheDocument();
    expect(within(settingsNav).getByRole("button", { name: "应用" })).toBeInTheDocument();
    expect(within(settingsNav).getByRole("button", { name: "渠道" })).toBeInTheDocument();
    expect(within(settingsNav).queryByRole("button", { name: "安全" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "退出登录" })).toBeInTheDocument();
    fireEvent.click(within(settingsNav).getByRole("button", { name: "外观" }));
    expect(screen.getByText("品牌 Logo")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "品牌 Logo" })).toBeInTheDocument();
    fireEvent.click(within(settingsNav).getByRole("button", { name: "模型" }));
    expect(screen.queryByText("AI")).not.toBeInTheDocument();
    expect(screen.getByText("当前配置")).toBeInTheDocument();
    expect(screen.queryByText("Presets")).not.toBeInTheDocument();
    fireEvent.pointerDown(screen.getByRole("button", { name: "当前配置" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "添加配置" }));
    const modelDialog = await screen.findByRole("dialog", { name: "新建模型配置" });
    expect(within(modelDialog).getByText("把提供商和模型保存成一键选项。")).toBeInTheDocument();
    fireEvent.change(within(modelDialog).getByPlaceholderText("快速写作"), {
      target: { value: "快速写作" },
    });
    fireEvent.change(within(modelDialog).getByPlaceholderText("openai/gpt-4.1"), {
      target: { value: "openai/gpt-4.1-mini" },
    });
    expect(within(modelDialog).getByRole("button", { name: /OpenAI/ })).toBeInTheDocument();
    expect(within(modelDialog).getByRole("button", { name: "保存" })).toBeEnabled();
    fireEvent.click(within(modelDialog).getByRole("button", { name: "取消" }));
    fireEvent.pointerDown(screen.getByRole("button", { name: /自动/ }));
    expect(screen.getAllByTestId("provider-picker-logo-openai").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("menuitem", { name: /自动/ }));
    const openModelPicker = () => {
      const modelButtons = screen.getAllByRole("button", { name: /openai\/gpt-4o/ });
      fireEvent.pointerDown(modelButtons[modelButtons.length - 1]);
    };
    openModelPicker();
    await screen.findByText("openai/gpt-4o-mini");
    fireEvent.click(screen.getAllByText("openai/gpt-4o-mini")[0]);
    expect(screen.getByText("有未保存的更改。").parentElement?.className).toContain(
      "text-blue-600",
    );
    const updatedModelButtons = screen.getAllByRole("button", { name: /openai\/gpt-4o-mini/ });
    fireEvent.pointerDown(updatedModelButtons[updatedModelButtons.length - 1]);
    await screen.findByText("openai/gpt-4o");
    fireEvent.click(screen.getAllByText("openai/gpt-4o")[0]);
    expect(screen.getByText("OpenRouter")).toBeInTheDocument();
    expect(screen.getByText("Ant Ling")).toBeInTheDocument();
    expect(screen.getByTestId("provider-logo-openai")).toBeInTheDocument();
    expect(screen.getByText(/产品名称、Logo 和品牌/)).toBeInTheDocument();
    expect(screen.getAllByText("未配置").length).toBeGreaterThan(0);
    const clickProviderRow = (label: string) => {
      const providerLabel = screen
        .getAllByText(label)
        .find((element) => element.className.includes("font-semibold"));
      expect(providerLabel).toBeTruthy();
      fireEvent.click(providerLabel!);
    };
    // 收权后：提供商列表只读，展开仅展示密钥状态与地址，无编辑/保存入口。
    clickProviderRow("OpenAI");
    expect(screen.getByText("密钥状态")).toBeInTheDocument();
    expect(screen.getByText("open••••-key")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存提供商" })).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("留空则保留当前 key")).not.toBeInTheDocument();
    expect(
      screen.getByText(/此处仅展示状态/),
    ).toBeInTheDocument();
    clickProviderRow("Ant Ling");
    expect(screen.getByText("https://api.ant-ling.com/v1")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("https://api.ant-ling.com/v1")).not.toBeInTheDocument();
    clickProviderRow("Atomic Chat");
    expect(screen.getByText("http://localhost:1337/v1")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("http://localhost:1337/v1")).not.toBeInTheDocument();

    // 图片生成是「模型」tab 的二级子区（文生图），经子区切换条进入
    fireEvent.click(within(screen.getByTestId("settings-subtabs")).getByRole("button", { name: "文生图" }));
    expect(screen.getByRole("heading", { name: "模型" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "图片生成" })).toBeInTheDocument();
    expect(screen.getByText("提供商状态")).toBeInTheDocument();
    expect(screen.getByDisplayValue("openai/gpt-5.4-image-2")).toBeInTheDocument();
    expect(screen.getByText("保存目录")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();

    fireEvent.click(within(settingsNav).getByRole("button", { name: "网页" }));
    expect(screen.getByText("搜索服务商")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Jina 阅读器" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Brave Search/ })).toBeInTheDocument();
    expect(screen.queryByTestId("provider-picker-logo-brave")).not.toBeInTheDocument();
    expect(screen.getByText("BSAo••••ew20")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByPlaceholderText("留空则保留当前 key"), {
      target: { value: "unsaved-brave-key" },
    });
    fireEvent.pointerDown(screen.getByRole("button", { name: /Brave Search/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Tavily" }));
    fireEvent.pointerDown(screen.getByRole("button", { name: /Tavily/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Brave Search" }));
    expect(screen.getByText("BSAo••••ew20")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("unsaved-brave-key")).not.toBeInTheDocument();

    fireEvent.click(within(settingsNav).getByRole("button", { name: "系统" }));
    expect(screen.getByText("Bot 名称")).toBeInTheDocument();
    expect(screen.queryByText("Tool hint length")).not.toBeInTheDocument();
    expect(screen.queryByText("Heartbeat")).not.toBeInTheDocument();
    expect(screen.queryByText("Dream")).not.toBeInTheDocument();
    expect(screen.queryByText("Unified session")).not.toBeInTheDocument();
    expect(screen.getByText("默认工作区")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
    fireEvent.pointerDown(screen.getByRole("button", { name: "UTC" }));
    expect(screen.getByPlaceholderText("搜索时区")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("搜索时区"), {
      target: { value: "Shanghai" },
    });
    fireEvent.click(screen.getByRole("menuitem", { name: /Asia\/Shanghai/ }));
    expect(screen.getByRole("button", { name: "Asia/Shanghai" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存" })).toBeEnabled();
  });

  it("restores the settings section from the URL hash after a page reload", async () => {
    mockFetchRoutes({ "/api/settings": baseSettingsPayload() });
    window.history.replaceState(null, "", "/#/settings?section=voice");

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    expect(await screen.findByRole("heading", { name: "语音识别" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#/settings?section=voice");
  });

  it("updates the URL hash when switching settings sections", async () => {
    mockFetchRoutes({ "/api/settings": baseSettingsPayload() });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "设置" }));
    expect(await screen.findByRole("heading", { name: "概览" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#/settings");

    const settingsNav = screen.getByRole("navigation", { name: "设置分区" });
    fireEvent.click(within(settingsNav).getByRole("button", { name: "模型" }));

    expect(await screen.findByRole("heading", { name: "模型" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#/settings?section=models");

    // 语音（ASR）是「模型」tab 的二级子区，经子区切换条进入
    fireEvent.click(within(screen.getByTestId("settings-subtabs")).getByRole("button", { name: "ASR" }));

    expect(await screen.findByRole("heading", { name: "语音识别" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#/settings?section=voice");
  });

  it("returns from settings to the blank start page when no session was active", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "First chat",
      },
      {
        key: "websocket:chat-b",
        channel: "websocket",
        chatId: "chat-b",
        createdAt: "2026-04-16T11:00:00Z",
        updatedAt: "2026-04-16T11:00:00Z",
        preview: "Second chat",
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/api/settings")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              agent: {
                model: "openai/gpt-4o",
                provider: "openai",
                resolved_provider: "openai",
                has_api_key: true,
                model_preset: "default",
                max_tokens: 8192,
                context_window_tokens: 65536,
                temperature: 0.1,
                reasoning_effort: null,
                timezone: "UTC",
                bot_name: "biscuitbot",
                bot_icon: "nb",
                tool_hint_max_length: 40,
              },
              model_presets: [
                {
                  name: "default",
                  label: "Default",
                  active: true,
                  is_default: true,
                  model: "openai/gpt-4o",
                  provider: "openai",
                  max_tokens: 8192,
                  context_window_tokens: 65536,
                  temperature: 0.1,
                  reasoning_effort: null,
                },
              ],
              providers: [{ name: "openai", label: "OpenAI", configured: true }],
              web_search: {
                provider: "duckduckgo",
                api_key_hint: null,
                base_url: null,
                max_results: 5,
                timeout: 30,
                providers: [
                  { name: "duckduckgo", label: "DuckDuckGo", credential: "none" },
                  { name: "brave", label: "Brave Search", credential: "api_key" },
                ],
              },
              web: {
                enable: true,
                proxy: null,
                user_agent: null,
                search: { max_results: 5, timeout: 30 },
                fetch: { use_jina_reader: true },
              },
              image_generation: {
                enabled: false,
                provider: "openrouter",
                provider_configured: false,
                model: "openai/gpt-5.4-image-2",
                default_aspect_ratio: "1:1",
                default_image_size: "1K",
                max_images_per_turn: 4,
                save_dir: "generated",
                providers: [
                  {
                    name: "openrouter",
                    label: "OpenRouter",
                    configured: false,
                    api_key_hint: null,
                    api_base: null,
                    default_api_base: "https://openrouter.ai/api/v1",
                  },
                ],
              },
              video_generation: {
                enabled: false,
                api_key_configured: false,
                model: "doubao-seedance",
                default_ratio: "16:9",
                default_duration: 6,
                default_resolution: null,
                generate_audio: true,
                watermark: false,
                save_dir: "generated/videos",
              },
              screenshot: {
                enabled: false,
                max_width: 1920,
                max_height: 1080,
                quality: 70,
                vision_model: null,
                vision_model_override: null,
                vision_model_configured: false,
                resolved_model: null,
                available_providers: [],
              },
              system_io: {
                enabled: false,
                allow_actions: [],
                available_actions: [],
              },
              runtime: {
                config_path: "/tmp/config.json",
                workspace_path: "/tmp/workspace",
                gateway_host: "127.0.0.1",
                gateway_port: 18790,
                heartbeat: {
                  enabled: true,
                  interval_s: 1800,
                  keep_recent_messages: 8,
                },
                dream: {
                  schedule: "every 2h",
                },
                unified_session: false,
              },
              advanced: {
                restrict_to_workspace: false,
                webui_allow_local_service_access: true,
                webui_default_access_mode: "default",
                private_service_protection_enabled: true,
                ssrf_whitelist_count: 0,
                mcp_server_count: 0,
                exec_enabled: true,
                exec_sandbox: null,
                exec_path_prepend_set: false,
                exec_path_append_set: false,
              },
              requires_restart: false,
            }),
          };
        }
        return { ok: false, status: 404, json: async () => ({}) };
      }),
    );

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "新建对话" }));
    await waitFor(() => expect(document.title).toBe("biscuitbot"));

    fireEvent.click(within(sidebar).getByRole("button", { name: "设置" }));
    expect(await screen.findByRole("heading", { name: "概览" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回聊天" }));

    await waitFor(() => expect(document.title).toBe("biscuitbot"));
    expect(screen.getByText(HERO_GREETING_PATTERN)).toBeInTheDocument();
  });

  it("filters sessions in the centered search dialog", async () => {
    mockSessions = [
      {
        key: "websocket:chat-alpha",
        channel: "websocket",
        chatId: "chat-alpha",
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
        title: "Q2 roadmap",
        preview: "Project planning notes",
      },
      {
        key: "websocket:chat-beta",
        channel: "websocket",
        chatId: "chat-beta",
        createdAt: "2026-04-15T10:00:00Z",
        updatedAt: "2026-04-15T10:00:00Z",
        preview: "Travel ideas",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    expect(within(sidebar).getByText("Q2 roadmap")).toBeInTheDocument();
    expect(within(sidebar).getByText("Travel ideas")).toBeInTheDocument();
    const newChatButton = within(sidebar).getByRole("button", { name: "新建对话" });
    const searchButton = within(sidebar).getByRole("button", { name: "搜索" });
    expect(
      newChatButton.compareDocumentPosition(searchButton) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    fireEvent.click(searchButton);
    const dialog = await screen.findByRole("dialog", { name: "搜索" });
    expect(dialog).toHaveClass("origin-center");
    expect(dialog.className).not.toContain("translate-x");
    expect(dialog.className).not.toContain("translate-y");
    expect(dialog.querySelector("kbd")).toBeNull();
    expect(within(dialog).getByText("Q2 roadmap")).toBeInTheDocument();
    expect(within(dialog).getByText("Travel ideas")).toBeInTheDocument();
    expect(within(dialog).queryByText("websocket")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("#1")).not.toBeInTheDocument();

    fireEvent.change(within(dialog).getByRole("textbox", { name: "搜索" }), {
      target: { value: "planning" },
    });

    expect(within(dialog).getByText("Q2 roadmap")).toBeInTheDocument();
    expect(within(dialog).queryByText("Travel ideas")).not.toBeInTheDocument();
    expect(within(sidebar).getByText("Travel ideas")).toBeInTheDocument();

    fireEvent.change(within(dialog).getByRole("textbox", { name: "搜索" }), {
      target: { value: "road q2" },
    });

    expect(within(dialog).getByText("Q2 roadmap")).toBeInTheDocument();
    expect(within(dialog).queryByText("Travel ideas")).not.toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: /Q2 roadmap/ }));

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "搜索" })).not.toBeInTheDocument(),
    );
  });

  it("opens search from the keyboard shortcut", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    fireEvent.keyDown(window, { key: "k", metaKey: true });

    const dialog = await screen.findByRole("dialog", { name: "搜索" });
    expect(within(dialog).queryByText("Global actions")).not.toBeInTheDocument();
    expect(within(dialog).getByText("Existing chat")).toBeInTheDocument();

    const textbox = within(dialog).getByRole("textbox", { name: "搜索" });
    fireEvent.change(textbox, { target: { value: "missing" } });
    expect(within(dialog).queryByText("Existing chat")).not.toBeInTheDocument();

    fireEvent.change(textbox, { target: { value: "existing" } });
    expect(within(dialog).getByText("Existing chat")).toBeInTheDocument();

    fireEvent.keyDown(textbox, { key: "Enter" });
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "搜索" })).not.toBeInTheDocument(),
    );
    expect(createChatSpy).not.toHaveBeenCalled();
  });

  it.each([
    ["Command", { metaKey: true }],
    ["Control", { ctrlKey: true }],
  ])("starts a new chat from the %s keyboard shortcut", async (_label, modifier) => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    fireEvent.keyDown(window, { key: "O", shiftKey: true, ...modifier });

    expect(window.location.hash).toBe("#/new");
  });

  it("closes search when starting a new chat from the keyboard shortcut", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(await screen.findByRole("dialog", { name: "搜索" })).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "O", shiftKey: true, metaKey: true });

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "搜索" })).not.toBeInTheDocument(),
    );
    expect(window.location.hash).toBe("#/new");
  });

  it("exposes the new chat keyboard shortcut in the sidebar title", async () => {
    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });

    const newChatButton = within(sidebar).getByRole("button", { name: "新建对话" });
    expect(newChatButton).toHaveAttribute(
      "title",
      "新建对话 (Ctrl+Shift+O)",
    );
    expect(newChatButton).toHaveAttribute(
      "aria-keyshortcuts",
      "Meta+Shift+O Control+Shift+O",
    );
  });

  it("uses macOS shortcut glyphs in the sidebar title", async () => {
    setNavigatorPlatform("MacIntel");
    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });

    expect(within(sidebar).getByRole("button", { name: "新建对话" })).toHaveAttribute(
      "title",
      "新建对话 (⌘⇧O)",
    );
  });

  it("keeps large sidebars light while search still covers every chat", async () => {
    mockSessions = Array.from({ length: 170 }, (_, index) => {
      const chatId = `chat-${index}`;
      return {
        key: `websocket:${chatId}`,
        channel: "websocket" as const,
        chatId,
        createdAt: new Date(Date.UTC(2026, 3, 16, 12, 0 - index)).toISOString(),
        updatedAt: new Date(Date.UTC(2026, 3, 16, 12, 0 - index)).toISOString(),
        title: index === 169 ? "Hidden target" : `Bulk chat ${index}`,
        preview: "",
      };
    });

    render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());
    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    await waitFor(() =>
      expect(within(sidebar).getByRole("button", { name: "Bulk chat 0" })).toBeInTheDocument(),
    );
    expect(within(sidebar).queryByText("Hidden target")).not.toBeInTheDocument();
    expect(within(sidebar).getByRole("button", { name: /再显示/ })).toBeInTheDocument();

    fireEvent.click(within(sidebar).getByRole("button", { name: "搜索" }));
    const dialog = await screen.findByRole("dialog", { name: "搜索" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "搜索" }), {
      target: { value: "hidden" },
    });
    expect(within(dialog).getByText("Hidden target")).toBeInTheDocument();
  });

  it("opens a blank start page without creating an empty chat", async () => {
    mockSessions = [
      {
        key: "websocket:chat-a",
        channel: "websocket",
        chatId: "chat-a",
        createdAt: "2026-04-16T10:00:00Z",
        updatedAt: "2026-04-16T10:00:00Z",
        preview: "Existing chat",
      },
    ];

    const matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query.includes("1024px"),
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    vi.stubGlobal("matchMedia", matchMedia);

    const { container } = render(<App />);

    await waitFor(() => expect(connectSpy).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "从顶部切换主题" }));
    expect(toggleThemeSpy).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "收起侧边栏" }));
    const sidebarAside = container.querySelector("aside.lg\\:block") as HTMLElement;
    await waitFor(() => expect(sidebarAside.style.width).toBe("56px"));

    expect(screen.queryByRole("button", { name: "Start a new chat" })).not.toBeInTheDocument();
    const rail = screen.getByRole("navigation", { name: "侧边栏导航" });
    expect(within(rail).getByRole("button", { name: "新建对话" })).toBeInTheDocument();
    expect(within(rail).getByRole("button", { name: "搜索" })).toBeInTheDocument();
    expect(within(rail).queryByRole("button", { name: "View" })).not.toBeInTheDocument();
    expect(within(rail).queryByText("Existing chat")).not.toBeInTheDocument();

    fireEvent.click(within(rail).getByRole("button", { name: "切换侧边栏" }));
    await waitFor(() => expect(sidebarAside.style.width).toBe("272px"));

    const sidebar = screen.getByRole("navigation", { name: "侧边栏导航" });
    fireEvent.click(within(sidebar).getByRole("button", { name: "新建对话" }));
    expect(createChatSpy).not.toHaveBeenCalled();
    expect(screen.getByText(HERO_GREETING_PATTERN)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start a new chat" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "从顶部切换主题" })).toBeInTheDocument();
    expect(within(sidebar).getByRole("button", { name: "设置" })).toBeInTheDocument();

    expect(within(sidebar).getByText("Existing chat")).toBeInTheDocument();
  });

  it("refreshes the bootstrap token before REST settings auth expires", async () => {
    vi.useFakeTimers();
    vi.mocked(fetchBootstrap)
      .mockResolvedValueOnce({
        token: "tok-1",
        ws_path: "/",
        expires_in: 30,
      })
      .mockResolvedValueOnce({
        token: "tok-2",
        ws_path: "/",
        expires_in: 300,
      });
    vi.mocked(deriveWsUrl).mockImplementation(
      (_wsPath: string, token: string) => `ws://test?token=${token}`,
    );

    const { unmount } = render(<App />);
    await act(async () => {});

    expect(connectSpy).toHaveBeenCalled();
    expect(fetchBootstrap).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });

    expect(fetchBootstrap).toHaveBeenCalledTimes(2);
    expect(updateUrlSpy).toHaveBeenCalledWith("ws://test?token=tok-2");
    unmount();
  });
});
