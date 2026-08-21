import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TalentMarketView } from "@/components/settings/TalentMarketView";
import { ClientProvider } from "@/providers/ClientProvider";
import type { Employee, TalentCatalogPayload } from "@/lib/types";

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: (name: string) => (name === "content-type" ? "application/json" : null) },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const CATALOG_URL = "https://example.com/employees.json";

const CATALOG: TalentCatalogPayload = {
  configured: true,
  source_url: CATALOG_URL,
  catalog_updated_at: "2026-08-13",
  employees: [
    {
      id: "copywriter",
      name: "文案专员",
      avatar: "✍️",
      description: "面向营销文案",
      system_prompt: "你是一名文案专员数字人员工。",
      skills: ["web_search", "browser"],
      category: "marketing",
      installed: false,
      source_url: CATALOG_URL,
    },
    {
      id: "editor",
      name: "剪辑助理",
      avatar: "🎬",
      description: "视频剪辑",
      system_prompt: "",
      skills: [],
      installed: false,
      source_url: CATALOG_URL,
    },
  ],
  installed_count: 0,
  sources: [
    {
      source_url: CATALOG_URL,
      catalog_updated_at: "2026-08-13",
      installed_count: 0,
    },
  ],
};

const UNCONFIGURED: TalentCatalogPayload = {
  configured: false,
  source_url: "",
  catalog_updated_at: null,
  employees: [],
  installed_count: 0,
  sources: [],
};

function renderMarket(overrides: {
  employees?: Employee[];
  onInstalled?: () => void;
  onBackToChat?: () => void;
} = {}) {
  return {
    onInstalled: overrides.onInstalled ?? vi.fn(),
    onBackToChat: overrides.onBackToChat ?? vi.fn(),
    ...render(
      <ClientProvider client={{} as never} token="tok">
        <TalentMarketView
          employees={overrides.employees ?? []}
          onInstalled={overrides.onInstalled ?? vi.fn()}
          onBackToChat={overrides.onBackToChat ?? vi.fn()}
        />
      </ClientProvider>,
    ),
  };
}

/** 目录在挂载时自动加载，无需输入 URL。 */
async function awaitCatalogLoaded() {
  await waitFor(() => expect(screen.getByText("文案专员")).toBeInTheDocument());
}

describe("TalentMarketView", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("auto-loads the configured registry catalog on mount", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(CATALOG));
    vi.stubGlobal("fetch", fetchMock);

    renderMarket();
    await awaitCatalogLoaded();

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/webui/talent-market/catalog?");
    expect(url).not.toContain("url=");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok");
    expect(screen.getByText("文案专员")).toBeInTheDocument();
    expect(screen.getByText("面向营销文案")).toBeInTheDocument();
    expect(screen.getByText("web_search")).toBeInTheDocument();
    // 无输入框/加载目录按钮，只展示目录 + 自动刷新提示
    expect(screen.queryByPlaceholderText("https://example.com/employees.json")).not.toBeInTheDocument();
    expect(screen.queryByText("加载目录")).not.toBeInTheDocument();
    expect(screen.getByText(/共 2 名员工/)).toBeInTheDocument();
  });

  it("auto-refreshes the catalog every 30 minutes", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => jsonResponse(CATALOG));
    vi.stubGlobal("fetch", fetchMock);

    renderMarket();
    // 冲刷微任务，等待挂载时的首次加载完成
    await act(async () => {
      for (let i = 0; i < 5; i += 1) await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // 前进 30 分钟 → 触发一次静默自动刷新
    await act(async () => {
      vi.advanceTimersByTime(30 * 60 * 1000);
    });
    await act(async () => {
      for (let i = 0; i < 5; i += 1) await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("installs a talent employee and reports back", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(CATALOG))
      .mockResolvedValueOnce(
        jsonResponse({
          id: "copywriter",
          name: "文案专员",
          avatar: "✍️",
          system_prompt: "你是一名文案专员数字人员工。",
          skills: ["web_search"],
          enabled: true,
          created_at: "2026-08-13T00:00:00Z",
          already_existed: false,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const onInstalled = vi.fn();
    renderMarket({ onInstalled });
    await awaitCatalogLoaded();

    const copywriterRow = screen
      .getByText("文案专员")
      .closest("article") as HTMLElement;
    fireEvent.click(within(copywriterRow).getByText("下载/添加"));
    await waitFor(() => expect(onInstalled).toHaveBeenCalledTimes(1));

    const installCall = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(installCall[0]).toContain("/api/webui/talent-market/install");
    expect(installCall[0]).toContain(`source_url=${encodeURIComponent(CATALOG_URL)}`);
    const header = (installCall[1].headers as Record<string, string>)[
      "X-Biscuitbot-Employee-Values"
    ];
    expect(JSON.parse(decodeURIComponent(header))).toEqual(
      expect.objectContaining({
        id: "copywriter",
        name: "文案专员",
        skills: ["web_search", "browser"],
      }),
    );
    expect(await screen.findByText("已添加")).toBeInTheDocument();
  });

  it("shows a per-entry source host badge and a source count for multiple catalogs", async () => {
    const multiSource: TalentCatalogPayload = {
      configured: true,
      source_url: CATALOG_URL,
      catalog_updated_at: "2026-08-13",
      employees: [
        {
          id: "copywriter",
          name: "文案专员",
          avatar: "✍️",
          description: "面向营销文案",
          system_prompt: "你是一名文案专员数字人员工。",
          skills: ["web_search", "browser"],
          category: "marketing",
          installed: false,
          source_url: "https://alpha.example/employees.json",
        },
        {
          id: "editor",
          name: "剪辑助理",
          avatar: "🎬",
          description: "视频剪辑",
          system_prompt: "",
          skills: [],
          installed: false,
          source_url: "https://beta.example/employees.json",
        },
      ],
      installed_count: 0,
      sources: [
        {
          source_url: "https://alpha.example/employees.json",
          catalog_updated_at: "2026-08-13",
          installed_count: 0,
        },
        {
          source_url: "https://beta.example/employees.json",
          catalog_updated_at: "2026-08-13",
          installed_count: 0,
        },
      ],
    };
    const fetchMock = vi.fn(async () => jsonResponse(multiSource));
    vi.stubGlobal("fetch", fetchMock);

    renderMarket();
    await awaitCatalogLoaded();

    // 每个条目按其所属来源显示 host 徽标
    expect(screen.getByText("alpha.example")).toBeInTheDocument();
    expect(screen.getByText("beta.example")).toBeInTheDocument();
    // 多个来源时 summary 显示来源数
    expect(screen.getByText(/2 个来源/)).toBeInTheDocument();
  });

  it("installs each entry using its own source_url when sources differ", async () => {
    const multiSource: TalentCatalogPayload = {
      configured: true,
      source_url: "https://alpha.example/employees.json",
      catalog_updated_at: "2026-08-13",
      employees: [
        {
          id: "copywriter",
          name: "文案专员",
          avatar: "✍️",
          description: "面向营销文案",
          system_prompt: "你是一名文案专员数字人员工。",
          skills: ["web_search", "browser"],
          category: "marketing",
          installed: false,
          source_url: "https://alpha.example/employees.json",
        },
        {
          id: "editor",
          name: "剪辑助理",
          avatar: "🎬",
          description: "视频剪辑",
          system_prompt: "你是一名剪辑助理数字人员工。",
          skills: ["ffmpeg"],
          installed: false,
          source_url: "https://beta.example/employees.json",
        },
      ],
      installed_count: 0,
      sources: [
        {
          source_url: "https://alpha.example/employees.json",
          catalog_updated_at: "2026-08-13",
          installed_count: 0,
        },
        {
          source_url: "https://beta.example/employees.json",
          catalog_updated_at: "2026-08-13",
          installed_count: 0,
        },
      ],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(multiSource))
      .mockResolvedValueOnce(
        jsonResponse({
          id: "copywriter",
          name: "文案专员",
          avatar: "✍️",
          system_prompt: "你是一名文案专员数字人员工。",
          skills: ["web_search"],
          enabled: true,
          created_at: "2026-08-13T00:00:00Z",
          already_existed: false,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          id: "editor",
          name: "剪辑助理",
          avatar: "🎬",
          system_prompt: "你是一名剪辑助理数字人员工。",
          skills: ["ffmpeg"],
          enabled: true,
          created_at: "2026-08-13T00:00:00Z",
          already_existed: false,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const onInstalled = vi.fn();
    renderMarket({ onInstalled });
    await awaitCatalogLoaded();

    // 安装文案专员 → 传其所属的 alpha 来源 URL
    const copywriterRow = screen
      .getByText("文案专员")
      .closest("article") as HTMLElement;
    fireEvent.click(within(copywriterRow).getByText("下载/添加"));
    await waitFor(() => expect(onInstalled).toHaveBeenCalledTimes(1));
    const alphaCall = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(alphaCall[0]).toContain(
      `source_url=${encodeURIComponent("https://alpha.example/employees.json")}`,
    );

    // 安装剪辑助理 → 传其所属的 beta 来源 URL
    const editorRow = screen
      .getByText("剪辑助理")
      .closest("article") as HTMLElement;
    fireEvent.click(within(editorRow).getByText("下载/添加"));
    await waitFor(() => expect(onInstalled).toHaveBeenCalledTimes(2));
    const betaCall = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(betaCall[0]).toContain(
      `source_url=${encodeURIComponent("https://beta.example/employees.json")}`,
    );
  });

  it("marks employees already present as installed", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(CATALOG));
    vi.stubGlobal("fetch", fetchMock);

    const installedEmployee: Employee = {
      id: "copywriter",
      name: "文案专员",
      system_prompt: "你是一名文案专员数字人员工。",
      skills: ["web_search"],
      enabled: true,
      created_at: "2026-08-13T00:00:00Z",
    };
    renderMarket({ employees: [installedEmployee] });
    await awaitCatalogLoaded();

    expect(await screen.findByText("已添加")).toBeInTheDocument();
  });

  it("disables install for entries without a persona", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(CATALOG));
    vi.stubGlobal("fetch", fetchMock);

    renderMarket();
    await awaitCatalogLoaded();

    // 剪辑助理 缺 system_prompt → 下载/添加 按钮禁用
    const editorRow = screen
      .getByText("剪辑助理")
      .closest("article") as HTMLElement;
    const installButton = Array.from(
      editorRow.querySelectorAll("button"),
    ).find((button) => button.textContent?.includes("下载/添加"));
    expect(installButton).toBeDefined();
    expect(installButton).toBeDisabled();
  });

  it("shows CLI guidance when the registry is not configured", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(UNCONFIGURED));
    vi.stubGlobal("fetch", fetchMock);

    renderMarket();
    await waitFor(() => expect(screen.getByText("人才市场尚未配置")).toBeInTheDocument());

    expect(screen.getByText(/biscuitbot talent-market set/)).toBeInTheDocument();
    expect(screen.queryByText("文案专员")).not.toBeInTheDocument();
  });
});
