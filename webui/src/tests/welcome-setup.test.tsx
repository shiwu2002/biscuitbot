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

import { completeSetup, fetchSettings } from "@/lib/api";

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
        apiBase: "https://api.openai.com/v1",
        model: "openai/gpt-5.6-terra",
      });
    });
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
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
