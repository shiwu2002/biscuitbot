import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "@/i18n";

// Real useSessions + real Shell + real ThreadShell. We only fake the
// transport: bootstrap, WebSocket client, and the HTTP API responses.
const forkChatSpy = vi.fn().mockResolvedValue("chat-fork");
const newChatSpy = vi.fn().mockResolvedValue("chat-new");

const sessionUpdateHandlers = new Set<(chatId: string, scope?: string) => void>();

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
    status = "open" as const;
    defaultChatId: string | null = null;
    connect = vi.fn();
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
    newChat = newChatSpy;
    forkChat = forkChatSpy;
    attach = vi.fn();
    close = vi.fn();
    updateUrl = vi.fn();
  }
  return { BiscuitbotClient: MockClient };
});

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response;
}

const SOURCE_KEY = "websocket:chat-a";
const SOURCE_CHAT = "chat-a";
const FORK_KEY = "websocket:chat-fork";

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
    web_search: { provider: "duckduckgo", api_key_hint: null, base_url: null, max_results: 5, timeout: 30, providers: [] },
    web: { enable: true, proxy: null, user_agent: null, search: { max_results: 5, timeout: 30 }, fetch: { use_jina_reader: true } },
    image_generation: { enabled: false, provider: "openrouter", provider_configured: false, model: "openai/gpt-5.4-image-2", default_aspect_ratio: "1:1", default_image_size: "1K", max_images_per_turn: 4, save_dir: "generated" },
    video_generation: { enabled: false, api_key_configured: false, model: "doubao-seedance", default_ratio: "16:9", default_duration: 6, default_resolution: null, generate_audio: true, watermark: false, save_dir: "generated/videos" },
    screenshot: { enabled: false, max_width: 1920, max_height: 1080, quality: 70, vision_model: null, vision_model_override: null, vision_model_configured: false, resolved_model: null },
    system_io: { enabled: false, allow_actions: [], available_actions: [] },
    runtime: { config_path: "/tmp/config.json", workspace_path: "/tmp/workspace", gateway_host: "127.0.0.1", gateway_port: 18790, heartbeat: { enabled: true, interval_s: 1800, keep_recent_messages: 8 }, dream: { schedule: "every 2h", max_batch_size: 20, max_iterations: 15, annotate_line_ages: true }, unified_session: false },
    advanced: { restrict_to_workspace: false, webui_allow_local_service_access: true, webui_default_access_mode: "default" },
    ui: { language: "zh-CN", theme: "auto" },
    workspace: { project_path: null, project_name: null, restrict_to_workspace: false, access_mode: "default" },
    security: { guard_level: 0, allow_any_workspace: false, workspaces: [] },
    channels: { telegram_bot_token: null, telegram_chat_id: null, discord_bot_token: null, slack_bot_token: null },
    webui: { auto_open_browser: false, host: "127.0.0.1", port: 18790 },
    dream: { enabled: true, schedule: "every 2h", max_batch_size: 20, max_iterations: 15, annotate_line_ages: true },
    metadata: {},
    requires_restart: false,
  };
}

import App from "@/App";

describe("fork navigation", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("zh-CN");
    forkChatSpy.mockReset().mockResolvedValue("chat-fork");
    newChatSpy.mockReset().mockResolvedValue("chat-new");
    sessionUpdateHandlers.clear();
    window.history.replaceState(null, "", "/");
    localStorage.removeItem("biscuitbot-webui.sidebar");
    localStorage.removeItem("biscuitbot-webui.sidebar.session-updates.v1");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("navigates to the forked session instead of resetting to a new conversation", async () => {
    // After the fork is created, the server session list eventually contains
    // both the source and the fork. Emulate the normal catch-up refresh.
    let forkPersisted = false;
    const sessionsRoute = async () => {
      const rows = [
        {
          key: SOURCE_KEY,
          created_at: "2026-04-16T10:00:00Z",
          updated_at: "2026-04-16T10:01:00Z",
          title: "Source chat",
          preview: "Original question",
        },
      ];
      if (forkPersisted) {
        rows.push({
          key: FORK_KEY,
          created_at: "2026-04-16T10:02:00Z",
          updated_at: "2026-04-16T10:02:00Z",
          title: "分叉：Source chat",
          preview: "Original question",
        });
      }
      return { sessions: rows };
    };
    const forkThreadKey = `/api/sessions/${encodeURIComponent(FORK_KEY)}/webui-thread`;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/sessions") return jsonResponse(await sessionsRoute());
        if (url.startsWith(forkThreadKey)) {
          return jsonResponse({
            schemaVersion: 3,
            messages: [
              { id: "u1", role: "user", content: "Original question", createdAt: 1 },
              { id: "a1", role: "assistant", content: "Original answer", createdAt: 2 },
            ],
            fork_boundary_message_count: 2,
            page: { before_cursor: null, has_more_before: false, loaded_message_count: 2, user_message_offset: 1 },
          });
        }
        if (url.startsWith(`/api/sessions/${encodeURIComponent(SOURCE_KEY)}/webui-thread`)) {
          return jsonResponse({
            schemaVersion: 3,
            messages: [
              { id: "u1", role: "user", content: "Original question", createdAt: 1 },
              { id: "a1", role: "assistant", content: "Original answer", createdAt: 2 },
            ],
            page: { before_cursor: null, has_more_before: false, loaded_message_count: 2, user_message_offset: 1 },
          });
        }
        const fixed: Record<string, unknown> = {
          "/api/settings": baseSettingsPayload(),
          "/api/webui/skills": { skills: [] },
          "/api/webui/employees": { employees: [] },
          "/api/webui/workspaces": { default_scope: { project_path: "/tmp/project", project_name: "project", access_mode: "restricted", restrict_to_workspace: true }, controls: null },
          "/api/slash-commands": { commands: [] },
          "/api/webui/cli-apps": { apps: [] },
          "/api/webui/mcp-presets": { presets: [] },
        };
        const body = fixed[url];
        if (body !== undefined) return jsonResponse(body);
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );

    render(<App />);

    // Wait until the app is booted and the sidebar lists the source session.
    const sourceRow = await screen.findByText("Source chat");
    fireEvent.click(sourceRow);

    // Wait for the thread to render the assistant reply, then click fork.
    const answer = await screen.findByText("Original answer");
    const bubble = answer.closest(".w-full");
    expect(bubble).not.toBeNull();

    fireEvent.click(
      (bubble as HTMLElement).querySelector('[aria-label="分叉"]') as HTMLElement,
    );

    await waitFor(() =>
      expect(forkChatSpy).toHaveBeenCalledWith(SOURCE_CHAT, 2, expect.any(String), expect.any(Number)),
    );

    // The optimistic fork row appears in the sidebar list.
    await screen.findAllByText("分叉：Source chat");

    // Server catches up: fork persisted, refresh returns the authoritative row.
    await act(async () => {
      forkPersisted = true;
      for (const handler of sessionUpdateHandlers) handler("chat-fork", "metadata");
    });

    // After everything settles the hash must still point at the fork session.
    await waitFor(() => {
      expect(window.location.hash).toContain("chat-fork");
    });

    // And the fork's thread must be shown (not the hero / new-chat greeting).
    await screen.findByText("Original answer");
    expect(screen.queryByText("我们要一起做点什么？")).not.toBeInTheDocument();
  });
});
