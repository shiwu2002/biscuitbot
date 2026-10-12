import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { AttachmentTile } from "@/components/AttachmentTile";
import { DocumentPreview } from "@/components/document-preview/DocumentPreview";
import { clearMediaBufferCache } from "@/lib/file-preview";
import type { UIMediaAttachment } from "@/lib/types";

// 重型渲染库全部 mock 掉：happy-dom 不解析真实二进制，只验证分派与渲染路径。
const mocks = vi.hoisted(() => ({
  renderAsync: vi.fn(async (_buffer: unknown, container: HTMLElement) => {
    const marker = document.createElement("div");
    marker.dataset.testid = "docx-rendered";
    container.appendChild(marker);
  }),
  read: vi.fn(() => ({ SheetNames: ["Sheet1"], Sheets: { Sheet1: {} } })),
  sheet_to_json: vi.fn(() => [["Name", "Score"], ["Alice", 10]] as unknown[][]),
  PptxPreview: () => <div data-testid="pptx-preview">pptx</div>,
}));

vi.mock("docx-preview", () => ({ renderAsync: mocks.renderAsync }));
vi.mock("xlsx", () => ({
  read: mocks.read,
  utils: { sheet_to_json: mocks.sheet_to_json },
}));
vi.mock("react-pptx-preview-kit", () => ({ PptxPreview: mocks.PptxPreview }));

beforeEach(() => {
  clearMediaBufferCache();
  // 每个用例独立的 mock 调用记录（否则 calls[0] 会指向上个用例已卸载清空的容器）
  vi.clearAllMocks();
});
afterEach(() => {
  vi.unstubAllGlobals();
  clearMediaBufferCache();
});

function stubFetch(body: ArrayBuffer | string) {
  const mock = vi.fn(async () => new Response(body));
  vi.stubGlobal("fetch", mock);
  return mock;
}

const pptxAttachment: UIMediaAttachment = {
  kind: "file",
  url: "/api/media/sig/deck",
  name: "deck.pptx",
};

describe("AttachmentTile document preview", () => {
  it("calls onOpenPreview instead of downloading when the main view passes it", () => {
    const onOpenPreview = vi.fn();
    render(<AttachmentTile attachment={pptxAttachment} onOpenPreview={onOpenPreview} />);

    const chip = screen.getByTitle("deck.pptx");
    expect(chip).toHaveTextContent("deck.pptx");
    expect(screen.queryByRole("link", { name: "文件附件" })).not.toBeInTheDocument();

    fireEvent.click(chip);
    expect(onOpenPreview).toHaveBeenCalledTimes(1);
    expect(onOpenPreview).toHaveBeenCalledWith(pptxAttachment);
  });

  it("keeps the download chip when onOpenPreview is not provided", () => {
    render(<AttachmentTile attachment={pptxAttachment} />);

    expect(screen.getByRole("link", { name: "文件附件" })).toHaveAttribute(
      "href",
      "/api/media/sig/deck",
    );
    expect(screen.queryByRole("button", { name: "预览 deck.pptx" })).not.toBeInTheDocument();
  });

  it("keeps the download chip for extensions the panel cannot preview without a path", () => {
    const onOpenPreview = vi.fn();
    render(
      <AttachmentTile
        attachment={{ kind: "file", url: "/m/pdf", name: "doc.pdf" }}
        onOpenPreview={onOpenPreview}
      />,
    );
    expect(screen.getByRole("link", { name: "文件附件" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /预览/ })).not.toBeInTheDocument();
  });

  it("keeps the download chip for inline or compact variants", () => {
    const onOpenPreview = vi.fn();
    const { unmount } = render(
      <AttachmentTile attachment={pptxAttachment} inline onOpenPreview={onOpenPreview} />,
    );
    expect(screen.getByRole("link", { name: "文件附件" })).toBeInTheDocument();
    unmount();
    render(
      <AttachmentTile attachment={pptxAttachment} variant="compact" onOpenPreview={onOpenPreview} />,
    );
    expect(screen.getByRole("link", { name: "文件附件" })).toBeInTheDocument();
  });

  it("keeps the unavailable tile without a url", () => {
    render(<AttachmentTile attachment={{ kind: "file", name: "deck.pptx" }} />);
    expect(screen.queryByRole("button", { name: /预览/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "文件附件" })).not.toBeInTheDocument();
  });
});

describe("DocumentPreview renderers", () => {
  it("renders docx via renderAsync and clears the container on unmount", async () => {
    stubFetch(new ArrayBuffer(16));
    const { unmount } = render(
      <DocumentPreview
        attachment={{ kind: "file", url: "/m/doc", name: "report.docx" }}
        kind="docx"
      />,
    );

    await waitFor(() => expect(screen.getByTestId("docx-rendered")).toBeInTheDocument());
    const container = mocks.renderAsync.mock.calls[0]?.[1] as HTMLElement;
    expect(container.innerHTML).not.toBe("");

    unmount();
    expect(container.innerHTML).toBe("");
  });

  it("renders xlsx sheets as tables", async () => {
    stubFetch(new ArrayBuffer(16));
    render(
      <DocumentPreview attachment={{ kind: "file", url: "/m/x", name: "data.xlsx" }} kind="xlsx" />,
    );

    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    expect(screen.getByText("Sheet1")).toBeInTheDocument();
    expect(screen.getByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument();
  });

  it("renders csv as a table without heavy deps", async () => {
    stubFetch("name,score\nalice,10\n");
    render(
      <DocumentPreview attachment={{ kind: "file", url: "/m/c", name: "data.csv" }} kind="csv" />,
    );

    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    expect(screen.getByText("alice")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument();
  });

  it("renders html inside a sandboxed iframe", async () => {
    stubFetch("<p>hello</p>");
    const { container } = render(
      <DocumentPreview
        attachment={{ kind: "file", url: "/m/h", name: "page.html" }}
        kind="html"
      />,
    );

    await waitFor(() => expect(container.querySelector("iframe")).toBeInTheDocument());
    const iframe = container.querySelector("iframe");
    expect(iframe).toHaveAttribute("sandbox", "allow-scripts");
    expect(iframe).toHaveAttribute("title", "page.html");
    expect(iframe?.getAttribute("srcdoc")).toContain("hello");
  });

  it("opens a fullscreen html viewer with allow-scripts", async () => {
    stubFetch("<p>hello</p>");
    render(
      <DocumentPreview
        attachment={{ kind: "file", url: "/m/h", name: "page.html" }}
        kind="html"
      />,
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "全屏查看" })).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: "全屏查看" }));

    const dialog = await screen.findByRole("dialog");
    const frame = within(dialog).queryByTitle("page.html");
    expect(frame).toHaveAttribute("sandbox", "allow-scripts");
    expect(frame?.getAttribute("srcdoc")).toContain("hello");
  });
});
