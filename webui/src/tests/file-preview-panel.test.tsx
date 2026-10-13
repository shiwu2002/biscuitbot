import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  fetchFilePreview: vi.fn(),
  fetchFileDownload: vi.fn(),
  revealWorkspaceFile: vi.fn(),
  revealAsset: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
      this.name = "ApiError";
    }
  },
  fetchFilePreview: mocks.fetchFilePreview,
  fetchFileDownload: mocks.fetchFileDownload,
  revealWorkspaceFile: mocks.revealWorkspaceFile,
  revealAsset: mocks.revealAsset,
}));

import { FilePreviewPanel } from "@/components/FilePreviewPanel";
import { ApiError } from "@/lib/api";

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

function renderPanel(path = "reports/报告.zip", canRevealFile = false) {
  return render(
    <FilePreviewPanel
      sessionKey="websocket:chat-1"
      path={path}
      token="tok"
      canRevealFile={canRevealFile}
      onClose={() => {}}
    />,
  );
}

function resolvedPreview() {
  mocks.fetchFilePreview.mockResolvedValue({
    path: "reports/报告.zip",
    display_path: "reports/报告.zip",
    project_path: "reports",
    language: "zip",
    content: "",
    size: 3,
    truncated: false,
  });
}

describe("FilePreviewPanel download", () => {
  it("lazily resolves a signed media url and hands it to the browser", async () => {
    resolvedPreview();
    mocks.fetchFileDownload.mockResolvedValue({
      url: "/api/media/sig/payload",
      name: "报告.zip",
    });
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});

    renderPanel();
    await waitFor(() => expect(mocks.fetchFilePreview).toHaveBeenCalled());

    const button = screen.getByRole("button", { name: "下载" });
    // 懒加载：仅预览不需要 staging 文件
    expect(mocks.fetchFileDownload).not.toHaveBeenCalled();

    fireEvent.click(button);

    await waitFor(() => expect(clickSpy).toHaveBeenCalled());
    expect(mocks.fetchFileDownload).toHaveBeenCalledWith(
      "tok",
      "websocket:chat-1",
      "reports/报告.zip",
    );
    const anchor = clickSpy.mock.instances[0] as unknown as HTMLAnchorElement;
    expect(anchor.getAttribute("href")).toBe("/api/media/sig/payload");
    expect(anchor.download).toBe("报告.zip");
    // 触发用的临时 anchor 不应留在 DOM 里
    expect(document.querySelector('a[href="/api/media/sig/payload"]')).toBeNull();
  });

  it("offers a download when the file is binary and cannot be previewed", async () => {
    mocks.fetchFilePreview.mockRejectedValue(
      new ApiError(415, "binary files cannot be previewed"),
    );
    mocks.fetchFileDownload.mockResolvedValue({
      url: "/api/media/sig/payload",
      name: "报告.zip",
    });

    renderPanel();

    await waitFor(() => expect(screen.getByText(/二进制文件/)).toBeInTheDocument());
    // 头部图标按钮 + 错误态带文字按钮
    expect(screen.getAllByRole("button", { name: "下载" })).toHaveLength(2);
  });

  it("surfaces a download failure without losing the preview", async () => {
    resolvedPreview();
    mocks.fetchFileDownload.mockRejectedValue(new Error("boom"));

    renderPanel();
    await waitFor(() => expect(mocks.fetchFilePreview).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "下载" }));

    await waitFor(() => expect(screen.getByText(/下载失败/)).toBeInTheDocument());
  });
});

describe("FilePreviewPanel HTML rendering", () => {
  function resolvedHtmlPreview(overrides: Record<string, unknown> = {}) {
    mocks.fetchFilePreview.mockResolvedValue({
      path: "E:/project/reports/report.html",
      display_path: "reports/report.html",
      project_path: "E:/project",
      language: "html",
      content: "<!doctype html><html><body><h1>销售报告</h1></body></html>",
      size: 54,
      truncated: false,
      ...overrides,
    });
  }

  it("renders HTML in a sandboxed iframe instead of syntax-highlighted source", async () => {
    resolvedHtmlPreview();

    renderPanel("reports/report.html");

    const frame = await screen.findByTitle("report.html");
    expect(frame.tagName).toBe("IFRAME");
    // 允许页面自身脚本渲染交互界面，但不给 allow-same-origin（来源不透明）
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame.getAttribute("srcdoc")).toContain("销售报告");
    // 渲染模式下不再把源码铺成代码视图
    expect(screen.queryByText(/<!doctype html>/i)).toBeNull();
  });

  it("offers fullscreen viewing for rendered HTML", async () => {
    resolvedHtmlPreview();

    renderPanel("reports/report.html");

    await waitFor(() => expect(screen.getByRole("button", { name: "全屏查看" })).toBeInTheDocument());
  });

  it("keeps showing the source when the preview is truncated", async () => {
    // 截断的文件渲染出来只是残缺页面，不如保留源码 + 截断提示
    resolvedHtmlPreview({ truncated: true });

    renderPanel("reports/report.html");

    await waitFor(() => expect(mocks.fetchFilePreview).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByTitle("report.html")).toBeNull());
    expect(screen.queryByRole("button", { name: "全屏查看" })).toBeNull();
  });

  it("keeps syntax-highlighted source for non-HTML text files", async () => {
    mocks.fetchFilePreview.mockResolvedValue({
      path: "E:/project/reports/notes.md",
      display_path: "reports/notes.md",
      project_path: "E:/project",
      language: "markdown",
      content: "# 标题",
      size: 6,
      truncated: false,
    });

    renderPanel("reports/notes.md");

    await waitFor(() => expect(mocks.fetchFilePreview).toHaveBeenCalled());
    expect(screen.queryByTitle("notes.md")).toBeNull();
    expect(screen.queryByRole("button", { name: "全屏查看" })).toBeNull();
  });
});

describe("FilePreviewPanel reveal in file manager", () => {
  it("is hidden when the client is not on the workspace machine", async () => {
    resolvedPreview();

    renderPanel();
    await waitFor(() => expect(mocks.fetchFilePreview).toHaveBeenCalled());

    // 异地浏览器没有这台机器上的文件，只能下载
    expect(screen.queryByRole("button", { name: "在文件夹中显示" })).toBeNull();
  });

  it("selects the original file instead of copying one", async () => {
    resolvedPreview();
    mocks.revealWorkspaceFile.mockResolvedValue({
      ok: true,
      path: "E:/project/reports/报告.zip",
    });

    renderPanel("reports/报告.zip", true);
    await waitFor(() => expect(mocks.fetchFilePreview).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "在文件夹中显示" }));

    await waitFor(() =>
      expect(mocks.revealWorkspaceFile).toHaveBeenCalledWith(
        "tok",
        "websocket:chat-1",
        "reports/报告.zip",
      ),
    );
    // 定位原文件即可，不应再 staging/下载一份副本
    expect(mocks.fetchFileDownload).not.toHaveBeenCalled();
  });

  it("surfaces a reveal failure", async () => {
    resolvedPreview();
    mocks.revealWorkspaceFile.mockRejectedValue(new Error("boom"));

    renderPanel("reports/报告.zip", true);
    await waitFor(() => expect(mocks.fetchFilePreview).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "在文件夹中显示" }));

    await waitFor(() => expect(screen.getByText(/无法在文件夹中定位/)).toBeInTheDocument());
  });

  it("offers both actions when a binary file cannot be previewed", async () => {
    mocks.fetchFilePreview.mockRejectedValue(
      new ApiError(415, "binary files cannot be previewed"),
    );

    renderPanel("reports/报告.zip", true);

    await waitFor(() => expect(screen.getByText(/二进制文件/)).toBeInTheDocument());
    // 头部图标按钮 + 错误态带文字按钮，两个动作各自成对
    expect(screen.getAllByRole("button", { name: "在文件夹中显示" })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "下载" })).toHaveLength(2);
  });
});

describe("FilePreviewPanel 会话资产（媒体模式）定位原文件", () => {
  const ASSET_ID = "QDoL3RtcC9kZWxpdmVyZWQv5oql5ZGK5omT5YyFLnppcA";

  function renderAsset(
    { assetId = ASSET_ID as string | null, name = "报告打包.zip", canRevealFile = true } = {},
  ) {
    return render(
      <FilePreviewPanel
        sessionKey="websocket:chat-1"
        media={{ url: `/api/media/sig/${name}`, name }}
        assetId={assetId}
        token="tok"
        canRevealFile={canRevealFile}
        onClose={() => {}}
      />,
    );
  }

  it("带资产 id 时按资产路由定位原文件（不 staging 副本）", async () => {
    mocks.revealAsset.mockResolvedValue({ ok: true, path: "E:/tmp/delivered/报告.docx" });

    renderAsset({ name: "报告.docx" });

    fireEvent.click(screen.getByRole("button", { name: "在文件夹中显示" }));

    await waitFor(() =>
      expect(mocks.revealAsset).toHaveBeenCalledWith("tok", "websocket:chat-1", ASSET_ID),
    );
    // 走的是资产 id 而不是相对路径：工作区外的交付件没有相对路径可用。
    expect(mocks.revealWorkspaceFile).not.toHaveBeenCalled();
    expect(mocks.fetchFileDownload).not.toHaveBeenCalled();
  });

  it("media 模式没有资产 id（真·附件）时不提供定位入口", async () => {
    renderAsset({ assetId: null, name: "报告.docx" });

    expect(screen.queryByRole("button", { name: "在文件夹中显示" })).toBeNull();
    expect(screen.getAllByRole("button", { name: "下载" })).toHaveLength(1);
  });

  it("预览不了的交付件在兜底态也给「在文件夹中显示」", async () => {
    mocks.revealAsset.mockResolvedValue({ ok: true, path: "E:/tmp/delivered/报告打包.zip" });

    renderAsset();

    // `.zip` 没有文档预览类型 → 走二进制兜底，出路不能只有下载
    await waitFor(() => expect(screen.getByText(/二进制文件/)).toBeInTheDocument());
    expect(screen.getAllByRole("button", { name: "在文件夹中显示" })).toHaveLength(2);

    fireEvent.click(screen.getAllByRole("button", { name: "在文件夹中显示" })[1]);

    await waitFor(() =>
      expect(mocks.revealAsset).toHaveBeenCalledWith("tok", "websocket:chat-1", ASSET_ID),
    );
  });
});
