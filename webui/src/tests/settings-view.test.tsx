import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SettingsView, type SettingsSectionKey } from "@/components/settings/SettingsView";
import { ClientProvider } from "@/providers/ClientProvider";
import type { SettingsPayload } from "@/lib/types";

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response;
}

function settingsPayload(): SettingsPayload {
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
      vision_model: null,
      vision_model_override: null,
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
      quality: 85,
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
      guard_level: "standard",
      cold_storage_days: 14,
      duplicate_similarity_threshold: 0.6,
    },
    requires_restart: false,
  };
}

function autoDynamicProviderPayload(
  options: {
    configured: boolean;
    hasApiKey: boolean;
    apiBase: string | null;
    apiKeyHint: string | null;
  },
): SettingsPayload {
  const base = settingsPayload();
  return {
    ...base,
    agent: {
      ...base.agent,
      model: "companyProxy/gpt-4o",
      provider: "companyProxy",
      resolved_provider: "companyProxy",
      has_api_key: options.hasApiKey,
    },
    model_presets: [
      {
        ...base.model_presets[0],
        model: "companyProxy/gpt-4o",
        provider: "auto",
      },
    ],
    providers: [
      {
        name: "companyProxy",
        label: "Company Proxy",
        configured: options.configured,
        auth_type: "api_key",
        api_key_required: false,
        api_key_hint: options.apiKeyHint,
        api_base: options.apiBase,
        default_api_base: null,
      },
    ],
  };
}

const installedAnyGen = {
  name: "anygen",
  display_name: "AnyGen",
  category: "generation",
  description: "Generate docs, slides, websites and more via AnyGen cloud API",
  requires: "ANYGEN_API_KEY",
  source: "harness",
  entry_point: "cli-anything-anygen",
  install_supported: true,
  installed: true,
  available: true,
  status: "installed",
  logo_url: "https://www.google.com/s2/favicons?domain=anygen.io&sz=64",
  brand_color: "#111827",
  skill_installed: true,
};

function renderSettingsView(
  options: {
    initialSection?: SettingsSectionKey;
    initialSettings?: SettingsPayload;
    showSidebar?: boolean;
    onSettingsChange?: (payload: SettingsPayload) => void;
    onNativeEngineRestart?: () => Promise<string>;
  } = {},
) {
  render(
    <ClientProvider client={{} as never} token="tok">
      <SettingsView
        theme="light"
        initialSection={options.initialSection ?? "apps"}
        initialSettings={options.initialSettings}
        showSidebar={options.showSidebar}
        onToggleTheme={() => {}}
        onBackToChat={() => {}}
        onModelNameChange={() => {}}
        onSettingsChange={options.onSettingsChange}
        onNativeEngineRestart={options.onNativeEngineRestart}
      />
    </ClientProvider>,
  );
}

describe("SettingsView Apps catalog", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("does not show the Settings kicker on the standalone Automations surface", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/settings") return jsonResponse(settingsPayload());
      if (url === "/api/webui/automations") return jsonResponse({ jobs: [] });
      return jsonResponse({});
    }));

    renderSettingsView({
      initialSection: "automations",
      initialSettings: settingsPayload(),
      showSidebar: false,
    });

    expect(screen.getByRole("heading", { name: "自动任务" })).toBeInTheDocument();
    expect(await screen.findByText("暂无自动任务。")).toBeInTheDocument();
    expect(screen.queryByText("设置")).not.toBeInTheDocument();
  });

  it("shows a visible uninstall button for installed CLI apps and calls uninstall", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/settings") {
        return jsonResponse(settingsPayload());
      }
      if (url === "/api/settings/cli-apps") {
        return jsonResponse({
          apps: [installedAnyGen],
          installed_count: 1,
          catalog_updated_at: "2026-04-18",
        });
      }
      if (url === "/api/settings/mcp-presets") {
        return jsonResponse({ presets: [], installed_count: 0 });
      }
      if (url === "/api/settings/cli-apps/uninstall?name=anygen") {
        return jsonResponse({
          apps: [{ ...installedAnyGen, installed: false, status: "available" }],
          installed_count: 0,
          catalog_updated_at: "2026-04-18",
          last_action: {
            ok: true,
            message: "Uninstalled CLI for AnyGen.",
            still_available: false,
          },
        });
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSettingsView();

    expect(await screen.findByRole("heading", { name: "应用" })).toBeInTheDocument();
    expect(await screen.findByText("AnyGen")).toBeInTheDocument();
    const uninstall = screen.getByRole("button", { name: "卸载 CLI" });

    fireEvent.click(uninstall);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/settings/cli-apps/uninstall?name=anygen",
        expect.objectContaining({
          headers: { Authorization: "Bearer tok" },
        }),
      ),
    );
    expect(await screen.findByText("Uninstalled CLI for AnyGen.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(screen.queryByText("Uninstalled CLI for AnyGen.")).not.toBeInTheDocument();
  });

  it("publishes the latest settings payload to the shell", async () => {
    const payload = settingsPayload();
    const onSettingsChange = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/settings") return jsonResponse(payload);
        if (url === "/api/settings/cli-apps") {
          return jsonResponse({ apps: [], installed_count: 0 });
        }
        if (url === "/api/settings/mcp-presets") {
          return jsonResponse({ presets: [], installed_count: 0 });
        }
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );

    renderSettingsView({ onSettingsChange });

    await waitFor(() => expect(onSettingsChange).toHaveBeenCalledWith(payload));
  });

  it("shows token activity on the overview", async () => {
    const payload: SettingsPayload = {
      ...settingsPayload(),
      usage: {
        days: [
          {
            date: "2026-06-03",
            prompt_tokens: 1200,
            completion_tokens: 300,
            cached_tokens: 500,
            total_tokens: 1500,
            requests: 2,
          },
        ],
        total_tokens: 1500,
        total_tokens_30d: 1500,
        total_tokens_365d: 1500,
        peak_day_tokens: 1500,
        current_streak_days: 1,
        longest_streak_days: 1,
        active_days_30d: 1,
        requests_30d: 2,
        updated_at: "2026-06-03T00:00:00Z",
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/settings") return jsonResponse(payload);
        if (url === "/api/settings/cli-apps") {
          return jsonResponse({ apps: [], installed_count: 0 });
        }
        if (url === "/api/settings/mcp-presets") {
          return jsonResponse({ presets: [], installed_count: 0 });
        }
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );

    renderSettingsView({ initialSection: "overview" });

    expect(await screen.findByLabelText("Token 活动")).toBeInTheDocument();
    expect(screen.getByText("Token Usage")).toBeInTheDocument();
    expect(screen.queryByText("Token 活动")).not.toBeInTheDocument();
    expect(screen.queryByText("累计 Token 数")).not.toBeInTheDocument();
    expect(screen.queryByText("峰值 Token 数")).not.toBeInTheDocument();
  });

  it("aligns token activity days with the configured timezone", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-02T18:00:00Z"));
    const basePayload = settingsPayload();
    const payload: SettingsPayload = {
      ...basePayload,
      agent: {
        ...basePayload.agent,
        timezone: "Asia/Shanghai",
      },
      usage: {
        days: [
          {
            date: "2026-06-03",
            prompt_tokens: 1200,
            completion_tokens: 300,
            cached_tokens: 500,
            total_tokens: 1500,
            requests: 2,
          },
        ],
        total_tokens: 1500,
        total_tokens_30d: 1500,
        total_tokens_365d: 1500,
        peak_day_tokens: 1500,
        current_streak_days: 1,
        longest_streak_days: 1,
        active_days_30d: 1,
        requests_30d: 2,
        updated_at: "2026-06-03T00:00:00Z",
      },
    };
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));

    renderSettingsView({ initialSection: "overview", initialSettings: payload });

    expect(screen.getByLabelText("2026-06-03：1.5K tokens，2 次请求")).toBeInTheDocument();
  });

  it("shows context window options in model settings", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/settings") return jsonResponse(settingsPayload());
        if (url === "/api/settings/cli-apps") {
          return jsonResponse({ apps: [], installed_count: 0 });
        }
        if (url === "/api/settings/mcp-presets") {
          return jsonResponse({ presets: [], installed_count: 0 });
        }
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );

    renderSettingsView({ initialSection: "models" });

    expect(await screen.findByText("上下文窗口")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "64K" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "256K" })).toBeInTheDocument();
  });

  it("uses the resolved provider row for auto dynamic providers without api keys", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));

    renderSettingsView({
      initialSection: "models",
      initialSettings: autoDynamicProviderPayload({
        configured: true,
        hasApiKey: false,
        apiBase: "https://proxy.example.test/v1",
        apiKeyHint: null,
      }),
    });

    const configurationButton = await screen.findByRole("button", {
      name: "当前配置",
    });
    expect(configurationButton).toHaveTextContent("companyProxy/gpt-4o");
    expect(configurationButton).toHaveTextContent("Company Proxy");
    expect(configurationButton).not.toHaveTextContent("未配置");
  });

  it("does not treat auto dynamic provider api keys as configured without apiBase", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));

    renderSettingsView({
      initialSection: "models",
      initialSettings: autoDynamicProviderPayload({
        configured: false,
        hasApiKey: true,
        apiBase: null,
        apiKeyHint: "sk-...",
      }),
    });

    const configurationButton = await screen.findByRole("button", {
      name: "当前配置",
    });
    expect(configurationButton).toHaveTextContent("未配置");
    expect(configurationButton).toHaveTextContent("Company Proxy · companyProxy/gpt-4o");
  });

  it("marks the current model as unconfigured when its provider needs setup", async () => {
    const payload: SettingsPayload = {
      ...settingsPayload(),
      agent: {
        ...settingsPayload().agent,
        model: "openai-codex/gpt-5.1-codex",
        provider: "openai_codex",
        resolved_provider: "openai_codex",
        has_api_key: false,
      },
      model_presets: [
        {
          ...settingsPayload().model_presets[0],
          model: "openai-codex/gpt-5.1-codex",
          provider: "openai_codex",
        },
      ],
      providers: [
        {
          name: "openai_codex",
          label: "OpenAI Codex",
          configured: false,
          auth_type: "oauth",
          api_key_required: false,
          api_key_hint: null,
          api_base: null,
          default_api_base: null,
          oauth_account: null,
          oauth_expires_at: null,
          oauth_login_supported: true,
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/settings") return jsonResponse(payload);
        if (url === "/api/settings/cli-apps") {
          return jsonResponse({ apps: [], installed_count: 0 });
        }
        if (url === "/api/settings/mcp-presets") {
          return jsonResponse({ presets: [], installed_count: 0 });
        }
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );

    renderSettingsView({ initialSection: "models" });

    const configurationButton = await screen.findByRole("button", {
      name: "当前配置",
    });
    expect(configurationButton).toHaveTextContent("未配置");
    expect(configurationButton).toHaveTextContent("OpenAI Codex · openai-codex/gpt-5.1-codex");
    // 收权后：OAuth 提供商以只读状态展示，不再提供登录/登出按钮。
    const providerRow = await screen.findByRole("button", { name: /OpenAI Codex/ });
    expect(within(providerRow).getByText("未登录")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "登录" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存提供商" })).not.toBeInTheDocument();
  });

  it("keeps unsigned OAuth providers out of the active provider picker", async () => {
    const payload: SettingsPayload = {
      ...settingsPayload(),
      agent: {
        ...settingsPayload().agent,
        model: "deepseek-chat",
        provider: "deepseek",
        resolved_provider: "deepseek",
      },
      model_presets: [
        {
          ...settingsPayload().model_presets[0],
          model: "deepseek-chat",
          provider: "deepseek",
        },
      ],
      providers: [
        {
          name: "deepseek",
          label: "DeepSeek",
          configured: true,
          auth_type: "api_key",
          api_key_required: true,
          api_key_hint: "sk-...",
          api_base: "https://api.deepseek.com",
          default_api_base: "https://api.deepseek.com",
        },
        {
          name: "openai_codex",
          label: "OpenAI Codex",
          configured: false,
          auth_type: "oauth",
          api_key_required: false,
          api_key_hint: null,
          api_base: null,
          default_api_base: null,
          oauth_account: null,
          oauth_expires_at: null,
          oauth_login_supported: true,
        },
        {
          name: "github_copilot",
          label: "GitHub Copilot",
          configured: false,
          auth_type: "oauth",
          api_key_required: false,
          api_key_hint: null,
          api_base: null,
          default_api_base: "https://api.githubcopilot.com",
          oauth_account: null,
          oauth_expires_at: null,
          oauth_login_supported: true,
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/settings") return jsonResponse(payload);
        if (url === "/api/settings/cli-apps") {
          return jsonResponse({ apps: [], installed_count: 0 });
        }
        if (url === "/api/settings/mcp-presets") {
          return jsonResponse({ presets: [], installed_count: 0 });
        }
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );

    renderSettingsView({ initialSection: "models" });

    const deepseekButtons = await screen.findAllByRole("button", { name: /DeepSeek/ });
    const providerPicker = deepseekButtons.find(
      (button) => button.getAttribute("aria-haspopup") === "menu",
    );
    if (!providerPicker) throw new Error("provider picker was not found");
    fireEvent.pointerDown(providerPicker);

    expect(await screen.findByRole("menuitem", { name: /DeepSeek/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /OpenAI Codex/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /GitHub Copilot/ })).not.toBeInTheDocument();
  });

  it("does not fetch model lists for unsigned OAuth providers", async () => {
    const payload: SettingsPayload = {
      ...settingsPayload(),
      agent: {
        ...settingsPayload().agent,
        model: "",
        provider: "openai_codex",
        resolved_provider: "openai_codex",
      },
      model_presets: [
        {
          ...settingsPayload().model_presets[0],
          model: "",
          provider: "openai_codex",
        },
      ],
      providers: [
        {
          name: "openai_codex",
          label: "OpenAI Codex",
          configured: false,
          auth_type: "oauth",
          api_key_required: false,
          api_key_hint: null,
          api_base: null,
          default_api_base: null,
          oauth_account: null,
          oauth_expires_at: null,
          oauth_login_supported: true,
        },
        {
          name: "github_copilot",
          label: "GitHub Copilot",
          configured: false,
          auth_type: "oauth",
          api_key_required: false,
          api_key_hint: null,
          api_base: null,
          default_api_base: "https://api.githubcopilot.com",
          oauth_account: null,
          oauth_expires_at: null,
          oauth_login_supported: true,
        },
      ],
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/settings") return jsonResponse(payload);
      if (url === "/api/settings/cli-apps") {
        return jsonResponse({ apps: [], installed_count: 0 });
      }
      if (url === "/api/settings/mcp-presets") {
        return jsonResponse({ presets: [], installed_count: 0 });
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSettingsView({ initialSection: "models" });

    fireEvent.pointerDown(await screen.findByRole("button", { name: /选择模型/ }));
    expect(
      await screen.findByText("加载模型前请先配置此提供商。"),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) =>
        String(input).startsWith("/api/settings/provider-models"),
      ),
    ).toBe(false);
  });

  it("prefills manual model ids for configured OAuth providers", async () => {
    const payload: SettingsPayload = {
      ...settingsPayload(),
      agent: {
        ...settingsPayload().agent,
        model: "open-codex/gpt-5.5",
        provider: "openai_codex",
        resolved_provider: "openai_codex",
      },
      model_presets: [
        {
          ...settingsPayload().model_presets[0],
          model: "open-codex/gpt-5.5",
          provider: "openai_codex",
        },
      ],
      providers: [
        {
          name: "openai_codex",
          label: "OpenAI Codex",
          configured: true,
          auth_type: "oauth",
          api_key_required: false,
          api_key_hint: null,
          api_base: null,
          default_api_base: null,
          oauth_account: "acct-test",
          oauth_expires_at: null,
          oauth_login_supported: true,
        },
      ],
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/settings") return jsonResponse(payload);
      if (url === "/api/settings/cli-apps") {
        return jsonResponse({ apps: [], installed_count: 0 });
      }
      if (url === "/api/settings/mcp-presets") {
        return jsonResponse({ presets: [], installed_count: 0 });
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSettingsView({ initialSection: "models" });

    const modelButtons = await screen.findAllByRole("button", { name: /open-codex\/gpt-5\.5/i });
    fireEvent.pointerDown(modelButtons[modelButtons.length - 1]);
    const input = (await screen.findByPlaceholderText("搜索或输入模型 ID")) as HTMLInputElement;
    expect(input.value).toBe("open-codex/gpt-5.5");

    fireEvent.change(input, { target: { value: "openai-codex/gpt-5.5" } });
    expect(await screen.findByText("“openai-codex/gpt-5.5”")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) =>
        String(input).startsWith("/api/settings/provider-models"),
      ),
    ).toBe(false);
  });

  it("can close the new configuration dialog without trapping the settings page", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/settings") return jsonResponse(settingsPayload());
        if (url === "/api/settings/cli-apps") {
          return jsonResponse({ apps: [], installed_count: 0 });
        }
        if (url === "/api/settings/mcp-presets") {
          return jsonResponse({ presets: [], installed_count: 0 });
        }
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );

    renderSettingsView({ initialSection: "models" });

    const configurationButton = await screen.findByRole("button", { name: "当前配置" });
    fireEvent.pointerDown(configurationButton!);
    fireEvent.click(await screen.findByText("添加配置"));

    expect(await screen.findByRole("heading", { name: "新建模型配置" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "新建模型配置" })).not.toBeInTheDocument(),
    );
    expect(document.body.style.pointerEvents).not.toBe("none");

    fireEvent.pointerDown(configurationButton!);
    expect(await screen.findByText("添加配置")).toBeInTheDocument();
  });

  it("loads provider models and lets users choose one without typing the id manually", async () => {
    const payload: SettingsPayload = {
      ...settingsPayload(),
      agent: {
        ...settingsPayload().agent,
        model: "deepseek-chat",
        provider: "deepseek",
        resolved_provider: "deepseek",
      },
      model_presets: [
        {
          ...settingsPayload().model_presets[0],
          model: "deepseek-chat",
          provider: "deepseek",
        },
      ],
      providers: [
        {
          name: "deepseek",
          label: "DeepSeek",
          configured: true,
          auth_type: "api_key",
          api_key_required: true,
          api_key_hint: "sk-...",
          api_base: "https://api.deepseek.com",
          default_api_base: "https://api.deepseek.com",
        },
      ],
    };
    const updatedPayload: SettingsPayload = {
      ...payload,
      agent: {
        ...payload.agent,
        model: "deepseek-reasoner",
      },
      model_presets: [
        {
          ...payload.model_presets[0],
          model: "deepseek-reasoner",
        },
      ],
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/settings") return jsonResponse(payload);
      if (url === "/api/settings/cli-apps") {
        return jsonResponse({ apps: [], installed_count: 0 });
      }
      if (url === "/api/settings/mcp-presets") {
        return jsonResponse({ presets: [], installed_count: 0 });
      }
      if (url === "/api/settings/provider-models?provider=deepseek") {
        return jsonResponse({
          provider: "deepseek",
          label: "DeepSeek",
          status: "available",
          catalog_kind: "official",
          models: [
            { id: "deepseek-chat", owned_by: "deepseek", context_window: 65536 },
            { id: "deepseek-reasoner", owned_by: "deepseek", context_window: 65536 },
          ],
          model_count: 2,
          fetched_at: 1,
        });
      }
      if (url === "/api/settings/update?model_preset=default&model=deepseek-reasoner") {
        return jsonResponse(updatedPayload);
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSettingsView({ initialSection: "models" });

    const modelButtons = await screen.findAllByRole("button", { name: /deepseek-chat/i });
    fireEvent.pointerDown(modelButtons[modelButtons.length - 1]);
    await screen.findByText("deepseek-reasoner");
    fireEvent.click(screen.getAllByText("deepseek-reasoner")[0]);
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/settings/provider-models?provider=deepseek",
        expect.objectContaining({
          headers: { Authorization: "Bearer tok" },
        }),
      ),
    );
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/settings/update?model_preset=default&model=deepseek-reasoner",
        expect.objectContaining({
          headers: { Authorization: "Bearer tok" },
        }),
      ),
    );
  });

  it("saves network safety without exposing technical SSRF copy", async () => {
    const payload = settingsPayload();
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/settings") return jsonResponse(payload);
      if (url === "/api/settings/cli-apps") {
        return jsonResponse({ apps: [], installed_count: 0 });
      }
      if (url === "/api/settings/mcp-presets") {
        return jsonResponse({ presets: [], installed_count: 0 });
      }
      if (url === "/api/settings/network-safety/update?webui_allow_local_service_access=false&webui_default_access_mode=default&guard_level=standard&cold_storage_days=14&duplicate_similarity_threshold=0.6") {
        return jsonResponse({
          ...payload,
          advanced: { ...payload.advanced, webui_allow_local_service_access: false },
          requires_restart: true,
          restart_required_sections: ["runtime"],
        });
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSettingsView({ initialSection: "advanced" });

    expect(await screen.findByText("WebUI 安全")).toBeInTheDocument();
    expect(
      screen.getByText("控制提示词注入与 shell 命令拦截的防护强度。服务访问与工作区边界始终开启。"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/SSRF/i)).not.toBeInTheDocument();
    expect(screen.queryByText("Private Service Protection")).not.toBeInTheDocument();
    expect(screen.getAllByText("默认权限").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "已限制" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "默认权限" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "完全访问权限" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("switch", { name: "本机服务" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/settings/network-safety/update?webui_allow_local_service_access=false&webui_default_access_mode=default&guard_level=standard&cold_storage_days=14&duplicate_similarity_threshold=0.6",
        expect.objectContaining({
          headers: { Authorization: "Bearer tok" },
        }),
      ),
    );
  });

  it("uses native host safety copy on the native surface", async () => {
    const payload = {
      ...settingsPayload(),
      surface: "native" as const,
      runtime_surface: "native" as const,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/settings") return jsonResponse(payload);
        if (url === "/api/settings/cli-apps") return jsonResponse({ apps: [], installed_count: 0 });
        if (url === "/api/settings/mcp-presets") return jsonResponse({ presets: [], installed_count: 0 });
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );

    renderSettingsView({ initialSection: "advanced" });

    expect(await screen.findByText("应用安全")).toBeInTheDocument();
    expect(screen.queryByText("WebUI 安全")).not.toBeInTheDocument();
    expect(screen.getByText("允许完全访问权限下的 shell 命令访问这台 Mac 上的服务。")).toBeInTheDocument();
  });

  it("refreshes settings with a fresh token after native engine restart", async () => {
    const payload = {
      ...settingsPayload(),
      surface: "native" as const,
      runtime_surface: "native" as const,
      runtime_capabilities: {
        can_restart_engine: true,
        can_pick_folder: true,
        can_open_logs: true,
        can_export_diagnostics: true,
      },
    };
    const restartedPayload = {
      ...payload,
      advanced: { ...payload.advanced, webui_allow_local_service_access: false },
      requires_restart: true,
      restart_required_sections: ["runtime"],
    };
    const refreshedPayload = {
      ...restartedPayload,
      requires_restart: false,
      restart_required_sections: [],
    };
    const restartEngine = vi.fn(async () => "fresh-token");
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const auth = (init?.headers as Record<string, string> | undefined)?.Authorization;
      if (url === "/api/settings" && auth === "Bearer fresh-token") {
        return jsonResponse(refreshedPayload);
      }
      if (url === "/api/settings") return jsonResponse(payload);
      if (url === "/api/settings/cli-apps") return jsonResponse({ apps: [], installed_count: 0 });
      if (url === "/api/settings/mcp-presets") return jsonResponse({ presets: [], installed_count: 0 });
      if (url === "/api/settings/network-safety/update?webui_allow_local_service_access=false&webui_default_access_mode=default&guard_level=standard&cold_storage_days=14&duplicate_similarity_threshold=0.6") {
        return jsonResponse(restartedPayload);
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSettingsView({
      initialSection: "advanced",
      onNativeEngineRestart: restartEngine,
    });

    expect(await screen.findByText("应用安全")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch", { name: "本机服务" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(restartEngine).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/settings",
        expect.objectContaining({
          headers: { Authorization: "Bearer fresh-token" },
        }),
      ),
    );
  });
});

describe("SettingsView 标签合并与二级子区", () => {
  beforeEach(() => {
    // 挂起 fetch：SettingsView 以 initialSettings 渲染，不发起真实网络请求
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("侧边栏收敛为 5 个顶层标签，子区不再出现在侧边栏", () => {
    renderSettingsView({
      initialSection: "overview",
      initialSettings: settingsPayload(),
      showSidebar: true,
    });
    const nav = screen.getByRole("navigation", { name: "设置分区" });
    for (const label of ["概览", "外观", "模型", "网页", "系统"]) {
      expect(within(nav).getByRole("button", { name: label })).toBeInTheDocument();
    }
    for (const gone of ["图片", "视觉", "语音", "系统 IO", "安全"]) {
      expect(within(nav).queryByRole("button", { name: gone })).not.toBeInTheDocument();
    }
  });

  it("模型 tab 二级切换条可在 LLM/文生图/文生视频/视图理解/ASR 间切换", async () => {
    renderSettingsView({ initialSection: "models", initialSettings: settingsPayload() });
    const subtabs = within(screen.getByTestId("settings-subtabs"));
    expect(subtabs.getByRole("button", { name: "LLM" })).toHaveAttribute("aria-current", "true");
    expect(screen.getByText("当前配置")).toBeInTheDocument();

    fireEvent.click(subtabs.getByRole("button", { name: "文生图" }));
    expect(subtabs.getByRole("button", { name: "文生图" })).toHaveAttribute("aria-current", "true");
    expect(await screen.findByRole("switch", { name: "图片生成" })).toBeInTheDocument();

    fireEvent.click(subtabs.getByRole("button", { name: "文生视频" }));
    expect(subtabs.getByRole("button", { name: "文生视频" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(await screen.findByRole("switch", { name: "视频生成" })).toBeInTheDocument();
    expect(screen.getByText("画面比例")).toBeInTheDocument();
    expect(screen.getByText("时长")).toBeInTheDocument();
    expect(screen.getByText("分辨率")).toBeInTheDocument();
    expect(screen.getByText("生成音轨")).toBeInTheDocument();
    expect(screen.getByText("水印")).toBeInTheDocument();
    expect(screen.getByText("保存目录")).toBeInTheDocument();
    expect(screen.getByText("密钥状态")).toBeInTheDocument();

    fireEvent.click(subtabs.getByRole("button", { name: "视图理解" }));
    expect(subtabs.getByRole("button", { name: "视图理解" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(await screen.findByRole("switch", { name: "截图" })).toBeInTheDocument();

    fireEvent.click(subtabs.getByRole("button", { name: "ASR" }));
    expect(subtabs.getByRole("button", { name: "ASR" })).toHaveAttribute("aria-current", "true");
    expect(await screen.findByRole("heading", { name: "语音识别" })).toBeInTheDocument();
  });

  it("image 深链落到模型 tab 的文生图子区，父标签高亮", () => {
    renderSettingsView({
      initialSection: "image",
      initialSettings: settingsPayload(),
      showSidebar: true,
    });
    expect(screen.getByRole("button", { name: "模型" })).toHaveAttribute("aria-current", "page");
    expect(
      within(screen.getByTestId("settings-subtabs")).getByRole("button", { name: "文生图" }),
    ).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("switch", { name: "图片生成" })).toBeInTheDocument();
  });

  it("voice 深链落到模型 tab 的 ASR 子区", () => {
    renderSettingsView({
      initialSection: "voice",
      initialSettings: settingsPayload(),
      showSidebar: true,
    });
    expect(screen.getByRole("button", { name: "模型" })).toHaveAttribute("aria-current", "page");
    expect(
      within(screen.getByTestId("settings-subtabs")).getByRole("button", { name: "ASR" }),
    ).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("heading", { name: "语音识别" })).toBeInTheDocument();
  });

  it("文生视频子区渲染真面板并保存设置，返回视频重启桶", async () => {
    const payload: SettingsPayload = {
      ...settingsPayload(),
      video_generation: {
        ...settingsPayload().video_generation,
        api_key_configured: true,
      },
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/settings") return jsonResponse(payload);
      if (url === "/api/settings/cli-apps") return jsonResponse({ apps: [], installed_count: 0 });
      if (url === "/api/settings/mcp-presets") return jsonResponse({ presets: [], installed_count: 0 });
      if (url.startsWith("/api/settings/video-generation/update")) {
        return jsonResponse({
          ...payload,
          video_generation: { ...payload.video_generation, enabled: true },
          requires_restart: true,
          restart_required_sections: ["video"],
        });
      }
      return { ok: false, status: 404, json: async () => ({}) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSettingsView({ initialSection: "models", initialSettings: payload });

    const subtabs = within(screen.getByTestId("settings-subtabs"));
    fireEvent.click(subtabs.getByRole("button", { name: "文生视频" }));

    expect(await screen.findByRole("switch", { name: "视频生成" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("doubao-seedance")).toBeInTheDocument();
    expect(screen.getByText("16:9")).toBeInTheDocument();
    expect(screen.getByText("generated/videos")).toBeInTheDocument();
    // 视频面板不含密钥/地址编辑字段
    expect(screen.queryByPlaceholderText(/apiKey|api key|留空则保留当前 key/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("switch", { name: "视频生成" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/settings/video-generation/update"),
        expect.objectContaining({
          headers: { Authorization: "Bearer tok" },
        }),
      ),
    );
    expect(await screen.findByText("已保存。准备好后重启。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /重启/ })).toBeInTheDocument();
  });

  it("LLM 子区提供商列表只读：无编辑/保存入口，展开仅展示密钥状态", async () => {
    const payload: SettingsPayload = {
      ...settingsPayload(),
      agent: {
        ...settingsPayload().agent,
        provider: "openai",
        resolved_provider: "openai",
      },
      model_presets: [
        {
          ...settingsPayload().model_presets[0],
          model: "openai/gpt-4o",
          provider: "openai",
        },
      ],
      providers: [
        {
          name: "openai",
          label: "OpenAI",
          configured: true,
          auth_type: "api_key",
          api_key_required: true,
          api_key_hint: "sk-o••••hint",
          api_base: null,
          default_api_base: "https://api.openai.com/v1",
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/settings") return jsonResponse(payload);
        if (url === "/api/settings/cli-apps") return jsonResponse({ apps: [], installed_count: 0 });
        if (url === "/api/settings/mcp-presets") return jsonResponse({ presets: [], installed_count: 0 });
        return { ok: false, status: 404, json: async () => ({}) } as Response;
      }),
    );

    renderSettingsView({ initialSection: "models", initialSettings: payload });

    // 提供商行按钮以其地址为副标题，可作为唯一定位点
    const providerCaption = await screen.findByText("https://api.openai.com/v1");
    expect(providerCaption).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存提供商" })).not.toBeInTheDocument();

    fireEvent.click(providerCaption);
    expect(screen.getByText("sk-o••••hint")).toBeInTheDocument();
    expect(screen.getByText(/此处仅展示状态/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存提供商" })).not.toBeInTheDocument();
  });

  it("系统 tab 二级切换条可在 运行/系统IO/安全 间切换", async () => {
    renderSettingsView({ initialSection: "runtime", initialSettings: settingsPayload() });
    const subtabs = within(screen.getByTestId("settings-subtabs"));
    expect(subtabs.getByRole("button", { name: "运行" })).toHaveAttribute("aria-current", "true");
    expect(screen.getByText("Bot 名称")).toBeInTheDocument();

    fireEvent.click(subtabs.getByRole("button", { name: "系统 IO" }));
    expect(subtabs.getByRole("button", { name: "系统 IO" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(await screen.findByRole("switch", { name: "系统 IO" })).toBeInTheDocument();

    fireEvent.click(subtabs.getByRole("button", { name: "安全" }));
    expect(subtabs.getByRole("button", { name: "安全" })).toHaveAttribute("aria-current", "true");
    expect(await screen.findByText("WebUI 安全")).toBeInTheDocument();
  });

  it("systemIo 深链落到系统 tab 的 系统IO 子区，父标签高亮", () => {
    renderSettingsView({
      initialSection: "systemIo",
      initialSettings: settingsPayload(),
      showSidebar: true,
    });
    expect(screen.getByRole("button", { name: "系统" })).toHaveAttribute("aria-current", "page");
    expect(
      within(screen.getByTestId("settings-subtabs")).getByRole("button", { name: "系统 IO" }),
    ).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("switch", { name: "系统 IO" })).toBeInTheDocument();
  });
});
