import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CapabilitiesView } from "@/components/settings/CapabilitiesView";
import { ClientProvider } from "@/providers/ClientProvider";
import type { ColdStoragePayload } from "@/lib/types";

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: (name: string) => (name === "content-type" ? "application/json" : null) },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function errorResponse(status: number, body: string): Response {
  return {
    ok: false,
    status,
    headers: { get: () => null },
    json: async () => ({ detail: body }),
    text: async () => body,
  } as unknown as Response;
}

const COLD: ColdStoragePayload = {
  cold_count: 2,
  entries: [
    {
      name: "web_search_legacy",
      capability: "旧版网页搜索，已被 web_search 取代",
      usage_md: "docs/web_search.md",
      source_file: "xianaibot.agent.tools.legacy_search",
      cold_since: 1_700_000_000,
      cold_days: 21,
    },
    {
      name: "pdf_ocr",
      capability: "扫描件 OCR",
      usage_md: "",
      source_file: "",
      cold_since: 1_700_800_000,
      cold_days: 2,
    },
  ],
};

const EMPTY: ColdStoragePayload = { cold_count: 0, entries: [] };

const CAPABILITIES = { capabilities: [], installed_count: 0 };

/** 按 URL 路由的 fetch 桩：能力目录列表 + 冷门仓库。 */
function stubFetch(coldStorage: unknown = COLD) {
  const fetchMock = vi.fn(async (url: string) => {
    if (url.includes("/api/webui/cold-storage")) {
      return typeof coldStorage === "function"
        ? (coldStorage as () => Response)()
        : jsonResponse(coldStorage);
    }
    if (url.includes("/api/webui/capabilities")) return jsonResponse(CAPABILITIES);
    throw new Error(`unexpected fetch: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderCapabilities() {
  return render(
    <ClientProvider client={{} as never} token="tok">
      <CapabilitiesView />
    </ClientProvider>,
  );
}

describe("CapabilitiesView 冷门仓库", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("点击按钮后才请求冷门仓库并展示工具列表", async () => {
    const fetchMock = stubFetch();
    renderCapabilities();

    // 能力目录先加载完，期间不该顺带请求冷门仓库
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/api/webui/capabilities"))).toBe(
        true,
      ),
    );
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/api/webui/cold-storage"))).toBe(
      false,
    );
    expect(screen.queryByTestId("cold-storage-sheet")).toBeNull();

    fireEvent.click(screen.getByTestId("cold-storage-open"));

    expect(await screen.findByTestId("cold-storage-sheet")).toBeTruthy();
    expect(screen.getByText("2 个工具在冷门仓库")).toBeTruthy();
    const rows = await screen.findAllByTestId("cold-storage-row");
    expect(rows).toHaveLength(2);
    expect(screen.getByText("web_search_legacy")).toBeTruthy();
    expect(screen.getByText("旧版网页搜索，已被 web_search 取代")).toBeTruthy();
    expect(screen.getByText("已冷落 21 天")).toBeTruthy();
    expect(screen.getByText("xianaibot.agent.tools.legacy_search")).toBeTruthy();
    expect(screen.getByText("pdf_ocr")).toBeTruthy();
    expect(screen.getByText("已冷落 2 天")).toBeTruthy();
  });

  it("冷门仓库为空时展示空状态", async () => {
    stubFetch(EMPTY);
    renderCapabilities();

    fireEvent.click(screen.getByTestId("cold-storage-open"));

    expect(await screen.findByText("冷门仓库为空。所有工具都在活跃索引中。")).toBeTruthy();
    expect(screen.queryByTestId("cold-storage-row")).toBeNull();
  });

  it("请求失败时展示错误文案且不白屏", async () => {
    stubFetch(() => errorResponse(500, "boom"));
    renderCapabilities();

    fireEvent.click(screen.getByTestId("cold-storage-open"));

    expect(await screen.findByText("冷门仓库加载失败。")).toBeTruthy();
    expect(screen.getByTestId("cold-storage-sheet")).toBeTruthy();
  });
});
