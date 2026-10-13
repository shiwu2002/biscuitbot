import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionAssetsPanel } from "@/components/thread/SessionAssetsPanel";
import { ClientProvider } from "@/providers/ClientProvider";
import { deleteAsset, fetchAssets } from "@/lib/api";
import type { Asset } from "@/lib/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchAssets: vi.fn(),
    deleteAsset: vi.fn(),
  };
});

const SESSION_KEY = "websocket:chat-1";

const ASSETS: Asset[] = [
  {
    id: "aW1nLnBuZw",
    name: "img_1234567890ab.png",
    kind: "image",
    size: 1000,
    created_at: "2026-08-17T10:00:00+08:00",
    caption: "a red fox",
    media_url: `/api/sessions/${encodeURIComponent(SESSION_KEY)}/workspace-media/sig/abc`,
  },
  {
    id: "dmlkLm1wNA",
    name: "vid_1234567890ab.mp4",
    kind: "video",
    size: 2000,
    created_at: "2026-08-17T11:00:00+08:00",
    caption: "",
    media_url: "",
  },
  {
    id: "dHRzLm1wMw",
    name: "tts_1234567890ab.mp3",
    kind: "audio",
    size: 300,
    created_at: "2026-08-17T12:00:00+08:00",
    caption: "",
    media_url: "",
  },
  {
    id: "cmVwb3J0LmRvY3g",
    name: "报告.docx",
    kind: "document",
    size: 128,
    created_at: "2026-08-17T13:00:00+08:00",
    caption: "",
    media_url: "",
    path: "reports/报告.docx",
  },
];

/** 卡面把文件名拆成「主干 + 后缀」两段（后缀要始终露出来），按可访问名找整卡更稳。 */
const card = (name: string) => screen.queryByRole("button", { name });
/** 音视频卡没有整卡热区：播放键才是它的入口。 */
const player = (name: string) => screen.queryByRole("button", { name: `播放 ${name}` });

function renderPanel(
  sessionKey: string = SESSION_KEY,
  onOpenAsset: (asset: Asset) => void = vi.fn(),
) {
  return render(
    <ClientProvider client={{} as never} token="tok">
      <SessionAssetsPanel
        open
        onOpenChange={vi.fn()}
        sessionKey={sessionKey}
        onOpenAsset={onOpenAsset}
      />
    </ClientProvider>,
  );
}

describe("SessionAssetsPanel 会话资产面板", () => {
  beforeEach(() => {
    vi.mocked(fetchAssets).mockResolvedValue({ assets: ASSETS });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("按会话键拉取资产，列出图片/视频/音频/文档", async () => {
    renderPanel();

    expect(await screen.findByRole("button", { name: "img_1234567890ab.png" })).toBeInTheDocument();
    expect(player("vid_1234567890ab.mp4")).toBeInTheDocument();
    expect(player("tts_1234567890ab.mp3")).toBeInTheDocument();
    expect(card("报告.docx")).toBeInTheDocument();
    // 音视频要能在卡面里就地播放：控件不能被包进「整卡热区」按钮（嵌套按钮无效）
    expect(card("vid_1234567890ab.mp4")).toBeNull();
    // 扫描根由后端按会话解析 —— 前端只报会话键与口径，不报路径。
    expect(fetchAssets).toHaveBeenCalledWith("tok", SESSION_KEY, "session");
  });

  it("会话切换后重新扫描新会话的工作区", async () => {
    const { rerender } = renderPanel();

    await screen.findByRole("button", { name: "img_1234567890ab.png" });

    vi.mocked(fetchAssets).mockResolvedValue({ assets: [] });
    rerender(
      <ClientProvider client={{} as never} token="tok">
        <SessionAssetsPanel open onOpenChange={vi.fn()} sessionKey="websocket:chat-2" />
      </ClientProvider>,
    );

    await waitFor(() => {
      expect(fetchAssets).toHaveBeenCalledWith("tok", "websocket:chat-2", "session");
    });
    await waitFor(() => {
      expect(card("img_1234567890ab.png")).toBeNull();
    });
  });

  it("分类筛选只显示所选类型的卡片", async () => {
    renderPanel();

    await screen.findByRole("button", { name: "报告.docx" });
    const view = within(screen.getByTestId("assets-view"));

    fireEvent.click(view.getByRole("button", { name: "视频" }));
    expect(player("vid_1234567890ab.mp4")).toBeInTheDocument();
    expect(card("img_1234567890ab.png")).toBeNull();
    expect(card("报告.docx")).toBeNull();

    fireEvent.click(view.getByRole("button", { name: "文档" }));
    expect(card("报告.docx")).toBeInTheDocument();
    expect(player("vid_1234567890ab.mp4")).toBeNull();

    fireEvent.click(view.getByRole("button", { name: "全部" }));
    expect(card("img_1234567890ab.png")).toBeInTheDocument();
    expect(player("vid_1234567890ab.mp4")).toBeInTheDocument();
    expect(player("tts_1234567890ab.mp3")).toBeInTheDocument();
  });

  it("点击图片资产打开图片预览（Lightbox）", async () => {
    renderPanel();

    const imageButton = await screen.findByRole("button", {
      name: "img_1234567890ab.png",
    });
    fireEvent.click(imageButton);

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  it("点击文档资产交给右侧预览框（不就地弹窗）", async () => {
    const onOpenAsset = vi.fn();
    renderPanel(SESSION_KEY, onOpenAsset);

    await screen.findByRole("button", { name: "报告.docx" });
    fireEvent.click(screen.getByRole("button", { name: "报告.docx" }));

    // 与会话里点附件走同一个右侧预览框：这里只把资产交出去，不自己渲染预览。
    expect(onOpenAsset).toHaveBeenCalledTimes(1);
    expect(onOpenAsset.mock.calls[0][0]).toMatchObject({
      id: "cmVwb3J0LmRvY3g",
      name: "报告.docx",
      path: "reports/报告.docx",
    });
    // 只有抽屉自己这一个 dialog —— 预览框是右侧面板，不在这里就地弹。
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
  });

  it("删除资产走确认弹窗并用返回值刷新列表", async () => {
    vi.mocked(deleteAsset).mockResolvedValue({
      assets: ASSETS.filter((a) => a.id !== "dHRzLm1wMw"),
    });
    renderPanel();

    const deleteButtons = await screen.findAllByRole("button", { name: "删除" });
    fireEvent.click(deleteButtons[2]); // 第三个 = TTS（打开确认弹窗）

    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(deleteAsset).toHaveBeenCalledWith("tok", SESSION_KEY, "dHRzLm1wMw", "session");
    });
    await waitFor(() => {
      expect(player("tts_1234567890ab.mp3")).toBeNull();
    });
  });

  it("工作区外的交付件（如 .zip）也照样交给右侧预览框", async () => {
    const onOpenAsset = vi.fn();
    vi.mocked(fetchAssets).mockResolvedValue({
      assets: [
        {
          id: "QDoL3RtcC9kZWxpdmVyZWQv5oql5ZGK5omT5YyFLnppcA",
          name: "报告打包.zip",
          kind: "document",
          size: 4096,
          created_at: "2026-08-17T14:00:00+08:00",
          caption: "",
          media_url: "/api/media/sig/zip",
          delivered: true,
        },
      ],
    });
    renderPanel(SESSION_KEY, onOpenAsset);

    fireEvent.click(await screen.findByRole("button", { name: "报告打包.zip" }));

    // 工作区外的交付件没有相对路径（path 为空串）：预览框按会话资产 id 兜底 ——
    // 预览不了就给「在文件夹中显示」/下载，出路在那边，不在这里。
    expect(onOpenAsset).toHaveBeenCalledTimes(1);
    expect(onOpenAsset.mock.calls[0][0]).toMatchObject({
      id: "QDoL3RtcC9kZWxpdmVyZWQv5oql5ZGK5omT5YyFLnppcA",
      name: "报告打包.zip",
      media_url: "/api/media/sig/zip",
      delivered: true,
    });
  });

  it("工作区文件过多时给出截断提示", async () => {
    vi.mocked(fetchAssets).mockResolvedValue({ assets: ASSETS, truncated: true });
    renderPanel();

    expect(
      await screen.findByText("工作区文件过多，这里只显示了最近的一部分。"),
    ).toBeInTheDocument();
  });

  it("交付给用户的文件带「已交付」标记（工作区外的也照列）", async () => {
    vi.mocked(fetchAssets).mockResolvedValue({
      assets: [
        { ...ASSETS[0], delivered: true },
        {
          id: "QDoL3RtcC9kZWxpdmVyZWQv5oql5ZGK5omT5YyFLnppcA",
          name: "报告打包.zip",
          kind: "document",
          size: 4096,
          created_at: "2026-08-17T14:00:00+08:00",
          caption: "",
          media_url: "/api/media/sig/zip",
          delivered: true,
        },
        { ...ASSETS[1], delivered: false },
      ],
    });
    renderPanel();

    // `.zip` 不在产出白名单里，但它交付过 —— 必须看得见（这是硬保证）。
    expect(await screen.findByRole("button", { name: "报告打包.zip" })).toBeInTheDocument();
    // 只有交付过的卡片带标记，扫描到的产出不带。
    expect(screen.getAllByText("已交付")).toHaveLength(2);
  });

  it("长文件名被截断时后缀仍然露在外面", async () => {
    vi.mocked(fetchAssets).mockResolvedValue({
      assets: [{ ...ASSETS[3], name: "内存价格最新消息与降价时点研判_2026-10-12.docx" }],
    });
    renderPanel();

    // 主干可截断，后缀单独成段、恒可见 —— 卡片再窄也丢不掉文件类型
    expect(
      await screen.findByText("内存价格最新消息与降价时点研判_2026-10-12"),
    ).toBeInTheDocument();
    expect(screen.getByText(".docx")).toBeInTheDocument();
    // 卡面上的大字后缀（供缩略图网格快速辨识）另算一处
    expect(screen.getByText("DOCX")).toBeInTheDocument();
  });

  it("空工作区给出空态提示", async () => {
    vi.mocked(fetchAssets).mockResolvedValue({ assets: [] });
    renderPanel();

    expect(
      await screen.findByText(
        "这个会话还没有产出。智能体在这里写过的、交付给你的文件会出现在这里。",
      ),
    ).toBeInTheDocument();
  });

  it("缺省是本会话口径，切到「整个工作区」再按工作区重取", async () => {
    vi.mocked(fetchAssets).mockResolvedValue({ assets: ASSETS });
    renderPanel();

    await screen.findByRole("button", { name: "img_1234567890ab.png" });
    // 缺省不是工作区口径：同一工作区被多个会话共用时，那不是会话资产。
    expect(fetchAssets).toHaveBeenCalledWith("tok", SESSION_KEY, "session");
    expect(screen.getByRole("tab", { name: "本会话" })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    vi.mocked(fetchAssets).mockResolvedValue({ assets: [ASSETS[0]] });
    fireEvent.click(screen.getByRole("tab", { name: "整个工作区" }));

    await waitFor(() => {
      expect(fetchAssets).toHaveBeenCalledWith("tok", SESSION_KEY, "workspace");
    });
    await waitFor(() => {
      expect(card("报告.docx")).toBeNull();
    });
    expect(screen.getByRole("tab", { name: "整个工作区" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("工作区口径下的删除也带上工作区口径", async () => {
    vi.mocked(fetchAssets).mockResolvedValue({ assets: ASSETS });
    vi.mocked(deleteAsset).mockResolvedValue({ assets: [] });
    renderPanel();

    await screen.findByRole("button", { name: "img_1234567890ab.png" });
    fireEvent.click(screen.getByRole("tab", { name: "整个工作区" }));
    await waitFor(() => {
      expect(fetchAssets).toHaveBeenCalledWith("tok", SESSION_KEY, "workspace");
    });

    fireEvent.click((await screen.findAllByRole("button", { name: "删除" }))[0]);
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(deleteAsset).toHaveBeenCalledWith("tok", SESSION_KEY, "aW1nLnBuZw", "workspace");
    });
  });
});
