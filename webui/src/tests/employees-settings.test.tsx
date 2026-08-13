import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EmployeesSettings } from "@/components/settings/EmployeesSettings";
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
  name: "剪辑高手",
  avatar: "🎬",
  system_prompt: "你是一名剪辑高手数字人员工，精通剪映自动化剪辑。",
  skills: ["jianying-editor"],
  enabled: true,
  created_at: "2026-08-13T00:00:00Z",
};

function renderSettings(overrides: { onChanged?: () => void } = {}) {
  render(
    <ClientProvider client={{} as never} token="tok">
      <EmployeesSettings
        employees={[CLIP_MASTER]}
        skills={["jianying-editor", "web_search"]}
        onChanged={overrides.onChanged}
      />
    </ClientProvider>,
  );
}

describe("EmployeesSettings", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("lists employees with persona, status and bound skills", () => {
    renderSettings();
    expect(screen.getByText("剪辑高手")).toBeInTheDocument();
    expect(screen.getByText("你是一名剪辑高手数字人员工，精通剪映自动化剪辑。")).toBeInTheDocument();
    expect(screen.getByText("启用")).toBeInTheDocument();
    expect(screen.getByText("绑定技能：jianying-editor")).toBeInTheDocument();
  });

  it("creates an employee through the editor dialog", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ...CLIP_MASTER }));
    vi.stubGlobal("fetch", fetchMock);

    const onChanged = vi.fn();
    renderSettings({ onChanged });

    fireEvent.click(screen.getByText("新建员工"));
    fireEvent.change(screen.getByPlaceholderText("例如：剪辑高手"), {
      target: { value: "配音专员" },
    });
    fireEvent.change(screen.getByPlaceholderText("你是一名「{{name}}」数字人员工……"), {
      target: { value: "你是一名配音专员，负责为视频配置旁白。".replace("{{name}}", "配音专员") },
    });
    fireEvent.click(screen.getByText("保存"));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/webui/employees/create");
    const header = (init.headers as Record<string, string>)["X-Biscuitbot-Employee-Values"];
    expect(JSON.parse(decodeURIComponent(header))).toEqual(
      expect.objectContaining({ name: "配音专员", enabled: true, skills: [] }),
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("creates an employee with a bound skill toggled on", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ...CLIP_MASTER }));
    vi.stubGlobal("fetch", fetchMock);

    renderSettings();

    fireEvent.click(screen.getByText("新建员工"));
    fireEvent.change(screen.getByPlaceholderText("例如：剪辑高手"), {
      target: { value: "剪辑高手" },
    });
    fireEvent.change(screen.getByPlaceholderText("你是一名「{{name}}」数字人员工……"), {
      target: { value: "你是一名剪辑高手。" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "jianying-editor" }));
    fireEvent.click(screen.getByText("保存"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/webui/employees/create");
    const header = (init.headers as Record<string, string>)["X-Biscuitbot-Employee-Values"];
    expect(JSON.parse(decodeURIComponent(header))).toEqual(
      expect.objectContaining({ name: "剪辑高手", skills: ["jianying-editor"] }),
    );
  });

  it("updates an existing employee", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ...CLIP_MASTER }));
    vi.stubGlobal("fetch", fetchMock);

    const onChanged = vi.fn();
    renderSettings({ onChanged });

    fireEvent.click(screen.getByLabelText("编辑员工 剪辑高手"));
    fireEvent.change(screen.getByPlaceholderText("你是一名「{{name}}」数字人员工……"), {
      target: { value: "更新后的提示词" },
    });
    fireEvent.click(screen.getByText("保存"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/webui/employees/update?id=clip-master");
    const header = (init.headers as Record<string, string>)["X-Biscuitbot-Employee-Values"];
    expect(JSON.parse(decodeURIComponent(header))).toEqual(
      expect.objectContaining({ name: "剪辑高手", system_prompt: "更新后的提示词" }),
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("deletes an employee after confirmation", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ deleted: true, id: "clip-master" }));
    vi.stubGlobal("fetch", fetchMock);

    const onChanged = vi.fn();
    renderSettings({ onChanged });

    fireEvent.click(screen.getByLabelText("删除员工 剪辑高手"));
    fireEvent.click(await screen.findByText("删除"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/webui/employees/clip-master/delete");
    expect(onChanged).toHaveBeenCalledTimes(1);
  });
});
