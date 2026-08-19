import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { KnowledgeView } from "@/components/knowledge/KnowledgeView";
import { ClientProvider } from "@/providers/ClientProvider";
import {
  deleteAsset,
  deleteKnowledgeDocument,
  fetchAssets,
  fetchDocumentPreview,
  fetchKnowledgeDocuments,
} from "@/lib/api";
import type { Asset, KnowledgeDocument } from "@/lib/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchKnowledgeDocuments: vi.fn(),
    deleteKnowledgeDocument: vi.fn(),
    fetchAssets: vi.fn(),
    deleteAsset: vi.fn(),
    fetchDocumentPreview: vi.fn(),
  };
});

const DOCS: KnowledgeDocument[] = [
  {
    doc_id: "guide.md",
    kind: "document",
    caption: "",
    size: 123,
    indexed_at: "2026-08-17T00:00:00Z",
  },
  {
    doc_id: "photo.png",
    kind: "image",
    caption: "a red fox",
    size: 456,
    indexed_at: "2026-08-16T00:00:00Z",
  },
];

const ASSETS: Asset[] = [
  {
    id: "img_1234567890ab",
    name: "img_1234567890ab.png",
    kind: "image",
    size: 1000,
    created_at: "2026-08-17T10:00:00+08:00",
    caption: "a red fox",
    media_url: "/api/media/x/img.png",
  },
  {
    id: "vid_1234567890ab",
    name: "vid_1234567890ab.mp4",
    kind: "video",
    size: 2000,
    created_at: "2026-08-17T11:00:00+08:00",
    caption: "",
    media_url: "/api/media/x/vid.mp4",
  },
  {
    id: "tts_1234567890ab",
    name: "tts_1234567890ab.mp3",
    kind: "audio",
    size: 300,
    created_at: "2026-08-17T12:00:00+08:00",
    caption: "",
    media_url: "/api/media/x/tts.mp3",
  },
  {
    id: "e9329aaeb3bc_report",
    name: "e9329aaeb3bc_report.docx",
    kind: "document",
    size: 128,
    created_at: "2026-08-17T13:00:00+08:00",
    caption: "",
    media_url: "/api/media/x/report.docx",
  },
];

function renderView() {
  return render(
    <ClientProvider client={{} as never} token="tok">
      <KnowledgeView onBackToChat={vi.fn()} />
    </ClientProvider>,
  );
}

describe("KnowledgeView 知识库视图", () => {
  beforeEach(() => {
    vi.mocked(fetchKnowledgeDocuments).mockResolvedValue({ documents: DOCS });
    window.confirm = vi.fn(() => true) as unknown as typeof window.confirm;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("默认渲染「文档」Tab 并列出上传的文档", async () => {
    renderView();

    expect(await screen.findByText("guide.md")).toBeInTheDocument();
    expect(screen.getByText("photo.png")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "文档" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    // 上传按钮只在文档 Tab 出现
    expect(screen.getByRole("button", { name: "上传文件" })).toBeInTheDocument();
  });

  it("切换到「资产」Tab 后列出图片/文档/视频/音频", async () => {
    vi.mocked(fetchAssets).mockResolvedValue({ assets: ASSETS });
    renderView();

    await screen.findByText("guide.md");
    fireEvent.click(screen.getByRole("button", { name: "资产" }));

    expect(await screen.findByText("img_1234567890ab.png")).toBeInTheDocument();
    expect(screen.getByText("vid_1234567890ab.mp4")).toBeInTheDocument();
    expect(screen.getByText("tts_1234567890ab.mp3")).toBeInTheDocument();
    expect(screen.getByText("e9329aaeb3bc_report.docx")).toBeInTheDocument();
    // 资产 Tab 没有上传按钮
    expect(screen.queryByRole("button", { name: "上传文件" })).not.toBeInTheDocument();
  });

  it("资产分类筛选只显示所选类型的卡片", async () => {
    vi.mocked(fetchAssets).mockResolvedValue({ assets: ASSETS });
    renderView();

    await screen.findByText("guide.md");
    fireEvent.click(screen.getByRole("button", { name: "资产" }));
    await screen.findByText("e9329aaeb3bc_report.docx");

    const view = within(screen.getByTestId("assets-view"));
    fireEvent.click(view.getByRole("button", { name: "视频" }));
    expect(screen.getByText("vid_1234567890ab.mp4")).toBeInTheDocument();
    expect(screen.queryByText("img_1234567890ab.png")).not.toBeInTheDocument();
    expect(screen.queryByText("e9329aaeb3bc_report.docx")).not.toBeInTheDocument();

    fireEvent.click(view.getByRole("button", { name: "文档" }));
    expect(screen.getByText("e9329aaeb3bc_report.docx")).toBeInTheDocument();
    expect(screen.queryByText("vid_1234567890ab.mp4")).not.toBeInTheDocument();

    fireEvent.click(view.getByRole("button", { name: "全部" }));
    expect(screen.getByText("img_1234567890ab.png")).toBeInTheDocument();
    expect(screen.getByText("vid_1234567890ab.mp4")).toBeInTheDocument();
    expect(screen.getByText("tts_1234567890ab.mp3")).toBeInTheDocument();
  });

  it("点击图片资产打开图片预览（Lightbox）", async () => {
    vi.mocked(fetchAssets).mockResolvedValue({ assets: ASSETS });
    renderView();

    await screen.findByText("guide.md");
    fireEvent.click(screen.getByRole("button", { name: "资产" }));

    const imageButton = await screen.findByRole("button", {
      name: "img_1234567890ab.png",
    });
    fireEvent.click(imageButton);

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  it("点击文档资产打开文本预览弹窗", async () => {
    vi.mocked(fetchAssets).mockResolvedValue({ assets: ASSETS });
    vi.mocked(fetchDocumentPreview).mockResolvedValue({
      id: "e9329aaeb3bc_report",
      name: "e9329aaeb3bc_report.docx",
      kind: "document",
      size: 128,
      content: "Total revenue: $5,000,000",
      truncated: false,
    });
    renderView();

    await screen.findByText("guide.md");
    fireEvent.click(screen.getByRole("button", { name: "资产" }));
    await screen.findByText("e9329aaeb3bc_report.docx");

    fireEvent.click(screen.getByRole("button", { name: "e9329aaeb3bc_report.docx" }));

    expect(
      await screen.findByText("Total revenue: $5,000,000"),
    ).toBeInTheDocument();
    expect(fetchDocumentPreview).toHaveBeenCalledWith("tok", "e9329aaeb3bc_report");
  });

  it("删除资产走确认弹窗并用返回值刷新列表", async () => {
    vi.mocked(fetchAssets).mockResolvedValue({ assets: ASSETS });
    vi.mocked(deleteAsset).mockResolvedValue({
      assets: ASSETS.filter((a) => a.id !== "tts_1234567890ab"),
    });
    renderView();

    await screen.findByText("guide.md");
    fireEvent.click(screen.getByRole("button", { name: "资产" }));

    const deleteButtons = await screen.findAllByRole("button", { name: "删除" });
    fireEvent.click(deleteButtons[2]); // 第三个 = TTS（打开确认弹窗）

    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(deleteAsset).toHaveBeenCalledWith("tok", "tts_1234567890ab");
    });
    await waitFor(() => {
      expect(screen.queryByText("tts_1234567890ab.mp3")).not.toBeInTheDocument();
    });
    expect(deleteKnowledgeDocument).not.toHaveBeenCalled();
  });
});
