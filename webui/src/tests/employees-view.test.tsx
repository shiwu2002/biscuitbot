import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EmployeesView } from "@/components/settings/EmployeesView";
import { ClientProvider } from "@/providers/ClientProvider";
import type { Employee } from "@/lib/types";

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: (name: string) => (name === "content-type" ? "application/json" : null) },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const CLIP_MASTER: Employee = {
  id: "clip-master",
  name: "剪影",
  title: "剪辑",
  avatar: "🎬",
  system_prompt: "你是「剪影」，团队里的剪辑高手，精通剪映自动化剪辑。",
  skills: [],
  enabled: true,
  created_at: "2026-08-13T00:00:00Z",
};

function renderView(overrides: {
  onChanged?: () => void;
  onPick?: (employee: Employee) => void;
  onOpenTalentMarket?: () => void;
} = {}) {
  return {
    onPick: overrides.onPick ?? vi.fn(),
    onOpenTalentMarket: overrides.onOpenTalentMarket ?? vi.fn(),
    ...render(
      <ClientProvider client={{} as never} token="tok">
        <EmployeesView
          employees={[CLIP_MASTER]}
          onChanged={overrides.onChanged}
          onPick={overrides.onPick}
          onOpenTalentMarket={overrides.onOpenTalentMarket}
          onBackToChat={vi.fn()}
        />
      </ClientProvider>,
    ),
  };
}

describe("EmployeesView 数字人员工视图", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders card face: codename, title, persona and status", () => {
    renderView();
    expect(screen.getByText("数字人员工")).toBeInTheDocument();
    expect(screen.getByText("剪影")).toBeInTheDocument();
    expect(screen.getByText("剪辑")).toBeInTheDocument();
    expect(screen.getByText("你是「剪影」，团队里的剪辑高手，精通剪映自动化剪辑。")).toBeInTheDocument();
    expect(screen.getByText("启用")).toBeInTheDocument();
    // 技能不手动分配：卡面不再出现技能
    expect(screen.queryByText(/绑定技能/)).not.toBeInTheDocument();
  });

  it("opens the talent market from the 人才市场 button", () => {
    const onOpenTalentMarket = vi.fn();
    renderView({ onOpenTalentMarket });
    fireEvent.click(screen.getByText("人才市场"));
    expect(onOpenTalentMarket).toHaveBeenCalledTimes(1);
  });

  it("picks the employee via 和 TA 对话", () => {
    const onPick = vi.fn();
    renderView({ onPick });
    fireEvent.click(screen.getByText("和 TA 对话"));
    expect(onPick).toHaveBeenCalledWith(CLIP_MASTER);
  });

  it("creates an employee with codename, title and persona", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ...CLIP_MASTER }));
    vi.stubGlobal("fetch", fetchMock);

    const onChanged = vi.fn();
    renderView({ onChanged });

    fireEvent.click(screen.getByText("新建员工"));
    fireEvent.change(screen.getByPlaceholderText("例如：剪影"), {
      target: { value: "绘野" },
    });
    fireEvent.change(screen.getByPlaceholderText("例如：剪辑"), {
      target: { value: "全能设计" },
    });
    fireEvent.change(screen.getByPlaceholderText("你是「{{name}}」数字人员工……"), {
      target: { value: "你是「绘野」，全能设计师。" },
    });
    fireEvent.click(screen.getByText("保存"));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/webui/employees/create");
    const header = (init.headers as Record<string, string>)["X-Biscuitbot-Employee-Values"];
    expect(JSON.parse(decodeURIComponent(header))).toEqual(
      expect.objectContaining({ name: "绘野", title: "全能设计", enabled: true }),
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("updates an existing employee", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ...CLIP_MASTER }));
    vi.stubGlobal("fetch", fetchMock);

    const onChanged = vi.fn();
    renderView({ onChanged });

    fireEvent.click(screen.getByText("编辑"));
    fireEvent.change(screen.getByPlaceholderText("例如：剪辑"), {
      target: { value: "短视频剪辑" },
    });
    fireEvent.click(screen.getByText("保存"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/api/webui/employees/update?id=clip-master");
    const header = (init.headers as Record<string, string>)["X-Biscuitbot-Employee-Values"];
    expect(JSON.parse(decodeURIComponent(header))).toEqual(
      expect.objectContaining({ name: "剪影", title: "短视频剪辑" }),
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("deletes an employee after confirmation", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ deleted: true, id: "clip-master" }));
    vi.stubGlobal("fetch", fetchMock);

    const onChanged = vi.fn();
    renderView({ onChanged });

    fireEvent.click(screen.getByText("删除"));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("删除员工")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByText("删除"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/webui/employees/clip-master/delete");
    expect(onChanged).toHaveBeenCalledTimes(1);
  });
});
