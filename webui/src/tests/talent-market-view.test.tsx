import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
    },
    {
      id: "editor",
      name: "剪辑助理",
      avatar: "🎬",
      description: "视频剪辑",
      system_prompt: "",
      skills: [],
      installed: false,
    },
  ],
  installed_count: 0,
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

async function loadCatalog() {
  fireEvent.change(
    screen.getByPlaceholderText("https://example.com/employees.json"),
    { target: { value: CATALOG_URL } },
  );
  fireEvent.click(screen.getByText("加载目录"));
  await waitFor(() => expect(screen.getByText("文案专员")).toBeInTheDocument());
}

describe("TalentMarketView", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads the catalog from the entered registry URL", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(CATALOG));
    vi.stubGlobal("fetch", fetchMock);

    renderMarket();
    await loadCatalog();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      "/api/webui/talent-market/catalog?url=https%3A%2F%2Fexample.com%2Femployees.json",
    );
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok");
    expect(screen.getByText("文案专员")).toBeInTheDocument();
    expect(screen.getByText("面向营销文案")).toBeInTheDocument();
    expect(screen.getByText("web_search")).toBeInTheDocument();
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
    await loadCatalog();

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
    await loadCatalog();

    expect(await screen.findByText("已添加")).toBeInTheDocument();
  });

  it("disables install for entries without a persona", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(CATALOG));
    vi.stubGlobal("fetch", fetchMock);

    renderMarket();
    await loadCatalog();

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
});
