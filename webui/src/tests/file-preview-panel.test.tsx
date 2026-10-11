import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  fetchFilePreview: vi.fn(),
  fetchFileDownload: vi.fn(),
  revealWorkspaceFile: vi.fn(),
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
