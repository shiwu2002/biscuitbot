import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

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
  it("shows a preview button and lazy-fetches only on expand", async () => {
    const fetchMock = stubFetch(new ArrayBuffer(16));
    render(<AttachmentTile attachment={pptxAttachment} />);

    expect(screen.getByLabelText("File attachment")).toHaveTextContent("deck.pptx");
    expect(screen.getByRole("button", { name: "预览" })).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "预览" }));
    await waitFor(() => expect(screen.getByTestId("pptx-preview")).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not offer preview for unsupported extensions", () => {
    render(<AttachmentTile attachment={{ kind: "file", url: "/m/pdf", name: "doc.pdf" }} />);
    expect(screen.queryByRole("button", { name: "预览" })).not.toBeInTheDocument();
  });

  it("does not offer preview for inline or compact variants", () => {
    const { unmount } = render(<AttachmentTile attachment={pptxAttachment} inline />);
    expect(screen.queryByRole("button", { name: "预览" })).not.toBeInTheDocument();
    unmount();
    render(<AttachmentTile attachment={pptxAttachment} variant="compact" />);
    expect(screen.queryByRole("button", { name: "预览" })).not.toBeInTheDocument();
  });

  it("does not offer preview without a url", () => {
    render(<AttachmentTile attachment={{ kind: "file", name: "deck.pptx" }} />);
    expect(screen.queryByRole("button", { name: "预览" })).not.toBeInTheDocument();
  });

  it("collapse unmounts the preview and re-expand uses the cached buffer", async () => {
    const fetchMock = stubFetch(new ArrayBuffer(16));
    render(
      <AttachmentTile
        attachment={{ kind: "file", url: "/api/media/sig/doc", name: "report.docx" }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "预览" }));
    await waitFor(() => expect(screen.getByTestId("docx-rendered")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "收起预览" }));
    expect(screen.queryByTestId("docx-rendered")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "预览" }));
    await waitFor(() => expect(screen.getByTestId("docx-rendered")).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("shows an error and download fallback when fetch fails", async () => {
    const fetchMock = vi.fn(async () => {
      throw new Error("boom");
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AttachmentTile attachment={{ ...pptxAttachment, url: "/m/bad" }} />);

    fireEvent.click(screen.getByRole("button", { name: "预览" }));
    await waitFor(() => expect(screen.getByText(/预览失败/)).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "下载" })).toHaveAttribute("href", "/m/bad");
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
    expect(iframe).toHaveAttribute("sandbox", "");
    expect(iframe).toHaveAttribute("title", "page.html");
    expect(iframe?.getAttribute("srcdoc")).toContain("hello");
  });
});
