import type { OutboundMedia, UIMediaAttachment, UIMediaKind } from "@/lib/types";

const IMAGE_EXTENSIONS = new Set([
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".bmp",
  ".ico",
  ".svg",
  ".tif",
  ".tiff",
]);

// 只保留后端 ``_VIDEO_MIME_ALLOWED`` 认下的四种。``.avi/.mkv/.3gp`` 曾经也
// 在这里，但它让「前端渲染成正常视频 chip」变成假象——提交后会被 WS 入口以
// ``mime`` 拒收。降级为 ``file`` 后前端所见即后端所收。
const VIDEO_EXTENSIONS = new Set([".mp4", ".webm", ".mov", ".m4v"]);

/** 上传附件类别——与后端 ``channels/websocket.py`` 的四张 MIME 白名单一一对应。 */
export type UploadKind = "image" | "video" | "document" | "audio";

/** 上传限额——与 ``xianaibot/channels/websocket.py`` 的常量逐项对齐。
 *
 * 前端预检**只为**把「超限」翻译成可本地化的内联提示：真正的权威是后端。
 * 尤其 ``maxTotalBytes``：逐项限额的组合（1 视频 + 4 图片）base64 后能超过
 * WS 帧上限，而那是 websockets 库在 handler 之前以协议级 close 断开的——
 * 前端只会看到掉线，拿不到任何错误文案。所以必须在发帧之前拦住。 */
export const UPLOAD_LIMITS = {
  maxImages: 4,
  maxImageBytes: 6 * 1024 * 1024,
  maxVideos: 1,
  maxVideoBytes: 18 * 1024 * 1024,
  /** 文档与音频**共用一个计数**（后端 ``doc_count`` 也是合并计数的）。 */
  maxDocuments: 3,
  maxDocumentBytes: 20 * 1024 * 1024,
  /** 本条消息所有内联附件原始字节之和。 */
  maxTotalBytes: 24 * 1024 * 1024,
} as const;

export const MAX_IMAGES_PER_MESSAGE = UPLOAD_LIMITS.maxImages;

/** 扩展名 → 规范 MIME。**扩展名优先于 ``File.type``**：Windows 常把 ``.md``
 * 报成 ``text/x-markdown``、``.zip`` 报成 ``application/x-zip-compressed``，
 * 两者都不在后端白名单里——原样发过去会被入口以 ``mime`` 拒收，而用户明明
 * 选的是一个受支持的文件。表内的值逐项对齐后端 ``_*_MIME_ALLOWED``。
 *
 * ``.webm`` 可能是音频也可能是视频，这里归入视频（后端两张白名单都收，
 * 只是计数口径不同——视频更严格，归入视频是更安全的默认）。 */
const EXTENSION_MIME: Readonly<Record<string, string>> = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".gif": "image/gif",
  ".mp4": "video/mp4",
  ".webm": "video/webm",
  ".mov": "video/quicktime",
  ".m4v": "video/x-m4v",
  ".pdf": "application/pdf",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".txt": "text/plain",
  ".md": "text/markdown",
  ".markdown": "text/markdown",
  ".csv": "text/csv",
  ".json": "application/json",
  ".zip": "application/zip",
  ".mp3": "audio/mpeg",
  ".m4a": "audio/x-m4a",
  ".wav": "audio/wav",
  ".ogg": "audio/ogg",
  ".aac": "audio/aac",
};

const MIME_KIND: Readonly<Record<string, UploadKind>> = {
  "image/png": "image",
  "image/jpeg": "image",
  "image/webp": "image",
  "image/gif": "image",
  "video/mp4": "video",
  "video/webm": "video",
  "video/quicktime": "video",
  "video/x-m4v": "video",
  "audio/mpeg": "audio",
  "audio/mp4": "audio",
  "audio/x-m4a": "audio",
  "audio/wav": "audio",
  "audio/x-wav": "audio",
  "audio/webm": "audio",
  "audio/ogg": "audio",
  "audio/aac": "audio",
};

/** 上传文件选择框的 ``accept``——直接由 ``EXTENSION_MIME`` 生成，保证「能选中
 * 的」与「发得出去的」是同一份集合（历史上两者曾经漂移）。 */
export const UPLOAD_ACCEPT_ATTR = Object.keys(EXTENSION_MIME).sort().join(",");

/** 该文件对应的规范 MIME；不在白名单内返回 ``null``。 */
export function uploadMimeFor(file: File): string | null {
  const ext = extensionOf(file.name);
  const byExtension = ext ? EXTENSION_MIME[ext] : undefined;
  if (byExtension) return byExtension;
  // 扩展名未知（或无扩展名）时才退回浏览器给的 type，且仍须在白名单内。
  const declared = (file.type || "").toLowerCase();
  return MIME_KIND[declared] ? declared : null;
}

/** 该文件会被当作哪一类附件上传；不在白名单内返回 ``null``。 */
export function uploadKindFor(file: File): UploadKind | null {
  const mime = uploadMimeFor(file);
  return mime ? MIME_KIND[mime] ?? (mime.startsWith("image/") ? "image" : "document") : null;
}

function cleanPath(value: string): string {
  return value.split(/[?#]/, 1)[0]?.toLowerCase() ?? "";
}

function extensionOf(value?: string): string {
  if (!value) return "";
  const path = cleanPath(value);
  const dot = path.lastIndexOf(".");
  if (dot < 0) return "";
  return path.slice(dot);
}

function explicitMediaKind(media: { url?: string; name?: string }): UIMediaKind | null {
  const url = media.url ?? "";
  if (url.startsWith("data:image/")) return "image";
  if (url.startsWith("data:video/")) return "video";

  const ext = extensionOf(media.name) || extensionOf(url);
  if (!ext) return null;
  if (IMAGE_EXTENSIONS.has(ext)) return "image";
  if (VIDEO_EXTENSIONS.has(ext)) return "video";
  return "file";
}

export function inferMediaKind(media: { url?: string; name?: string }): UIMediaKind {
  return explicitMediaKind(media) ?? "file";
}

export function toMediaAttachment(media: {
  url?: string;
  name?: string;
  kind?: UIMediaKind;
}): UIMediaAttachment {
  return {
    kind: explicitMediaKind(media) ?? media.kind ?? "file",
    url: media.url,
    name: media.name,
  };
}

/** 上传类别 → 展示类别。``UIMediaKind`` 没有 document/audio，统一归 ``file``
 * （``AttachmentTile`` 的 file 分支会按扩展名决定是否给「预览」按钮）。 */
export function toUIMediaKind(kind: UploadKind): UIMediaKind {
  if (kind === "image") return "image";
  if (kind === "video") return "video";
  return "file";
}

/** 附件 → 上帧载荷。缺载荷时返回 ``null``（调用方应跳过它而不是发空条目）。 */
export function toOutboundMedia(item: {
  dataUrl?: string;
  name?: string;
}): OutboundMedia | null {
  if (!item.dataUrl) return null;
  return { kind: "data", data_url: item.dataUrl, ...(item.name ? { name: item.name } : {}) };
}

/** 附件 → 乐观气泡预览地址（本地编码后的 data URL）。 */
export function toPreviewUrl(item: { dataUrl?: string }): string | undefined {
  return item.dataUrl;
}
