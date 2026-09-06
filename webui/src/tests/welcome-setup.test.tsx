import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WelcomeSetup, hasSkippedSetup } from "@/components/setup/WelcomeSetup";
import type { SettingsPayload } from "@/lib/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchSettings: vi.fn(),
    completeSetup: vi.fn(),
  };
});

import { ApiError, completeSetup, fetchSettings } from "@/lib/api";

function settingsPayload(overrides: Partial<SettingsPayload> = {}): SettingsPayload {
  return {
    surface: "browser",
    runtime_surface: "browser",
    agent: {
      model: "deepseek/deepseek-chat",
      provider: "deepseek",
      resolved_provider: null,
      has_api_key: false,
      model_preset: null,
      max_tokens: 8192,
      context_window_tokens: 65536,
      temperature: 1.0,
      reasoning_effort: null,
      timezone: "Asia/Shanghai",
      bot_name: "biscuitbot",
      vision_model: null,
      vision_model_override: null,
      vision_model_configured: false,
    },
    providers: [
      {
        name: "deepseek",
        label: "DeepSeek",
        configured: false,
        api_key_required: true,
        api_key_hint: null,
        api_base: null,
        default_api_base: "https://api.deepseek.com",
        model_selectable: true,
      },
      {
        name: "openai",
        label: "OpenAI",
        configured: false,
        api_key_required: true,
        api_key_hint: null,
        api_base: null,
        default_api_base: "https://api.openai.com/v1",
        model_selectable: true,
      },
      {
        name: "anthropic",
        label: "Anthropic",
        configured: false,
        auth_type: "oauth",
        api_key_required: true,
        api_key_hint: null,
        api_base: null,
        default_api_base: "https://api.anthropic.com",
        model_selectable: true,
      },
    ],
    ...overrides,
  } as SettingsPayload;
}

describe("WelcomeSetup", () => {
  beforeEach(() => {
    vi.mocked(fetchSettings).mockReset();
    vi.mocked(completeSetup).mockReset();
    window.localStorage.removeItem("biscuitbot-webui.setup-skipped");
  });

  it("renders the provider list and hides oauth-only providers", async () => {
    vi.mocked(fetchSettings).mockResolvedValue(settingsPayload());
    render(<WelcomeSetup token="tok" onDone={vi.fn()} />);

    expect(await screen.findByText("DeepSeek")).toBeInTheDocument();
    expect(screen.getByText("OpenAI")).toBeInTheDocument();
    // OAuth provider is filtered out of the first-run provider list.
    expect(screen.queryByText("Anthropic")).not.toBeInTheDocument();
  });

  it("selects a provider and shows the API key field", async () => {
    vi.mocked(fetchSettings).mockResolvedValue(settingsPayload());
    render(<WelcomeSetup token="tok" onDone={vi.fn()} />);

    fireEvent.click(await screen.findByText("DeepSeek"));
    expect(screen.getByPlaceholderText("粘贴你的 API 密钥")).toBeInTheDocument();
    // Recommended model is pre-filled for the chosen provider.
    expect(
      (screen.getByPlaceholderText("留空则使用默认模型") as HTMLInputElement).value,
    ).toBe("deepseek/deepseek-v4-flash");
  });

  it("keeps the submit button disabled until a key is entered", async () => {
    vi.mocked(fetchSettings).mockResolvedValue(settingsPayload());
    render(<WelcomeSetup token="tok" onDone={vi.fn()} />);

    fireEvent.click(await screen.findByText("OpenAI"));
    const submit = screen.getByRole("button", { name: "开始使用" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    fireEvent.change(screen.getByPlaceholderText("粘贴你的 API 密钥"), {
      target: { value: "sk-test" },
    });
    expect(submit.disabled).toBe(false);
  });

  it("submits the provider + key and calls onDone", async () => {
    vi.mocked(fetchSettings).mockResolvedValue(settingsPayload());
    vi.mocked(completeSetup).mockResolvedValue({ ok: true, needs_setup: false });
    const onDone = vi.fn();
    render(<WelcomeSetup token="tok" onDone={onDone} />);

    fireEvent.click(await screen.findByText("OpenAI"));
    fireEvent.change(screen.getByPlaceholderText("粘贴你的 API 密钥"), {
      target: { value: "sk-test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始使用" }));

    await waitFor(() => {
      expect(completeSetup).toHaveBeenCalledWith("tok", {
        provider: "openai",
        apiKey: "sk-test",
        model: "openai/gpt-5.6-terra",
      });
    });
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
  });

  it("内置厂商不展示 API 地址输入框（地址自动管理）", async () => {
    vi.mocked(fetchSettings).mockResolvedValue(settingsPayload());
    render(<WelcomeSetup token="tok" onDone={vi.fn()} />);

    fireEvent.click(await screen.findByText("DeepSeek"));
    expect(screen.getByPlaceholderText("粘贴你的 API 密钥")).toBeInTheDocument();
    expect(screen.queryByText("API 地址")).not.toBeInTheDocument();
  });

  it("无默认地址的网关厂商要求填写 API 地址", async () => {
    const payload = settingsPayload({
      providers: [
        {
          name: "newapi",
          label: "New API 中转",
          configured: false,
          api_key_required: true,
          api_key_hint: null,
          api_base: null,
          default_api_base: null,
          model_selectable: true,
        },
      ],
    } as Partial<SettingsPayload>);
    vi.mocked(fetchSettings).mockResolvedValue(payload);
    render(<WelcomeSetup token="tok" onDone={vi.fn()} />);

    fireEvent.click(await screen.findByText("New API 中转"));
    expect(screen.getByText("API 地址")).toBeInTheDocument();

    const submit = screen.getByRole("button", { name: "开始使用" }) as HTMLButtonElement;
    fireEvent.change(screen.getByPlaceholderText("粘贴你的 API 密钥"), {
      target: { value: "sk-test" },
    });
    expect(submit.disabled).toBe(true);

    fireEvent.change(screen.getByPlaceholderText("https://…"), {
      target: { value: "https://gw.example.com/v1" },
    });
    expect(submit.disabled).toBe(false);

    vi.mocked(completeSetup).mockResolvedValue({ ok: true, needs_setup: false });
    fireEvent.click(submit);
    await waitFor(() => {
      expect(completeSetup).toHaveBeenCalledWith("tok", {
        provider: "newapi",
        apiKey: "sk-test",
        apiBase: "https://gw.example.com/v1",
      });
    });
  });

  it("retries with a fresh token when submit gets a 401", async () => {
    vi.mocked(fetchSettings).mockResolvedValue(settingsPayload());
    vi.mocked(completeSetup)
      .mockRejectedValueOnce(new ApiError(401, "Unauthorized"))
      .mockResolvedValueOnce({ ok: true, needs_setup: false });
    const onRefreshToken = vi.fn().mockResolvedValue("fresh-tok");
    const onDone = vi.fn();
    render(
      <WelcomeSetup token="tok" onDone={onDone} onRefreshToken={onRefreshToken} />,
    );

    fireEvent.click(await screen.findByText("OpenAI"));
    fireEvent.change(screen.getByPlaceholderText("粘贴你的 API 密钥"), {
      target: { value: "sk-test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "开始使用" }));

    await waitFor(() => {
      expect(onRefreshToken).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(completeSetup).toHaveBeenCalledWith("fresh-tok", {
        provider: "openai",
        apiKey: "sk-test",
        model: "openai/gpt-5.6-terra",
      });
    });
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("skips setup by persisting the skip flag and calling onDone", async () => {
    vi.mocked(fetchSettings).mockResolvedValue(settingsPayload());
    const onDone = vi.fn();
    render(<WelcomeSetup token="tok" onDone={onDone} />);

    fireEvent.click(await screen.findByText("稍后在设置中配置"));
    expect(hasSkippedSetup()).toBe(true);
    expect(onDone).toHaveBeenCalledTimes(1);
  });
});
