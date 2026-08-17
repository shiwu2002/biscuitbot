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
        initialSection={options.initialSection ?? "overview"}
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

  it("模型厂商子区可编辑：展开展示 API 地址/密钥表单与保存入口", async () => {
    const payload: SettingsPayload = {
      ...settingsPayload(),
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

    renderSettingsView({ initialSection: "providers", initialSettings: payload });

    // 提供商行按钮以其地址为副标题，可作为唯一定位点
    const providerCaption = await screen.findByText("https://api.openai.com/v1");
    expect(providerCaption).toBeInTheDocument();

    fireEvent.click(providerCaption);
    expect(await screen.findByText("API 地址")).toBeInTheDocument();
    expect(screen.getByText("API Key")).toBeInTheDocument();
    expect(screen.getByDisplayValue("https://api.openai.com/v1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存" })).toBeInTheDocument();
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
