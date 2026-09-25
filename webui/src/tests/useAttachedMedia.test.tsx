import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { payloadBytesOf, useAttachedMedia } from "@/hooks/useAttachedMedia";
import type { Attachment } from "@/hooks/useAttachedMedia";
import type { EncodeResponse } from "@/lib/imageEncode";
import { MAX_IMAGES_PER_MESSAGE, UPLOAD_LIMITS } from "@/lib/media";

const encodeImage = vi.fn<(file: File) => Promise<EncodeResponse>>();

vi.mock("@/lib/imageEncode", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/imageEncode")>();
  return {
    ...actual,
    encodeImage: (file: File) => encodeImage(file),
  };
});

/** 一个普通文件：``type`` 故意留空——Windows 常这样上报，扩展名才是唯一线索。 */
function fileOf(name: string, type = "", size = 16): File {
  return new File([new Uint8Array(size)], name, { type });
}

/**
 * 排空 enqueue 排进微任务队列的解码（图片 Worker / FileReader）。
 * 不排空的话，解码回调里的 setState 会落在 act 之外，测试输出被警告淹没。
 */
async function settle() {
  await act(async () => {
    await Promise.resolve();
  });
}

function imageBytes(file: File): EncodeResponse {
  return {
    id: "stub",
    ok: true,
    dataUrl: `data:image/png;base64,${btoa(file.name)}`,
    mime: "image/png",
    bytes: file.size,
    origBytes: file.size,
    normalized: false,
  };
}

beforeEach(() => {
  encodeImage.mockReset();
  encodeImage.mockImplementation((file) => Promise.resolve(imageBytes(file)));
  let id = 0;
  (globalThis.URL as unknown as { createObjectURL: (b: Blob) => string }).createObjectURL =
    () => `blob:mock/${++id}`;
  (globalThis.URL as unknown as { revokeObjectURL: (u: string) => void }).revokeObjectURL =
    () => {};
});

describe("useAttachedMedia — 分派与限额", () => {
  it("按扩展名而不是 File.type 判定类别", async () => {
    const { result } = renderHook(() => useAttachedMedia());

    act(() => {
      // ``text/x-markdown`` / ``application/x-zip-compressed`` 都是 Windows 的
      // 说辞，后端白名单里没有这两个值——若原样发过去会被入口以 mime 拒收。
      result.current.enqueue([
        fileOf("notes.md", "text/x-markdown"),
        fileOf("bundle.zip", "application/x-zip-compressed"),
        fileOf("clip.mov", "video/quicktime"),
      ]);
    });

    expect(result.current.attachments.map((a) => [a.name, a.kind])).toEqual([
      ["notes.md", "document"],
      ["bundle.zip", "document"],
      ["clip.mov", "video"],
    ]);

    await settle();
  });

  it("拒绝白名单外的类型", () => {
    const { result } = renderHook(() => useAttachedMedia());
    const exe = fileOf("installer.exe", "application/x-msdownload");

    let rejected: Array<{ reason: string }> = [];
    act(() => {
      rejected = result.current.enqueue([exe]).rejected;
    });

    expect(rejected).toEqual([{ file: exe, reason: "unsupported_type" }]);
    expect(result.current.attachments).toHaveLength(0);
  });

  it("只允许 1 个视频", async () => {
    const { result } = renderHook(() => useAttachedMedia());

    let rejected: Array<{ reason: string }> = [];
    act(() => {
      result.current.enqueue([fileOf("a.mp4", "video/mp4")]);
    });
    act(() => {
      rejected = result.current.enqueue([fileOf("b.mp4", "video/mp4")]).rejected;
    });

    expect(rejected.map((r) => r.reason)).toEqual(["too_many_videos"]);
    expect(result.current.attachments).toHaveLength(1);

    await settle();
  });

  it("文档与音频共用 3 份的计数上限", async () => {
    const { result } = renderHook(() => useAttachedMedia());

    let rejected: Array<{ reason: string }> = [];
    act(() => {
      // 图片与视频不占这个计数：混在一起验证「共用一个桶」而不是各算各的。
      const { rejected: first } = result.current.enqueue([
        fileOf("a.png", "image/png"),
        fileOf("b.pdf", "application/pdf"),
        fileOf("c.docx"),
        fileOf("d.mp3", "audio/mpeg"),
      ]);
      rejected = first;
    });
    act(() => {
      rejected = result.current.enqueue([fileOf("e.xlsx")]).rejected;
    });

    expect(rejected.map((r) => r.reason)).toEqual(["too_many_documents"]);
    expect(result.current.attachments).toHaveLength(4);

    await settle();
  });

  it("超出单项体积的视频被拒，且不进附件列表", () => {
    const { result } = renderHook(() => useAttachedMedia());
    const huge = fileOf("big.mp4", "video/mp4", UPLOAD_LIMITS.maxVideoBytes + 1);

    let rejected: Array<{ reason: string }> = [];
    act(() => {
      rejected = result.current.enqueue([huge]).rejected;
    });

    expect(rejected.map((r) => r.reason)).toEqual(["too_large"]);
    expect(result.current.attachments).toHaveLength(0);
  });

  it("图片不受原始体积预检限制（Worker 会先压缩）", async () => {
    const { result } = renderHook(() => useAttachedMedia());
    // 40 MB 的 PNG 压缩后完全合规：拿原始体积预检会误杀它。
    const huge = fileOf("photo.png", "image/png", UPLOAD_LIMITS.maxTotalBytes + 1);

    act(() => {
      result.current.enqueue([huge]);
    });

    await waitFor(() => expect(result.current.attachments[0]?.status).toBe("ready"));
    expect(result.current.attachments[0].kind).toBe("image");
  });

  it("累计超出总量预算的非图片附件被拒", async () => {
    const { result } = renderHook(() => useAttachedMedia());

    act(() => {
      result.current.enqueue([fileOf("clip.mp4", "video/mp4", 18 * 1024 * 1024)]);
    });
    let rejected: Array<{ reason: string }> = [];
    act(() => {
      // 18 MB + 20 MB > 24 MB：逐项都合规，合起来装不进 WS 帧。
      rejected = result.current.enqueue([fileOf("deck.pdf", "application/pdf", 20 * 1024 * 1024)])
        .rejected;
    });

    expect(rejected.map((r) => r.reason)).toEqual(["too_large"]);

    await settle();
  });

  it("满额时 full 为真，但单一类别满额不阻塞其他类别", async () => {
    const { result } = renderHook(() => useAttachedMedia());

    act(() => {
      result.current.enqueue(
        Array.from({ length: MAX_IMAGES_PER_MESSAGE }, (_, i) => fileOf(`${i}.png`, "image/png")),
      );
    });

    expect(result.current.full).toBe(false);
    let rejected: Array<{ reason: string }> = [];
    act(() => {
      rejected = result.current
        .enqueue([fileOf("extra.png", "image/png"), fileOf("ok.pdf", "application/pdf")])
        .rejected;
    });

    expect(rejected.map((r) => r.reason)).toEqual(["too_many_images"]);
    expect(result.current.attachments.map((a) => a.name)).toContain("ok.pdf");

    await settle();
  });
});

describe("useAttachedMedia — 只接受本地上传", () => {
  // 「视频直链」附件已下线：链接写在消息正文里即可，由模型自己读并按
  // ``video-understanding`` 技能处理。这两条钉住「不要把它加回来」。
  it("不再暴露直链入口", () => {
    const { result } = renderHook(() => useAttachedMedia());

    expect("addUrl" in result.current).toBe(false);
    expect(result.current.attachments).toHaveLength(0);
  });

  it("载荷一律按本地字节计算（没有恒为 0 的直链条目）", () => {
    const attachment: Attachment = {
      id: "a",
      kind: "video",
      name: "v.mp4",
      size: 1234,
      status: "ready",
    };

    expect(payloadBytesOf(attachment)).toBe(1234);
  });
});

describe("useAttachedMedia — 载荷与预算", () => {
  it("图片走 Worker，非图片直读并把 MIME 改写成规范值", async () => {
    const { result } = renderHook(() => useAttachedMedia());

    act(() => {
      result.current.enqueue([
        fileOf("a.png", "image/png"),
        // 浏览器可能报 application/octet-stream，规范值应为 application/pdf。
        fileOf("spec.pdf", "application/octet-stream"),
      ]);
    });

    await waitFor(() =>
      expect(result.current.attachments.every((a) => a.status === "ready")).toBe(true),
    );

    const [image, pdf] = result.current.attachments;
    expect(encodeImage).toHaveBeenCalledTimes(1);
    expect(encodeImage.mock.calls[0][0].name).toBe("a.png");
    // 文档不走 Worker，只有 FileReader + 前缀改写。
    expect(pdf.dataUrl?.startsWith("data:application/pdf;base64,")).toBe(true);
    expect(image.dataUrl?.startsWith("data:image/png;base64,")).toBe(true);
  });

  it("plannedBytes 用归一化后的体积计图片、原始体积计其余", async () => {
    const { result } = renderHook(() => useAttachedMedia());
    const png = fileOf("huge.png", "image/png", 5_000);
    // Worker 把 5000 字节压到 100 字节——预算必须按后者算。
    encodeImage.mockImplementationOnce(() =>
      Promise.resolve({ ...imageBytes(png), bytes: 100 }),
    );

    act(() => {
      result.current.enqueue([png, fileOf("doc.pdf", "application/pdf", 2_048)]);
    });

    await waitFor(() =>
      expect(result.current.attachments.every((a) => a.status === "ready")).toBe(true),
    );

    expect(result.current.plannedBytes).toBe(100 + 2_048);
  });

  it("读取失败时给出 io 错误而不是静默留下半成品", async () => {
    const readAsDataURL = vi
      .spyOn(FileReader.prototype, "readAsDataURL")
      .mockImplementation(function (this: FileReader) {
        queueMicrotask(() => this.onerror?.(new ProgressEvent("error") as ProgressEvent<FileReader>));
      });
    const { result } = renderHook(() => useAttachedMedia());

    act(() => {
      result.current.enqueue([fileOf("doc.pdf", "application/pdf")]);
    });

    await waitFor(() => expect(result.current.attachments[0]?.status).toBe("error"));
    expect(result.current.attachments[0].error).toBe("io");
    // 失败的条目绝不能进载荷——否则 submit 会发出一个空的 data_url。
    expect(result.current.attachments[0].dataUrl).toBeUndefined();

    readAsDataURL.mockRestore();
  });
});
