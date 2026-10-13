import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { KnowledgeView } from "@/components/knowledge/KnowledgeView";
import { ClientProvider } from "@/providers/ClientProvider";
import { deleteKnowledgeDocument, fetchKnowledgeDocuments } from "@/lib/api";
import type { KnowledgeDocument } from "@/lib/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchKnowledgeDocuments: vi.fn(),
    deleteKnowledgeDocument: vi.fn(),
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

  it("列出上传的文档并提供上传入口", async () => {
    renderView();

    expect(await screen.findByText("guide.md")).toBeInTheDocument();
    expect(screen.getByText("photo.png")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上传文件" })).toBeInTheDocument();
  });

  it("不再有「资产」Tab（资产已按会话工作区搬进会话视图）", async () => {
    renderView();

    await screen.findByText("guide.md");

    // 全局资产 Tab 已下线：留下的只有文档列表。
    expect(screen.queryByRole("button", { name: "资产" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("assets-view")).not.toBeInTheDocument();
  });

  it("删除文档走确认弹窗", async () => {
    vi.mocked(deleteKnowledgeDocument).mockResolvedValue({
      documents: DOCS.filter((d) => d.doc_id !== "guide.md"),
    });
    renderView();

    await screen.findByText("guide.md");
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);

    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(deleteKnowledgeDocument).toHaveBeenCalledWith("tok", "guide.md");
    });
  });
});
