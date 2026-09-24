import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SkillHubView } from "@/components/settings/SkillHubView";
import { ClientProvider } from "@/providers/ClientProvider";
import type {
  SkillHubCatalogPayload,
  SkillHubInstalledPayload,
  SkillHubSkill,
  SkillHubStatus,
} from "@/lib/types";

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

const STATUS: SkillHubStatus = {
  available: true,
  enabled: true,
  cli_path: "/home/u/.skillhub/skills_store_cli.py",
  version: "1.4.0",
  skills_dir: "/home/u/.xianaibot/workspace/skills",
  reason: "",
};

const UNAVAILABLE_STATUS: SkillHubStatus = {
  ...STATUS,
  available: false,
  cli_path: "",
  version: "",
  reason: "未检测到 SkillHub CLI（~/.skillhub/skills_store_cli.py）",
};

const CALENDAR: SkillHubSkill = {
  slug: "@clawhub_x/calendar",
  canonical_name: "@clawhub_x/calendar",
  name: "日历助手",
  description: "读取与创建日程",
  version: "1.2.0",
  category: "productivity",
  icon_url: "",
  homepage: "https://example.com/calendar",
  handle: "clawhub_x",
  namespace: "ClawHub 精选",
  downloads: 120,
  stars: 8,
  verified: true,
  source: "clawhub",
};

const WEATHER: SkillHubSkill = {
  slug: "@acme/weather",
  canonical_name: "@acme/weather",
  name: "天气查询",
  description: "查询实时天气",
  version: "0.3.1",
  category: "life",
  icon_url: "",
  homepage: "",
  handle: "acme",
  namespace: "Acme",
  downloads: 3,
  stars: 0,
  verified: false,
  source: "clawhub",
};

const CATALOG: SkillHubCatalogPayload = {
  available: true,
  enabled: true,
  ranking_type: "hot",
  skills: [CALENDAR, WEATHER],
  total: 2,
};

const INSTALLED_EMPTY: SkillHubInstalledPayload = { skills: [], total: 0 };

/** 安装成功后的锁文件快照（用于断言「已安装」徽标不会被随后的刷新冲掉）。 */
const INSTALLED_WITH_CALENDAR: SkillHubInstalledPayload = {
  skills: [
    {
      canonical_name: "@clawhub_x/calendar",
      slug: "calendar",
      handle: "clawhub_x",
      name: "日历助手",
      version: "1.2.0",
      source: "clawhub",
      install_dir: "/skills/calendar",
    },
  ],
  total: 1,
};

const ROUTES: Array<[string, unknown]> = [
  ["/api/webui/skill-hub/status", STATUS],
  ["/api/webui/skill-hub/installed", INSTALLED_EMPTY],
  ["/api/webui/skill-hub/catalog", CATALOG],
  ["/api/webui/skill-hub/search", { available: true, query: "天气", skills: [WEATHER], total: 1 }],
  [
    "/api/webui/skill-hub/updates",
    {
      checked: 2,
      upgradable: 1,
      skipped: 1,
      failed: 0,
      details: ["[skip] @acme/weather 无更新清单"],
      summary: "upgrade done: checked=2 upgradable=1 skipped=1 failed=0",
    },
  ],
  [
    "/api/webui/skill-hub/install",
    { installed: true, slug: "calendar", namespace: "clawhub_x", output: "ok" },
  ],
];

/**
 * 按 URL 路由的 fetch 桩。
 *
 * 长路径优先匹配：``/skill-hub/installed`` 也包含 ``/skill-hub/install`` 前缀，
 * 若短路径先匹配，锁文件接口会被安装接口吞掉。
 */
function stubFetch(overrides: Array<[string, unknown]> = []) {
  const table = [...overrides, ...ROUTES].sort((a, b) => b[0].length - a[0].length);
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    for (const [needle, body] of table) {
      if (!url.includes(needle)) continue;
      if (typeof body === "function") {
        return (body as (u: string, i?: RequestInit) => Response)(url, init);
      }
      return jsonResponse(body);
    }
    throw new Error(`unexpected fetch: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderSkillHub(
  overrides: { onInstalled?: () => void; onBackToCapabilities?: () => void } = {},
) {
  return {
    onInstalled: overrides.onInstalled ?? vi.fn(),
    onBackToCapabilities: overrides.onBackToCapabilities ?? vi.fn(),
    ...render(
      <ClientProvider client={{} as never} token="tok">
        <SkillHubView
          onInstalled={overrides.onInstalled ?? vi.fn()}
          onBackToCapabilities={overrides.onBackToCapabilities ?? vi.fn()}
        />
      </ClientProvider>,
    ),
  };
}

async function awaitCatalogLoaded() {
  await waitFor(() =>
    expect(screen.getAllByText("日历助手").length).toBeGreaterThan(0),
  );
}

/** 排行榜里的技能卡片行（同名技能可能同时出现在「已安装」区域）。 */
function skillRow(name: string): HTMLElement {
  const row = screen
    .getAllByText(name)
    .map((el) => el.closest("article"))
    .find((el): el is HTMLElement => el !== null);
  if (!row) throw new Error(`找不到技能卡片：${name}`);
  return row;
}

/** 安装请求的 URL（排除同样以 ``/install`` 为前缀的 ``/installed``）。 */
function isInstallCall(url: unknown): boolean {
  return String(url).endsWith("/api/webui/skill-hub/install");
}

describe("SkillHubView", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("keeps a back-to-capabilities button at every width", async () => {
    stubFetch([["/api/webui/skill-hub/installed", INSTALLED_WITH_CALENDAR]]);
    const onBackToCapabilities = vi.fn();

    renderSkillHub({ onBackToCapabilities });
    const back = await screen.findByTestId("skill-hub-back");

    expect(back).toHaveTextContent("返回能力目录");
    // jsdom 不套用 CSS，只能这样守住回归：按钮曾经带 lg:hidden，桌面宽度下整块消失。
    expect(back.className).not.toContain("lg:hidden");

    fireEvent.click(back);
    expect(onBackToCapabilities).toHaveBeenCalledTimes(1);
  });

  it("renders the CLI install guide when the store is unavailable", async () => {
    const fetchMock = stubFetch([
      ["/api/webui/skill-hub/status", UNAVAILABLE_STATUS],
      [
        "/api/webui/skill-hub/catalog",
        { available: false, reason: UNAVAILABLE_STATUS.reason, skills: [], total: 0 },
      ],
    ]);

    renderSkillHub();
    await waitFor(() => expect(screen.getByText("技能商店尚不可用")).toBeInTheDocument());

    // 空态如实展示后端给的 reason，并给出官方安装命令（不是错误态）
    expect(screen.getAllByText(/未检测到 SkillHub CLI/).length).toBeGreaterThan(0);
    expect(screen.getByText(/install\.sh \| bash/)).toBeInTheDocument();
    expect(screen.queryByText("日历助手")).not.toBeInTheDocument();
    expect(
      screen.queryByText("加载技能商店失败，请检查网络与 SkillHub CLI 安装情况"),
    ).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.length).toBeGreaterThan(0);
  });

  it("renders the ranking list on mount and lists installed skills", async () => {
    stubFetch([["/api/webui/skill-hub/installed", INSTALLED_WITH_CALENDAR]]);

    renderSkillHub();
    await awaitCatalogLoaded();

    expect(screen.getByRole("heading", { name: "技能商店" })).toBeInTheDocument();
    // 商店只展示尚未安装的技能：未装的「天气查询」在列表，已装的日历助手被过滤
    expect(screen.getByText("天气查询")).toBeInTheDocument();
    expect(screen.getByText("查询实时天气")).toBeInTheDocument();
    expect(screen.queryByText("读取与创建日程")).not.toBeInTheDocument();
    // 分类 / 命名空间 / 来源 chip 与版本
    expect(screen.getByText("life")).toBeInTheDocument();
    // 命名空间 chip 用 handle（与 slug 前缀一致），展示名挂在 title 上
    expect(screen.getByText("acme")).toHaveAttribute("title", "Acme");
    const row = skillRow("天气查询");
    expect(within(row).getByText("clawhub")).toBeInTheDocument();
    expect(within(row).getByText("v0.3.1")).toBeInTheDocument();
    // 已验证徽标只出现在 verified 条目上；唯一 verified 的日历助手已安装被过滤
    expect(screen.queryByText("已验证")).not.toBeInTheDocument();
    // 已安装区域（标题 + 条目）
    expect(screen.getByRole("heading", { name: "已安装" })).toBeInTheDocument();
    expect(screen.getByText("@clawhub_x/calendar")).toBeInTheDocument();
    // 未安装的技能正常给出安装按钮
    expect(within(row).getByText("安装")).toBeInTheDocument();
  });

  it("replaces the ranking with search results", async () => {
    const fetchMock = stubFetch();

    renderSkillHub();
    await awaitCatalogLoaded();

    fireEvent.change(screen.getByPlaceholderText("搜索商店技能…"), {
      target: { value: "天气" },
    });
    fireEvent.click(screen.getByText("搜索"));

    await waitFor(() => expect(screen.getByText("天气查询")).toBeInTheDocument());
    expect(screen.queryByText("日历助手")).not.toBeInTheDocument();
    expect(screen.getByText(/「天气」共 1 个可安装技能/)).toBeInTheDocument();

    const searchCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/api/webui/skill-hub/search"),
    ) as [string, RequestInit] | undefined;
    expect(searchCall?.[0]).toContain("q=%E5%A4%A9%E6%B0%94");

    // 返回排行榜后恢复默认列表
    fireEvent.click(screen.getByText("返回排行榜"));
    await waitFor(() => expect(screen.getByText("日历助手")).toBeInTheDocument());
  });

  it("installs a skill, passes slug + namespace, then shows it as installed", async () => {
    // 安装成功后锁文件里就会有这条记录：让 installed 接口随之变化，
    // 以便断言安装后的刷新不会把「已安装」徽标又抹掉
    let installedBody: SkillHubInstalledPayload = INSTALLED_EMPTY;
    const fetchMock = stubFetch([
      ["/api/webui/skill-hub/installed", () => jsonResponse(installedBody)],
      [
        "/api/webui/skill-hub/install",
        () => {
          installedBody = INSTALLED_WITH_CALENDAR;
          return jsonResponse({
            installed: true,
            slug: "calendar",
            namespace: "clawhub_x",
            output: "ok",
          });
        },
      ],
    ]);

    const onInstalled = vi.fn();
    renderSkillHub({ onInstalled });
    await awaitCatalogLoaded();

    const row = skillRow("日历助手");
    fireEvent.click(within(row).getByText("安装"));
    await waitFor(() => expect(onInstalled).toHaveBeenCalledTimes(1));

    const installCall = fetchMock.mock.calls.find(([url]) => isInstallCall(url)) as [
      string,
      RequestInit,
    ];
    expect(installCall[0]).toContain("/api/webui/skill-hub/install");
    const header = (installCall[1].headers as Record<string, string>)[
      "X-Xianaibot-SkillHub-Values"
    ];
    // slug 是完整形态、namespace 是命名空间 handle，两者都要传给后端
    expect(JSON.parse(decodeURIComponent(header))).toEqual({
      slug: "@clawhub_x/calendar",
      namespace: "clawhub_x",
      force: false,
    });
    // 安装成功：给出成功提示，技能从可安装列表移除，并出现在已安装区域
    await waitFor(() => expect(screen.getByText("已安装「日历助手」")).toBeInTheDocument());
    await waitFor(() => expect(screen.queryByText("读取与创建日程")).not.toBeInTheDocument());
    expect(screen.getByText("@clawhub_x/calendar")).toBeInTheDocument();
  });

  it("offers a force reinstall after a 409 conflict", async () => {
    const fetchMock = stubFetch([
      [
        "/api/webui/skill-hub/install",
        (_url: string, init?: RequestInit) => {
          const header = (init?.headers as Record<string, string>)[
            "X-Xianaibot-SkillHub-Values"
          ];
          const values = JSON.parse(decodeURIComponent(header)) as { force?: boolean };
          if (!values.force) {
            return errorResponse(409, "技能已存在：clawhub_x/calendar（可用覆盖安装强制更新）");
          }
          return jsonResponse({
            installed: true,
            slug: "calendar",
            namespace: "clawhub_x",
            output: "ok",
          });
        },
      ],
    ]);

    renderSkillHub();
    await awaitCatalogLoaded();

    const row = skillRow("日历助手");
    fireEvent.click(within(row).getByText("安装"));
    await waitFor(() => expect(screen.getByText("已安装，可覆盖")).toBeInTheDocument());
    expect(within(row).getByText("覆盖安装")).toBeInTheDocument();

    fireEvent.click(within(row).getByText("覆盖安装"));
    // 强制覆盖成功后给出成功提示
    await waitFor(() => expect(screen.getByText("已安装「日历助手」")).toBeInTheDocument());

    const installCalls = fetchMock.mock.calls.filter(([url]) =>
      isInstallCall(url),
    ) as Array<[string, RequestInit]>;
    const lastForce = JSON.parse(
      decodeURIComponent(
        (installCalls[installCalls.length - 1][1].headers as Record<string, string>)[
          "X-Xianaibot-SkillHub-Values"
        ],
      ),
    ) as { force?: boolean };
    expect(lastForce.force).toBe(true);
  });

  it("reports update check results including skipped skills", async () => {
    stubFetch();

    renderSkillHub();
    await awaitCatalogLoaded();

    fireEvent.click(screen.getByText("检查升级"));
    await waitFor(() =>
      expect(
        screen.getByText(/共检查 2 个 · 可升级 1 个 · 跳过 1 个 · 失败 0 个/),
      ).toBeInTheDocument(),
    );
    // skipped 的含义必须如实说明：无更新清单的技能无法被升级
    expect(screen.getByText(/没有自带更新清单（config.json）/)).toBeInTheDocument();
    expect(screen.getByText(/upgrade done: checked=2/)).toBeInTheDocument();
  });

  it("auto-refreshes the ranking every 30 minutes", async () => {
    vi.useFakeTimers();
    const fetchMock = stubFetch();

    renderSkillHub();
    // 冲刷微任务，等待挂载时的首次加载完成
    await act(async () => {
      for (let i = 0; i < 5; i += 1) await Promise.resolve();
    });
    const catalogCalls = () =>
      fetchMock.mock.calls.filter(([url]) => String(url).includes("/catalog")).length;
    expect(catalogCalls()).toBe(1);
    expect(
      String(fetchMock.mock.calls.find(([url]) => String(url).includes("/catalog"))?.[0]),
    ).toContain("type=hot");

    // 前进 30 分钟 → 触发一次静默自动刷新
    await act(async () => {
      vi.advanceTimersByTime(30 * 60 * 1000);
    });
    await act(async () => {
      for (let i = 0; i < 5; i += 1) await Promise.resolve();
    });
    expect(catalogCalls()).toBe(2);
  });
});
