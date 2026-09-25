import { useCallback, useEffect, useRef, useState } from "react";

import { encodeImage, type EncodeFailure } from "@/lib/imageEncode";
import {
  MAX_IMAGES_PER_MESSAGE,
  UPLOAD_LIMITS,
  uploadKindFor,
  uploadMimeFor,
  type UploadKind,
} from "@/lib/media";

/** Lifecycle stages of one attachment:
 *
 * - ``encoding``  — being read/compressed; chip shows a spinner
 * - ``ready``     — ``dataUrl`` available; safe to submit
 * - ``error``     — validation / decode failure; chip shows inline error
 */
export type AttachmentStatus = "encoding" | "ready" | "error";

export interface Attachment {
  id: string;
  kind: UploadKind;
  /** 该项持有的原始文件。 */
  file?: File;
  /** 展示用文件名。 */
  name: string;
  /** 原始字节数。 */
  size: number;
  /** chip 缩略图用的 ``blob:`` URL；撤销点见 ``remove`` / ``clear`` / unmount。 */
  previewUrl?: string;
  status: AttachmentStatus;
  /** ``status === "ready"`` 时才有：真正会上帧的载荷。 */
  dataUrl?: string;
  /** 载荷解码后的字节数（图片是 Worker 归一化后的体积）。 */
  encodedBytes?: number;
  /** 图片是否被 Worker 重新编码以命中体积预算。 */
  normalized?: boolean;
  error?: AttachmentError;
}

export interface RestoredReadyImage {
  dataUrl: string;
  name?: string;
}

export interface RejectedFile {
  file: File;
  reason: AttachmentError;
}

/** Machine-readable rejection reasons surfaced as inline chip errors.
 *
 * Callers localize these via the ``thread.composer.imageRejected.*`` i18n table. */
export type AttachmentError =
  | "unsupported_type"     // 不在后端上传白名单内
  | "too_many_images"      // 每消息图片数上限（4）
  | "too_many_videos"      // 每消息视频数上限（1）
  | "too_many_documents"   // 文档与音频共用的计数上限（3）
  | "magic_mismatch"       // 扩展名与真实内容不符
  | "decode_failed"        // Worker 无法解码 / 重新编码
  | "too_large"            // 单项或本条消息附件总量超限
  | "io";                  // 浏览器层读文件失败

export { MAX_IMAGES_PER_MESSAGE };

function uuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return (crypto as Crypto).randomUUID();
  }
  return `att-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function dataUrlMime(dataUrl: string): string {
  const match = /^data:([^;,]+)[;,]/.exec(dataUrl);
  return match?.[1] || "image/png";
}

function dataUrlToFile(dataUrl: string, name?: string): File {
  const mime = dataUrlMime(dataUrl);
  const fallbackName = `image.${mime.split("/")[1] || "png"}`;
  try {
    const [, base64 = ""] = dataUrl.split(",", 2);
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return new File([bytes], name || fallbackName, { type: mime });
  } catch {
    return new File([], name || fallbackName, { type: mime });
  }
}

function mapEncodeFailure(reason: EncodeFailure["reason"]): AttachmentError {
  switch (reason) {
    case "invalid_mime":
    case "magic_mismatch":
      return "magic_mismatch";
    case "too_large_after_normalize":
      return "too_large";
    case "io":
      return "io";
    case "decode_failed":
    default:
      return "decode_failed";
  }
}

/** 把 ``FileReader`` 产出的 data URL 的 MIME 段改写成我们认定的规范值。
 *
 * ``FileReader.readAsDataURL`` 用的是 ``File.type``（Windows 上常是空串或
 * ``text/x-markdown`` 这类后端不认的值），而协议另一头是按 ``data:<mime>``
 * 判白名单的。原地改写前缀避免了为 18 MB 视频再做一次 base64 编码。 */
function rewriteDataUrlMime(dataUrl: string, mime: string): string {
  return dataUrl.replace(/^data:[^;,]*(?=[;,])/, `data:${mime}`);
}

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

/** 该附件**会占用的**载荷字节数。
 *
 * 图片取 Worker 归一化后的体积（原始素材可能有 40 MB，压缩后只有 3 MB），
 * 其余取原始体积（它们不做任何压缩）。这个和必须与后端 ``planned_bytes``
 * 口径一致，否则前端会放过一个必然被协议级断开的组合。 */
export function payloadBytesOf(attachment: Attachment): number {
  return attachment.encodedBytes ?? attachment.size;
}

export interface UseAttachedMediaApi {
  attachments: Attachment[];
  /** Enqueue local files. Rejected entries are returned so the caller can
   * surface inline errors; they are *not* added to ``attachments``. */
  enqueue: (files: Iterable<File>) => { rejected: RejectedFile[] };
  remove: (id: string) => { nextFocusId: string | null };
  /** Revoke every staged blob URL and drop all attachments. Called after a
   * successful submit — the optimistic bubble holds onto an independent
   * ``data:`` URL so tearing down blob previews here is safe. */
  clear: () => void;
  /** Restore already-encoded images, e.g. a queued composer draft moving back
   * into the input. These entries are immediately sendable and use their
   * ``data:`` URL as a stable preview. */
  restoreReadyImages: (images: RestoredReadyImage[]) => void;
  /** ``true`` while at least one attachment is still being read — Send waits. */
  encoding: boolean;
  /** ``true`` once **every** kind has hit its cap (the attach button greys out;
   * hitting one kind's cap surfaces an inline error instead, so a full image
   * row never blocks attaching a document). */
  full: boolean;
  /** 本条消息附件的载荷字节总和，用于发送前预检。 */
  plannedBytes: number;
}

/** Manage the lifecycle of files attached to the Composer.
 *
 * One hook for all upload kinds rather than one per kind: the counts are
 * interleaved (documents and audio share a counter), the total-byte budget
 * spans them all, and the chips render in a single row.
 *
 * Responsibilities in one place:
 *   - validation (extension→MIME whitelist, per-kind caps, total budget)
 *   - blob URL creation + revocation
 *   - Worker orchestration for images, direct reads for everything else
 *   - focus bookkeeping so keyboard delete doesn't strand the user
 */
export function useAttachedMedia(): UseAttachedMediaApi {
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  // Ref mirror so ``enqueue`` can see the authoritative state when invoked
  // multiple times in a single tick (rapid file selection, drag of many
  // files, paste storms). ``state`` is stale for that second + call.
  const attachmentsRef = useRef<Attachment[]>([]);
  attachmentsRef.current = attachments;

  const setEntry = useCallback((id: string, patch: Partial<Attachment>) => {
    setAttachments((prev) => {
      const next = prev.map((item) => (item.id === id ? { ...item, ...patch } : item));
      attachmentsRef.current = next;
      return next;
    });
  }, []);

  const enqueue = useCallback(
    (files: Iterable<File>) => {
      const rejected: RejectedFile[] = [];
      const toAdd: Attachment[] = [];
      let imageCount = 0;
      let videoCount = 0;
      // 文档与音频共用一个计数——后端 ``doc_count`` 也是合并计数的。
      let documentCount = 0;
      let bytes = 0;
      for (const item of attachmentsRef.current) {
        if (item.kind === "image") imageCount += 1;
        else if (item.kind === "video") videoCount += 1;
        else documentCount += 1;
        bytes += payloadBytesOf(item);
      }

      for (const file of files) {
        const kind = uploadKindFor(file);
        const mime = uploadMimeFor(file);
        if (!kind || !mime) {
          rejected.push({ file, reason: "unsupported_type" });
          continue;
        }
        if (kind === "image") {
          if (imageCount >= UPLOAD_LIMITS.maxImages) {
            rejected.push({ file, reason: "too_many_images" });
            continue;
          }
          imageCount += 1;
        } else if (kind === "video") {
          if (videoCount >= UPLOAD_LIMITS.maxVideos) {
            rejected.push({ file, reason: "too_many_videos" });
            continue;
          }
          if (file.size > UPLOAD_LIMITS.maxVideoBytes) {
            rejected.push({ file, reason: "too_large" });
            continue;
          }
          videoCount += 1;
        } else {
          if (documentCount >= UPLOAD_LIMITS.maxDocuments) {
            rejected.push({ file, reason: "too_many_documents" });
            continue;
          }
          if (file.size > UPLOAD_LIMITS.maxDocumentBytes) {
            rejected.push({ file, reason: "too_large" });
            continue;
          }
          documentCount += 1;
        }
        // 图片不进总量预算：Worker 会把它压到 ≤6 MB，原始体积不代表最终
        // 载荷，拿它预检会把「一张 40 MB 的 PNG 压缩后完全合规」误判为超限。
        if (kind !== "image") {
          if (bytes + file.size > UPLOAD_LIMITS.maxTotalBytes) {
            rejected.push({ file, reason: "too_large" });
            continue;
          }
          bytes += file.size;
        }
        toAdd.push({
          id: uuid(),
          kind,
          file,
          name: file.name,
          size: file.size,
          previewUrl: URL.createObjectURL(file),
          status: "encoding",
        });
      }

      if (toAdd.length > 0) {
        const next = [...attachmentsRef.current, ...toAdd];
        attachmentsRef.current = next;
        setAttachments(next);
        // Fire the readers after the commit so chips render first (good INP).
        for (const entry of toAdd) {
          const mime = entry.file ? uploadMimeFor(entry.file) : null;
          queueMicrotask(() => {
            if (entry.kind === "image" && entry.file) {
              encodeImage(entry.file).then(
                (result) => {
                  if (result.ok) {
                    setEntry(entry.id, {
                      status: "ready",
                      dataUrl: result.dataUrl,
                      encodedBytes: result.bytes,
                      normalized: result.normalized,
                    });
                  } else {
                    setEntry(entry.id, {
                      status: "error",
                      error: mapEncodeFailure(result.reason),
                    });
                  }
                },
                () => {
                  setEntry(entry.id, { status: "error", error: "decode_failed" });
                },
              );
              return;
            }
            // 视频 / 文档 / 音频不做压缩，直接读原始字节——因此 loader 也要
            // 改成 data URL，而不是走 Worker。
            if (!entry.file || !mime) {
              setEntry(entry.id, { status: "error", error: "unsupported_type" });
              return;
            }
            readFileAsDataUrl(entry.file).then(
              (dataUrl) =>
                setEntry(entry.id, {
                  status: "ready",
                  dataUrl: rewriteDataUrlMime(dataUrl, mime),
                  encodedBytes: entry.file?.size,
                  normalized: false,
                }),
              () => setEntry(entry.id, { status: "error", error: "io" }),
            );
          });
        }
      }
      return { rejected };
    },
    [setEntry],
  );

  const remove = useCallback((id: string) => {
    let nextFocusId: string | null = null;
    setAttachments((prev) => {
      const idx = prev.findIndex((item) => item.id === id);
      if (idx === -1) return prev;
      const target = prev[idx];
      if (target.previewUrl) {
        try {
          URL.revokeObjectURL(target.previewUrl);
        } catch {
          // No-op: previewUrl revocation is best-effort.
        }
      }
      const next = [...prev.slice(0, idx), ...prev.slice(idx + 1)];
      attachmentsRef.current = next;
      // Prefer moving focus to the chip at the same index, else previous.
      const candidate = next[idx] ?? next[idx - 1];
      nextFocusId = candidate?.id ?? null;
      return next;
    });
    return { nextFocusId };
  }, []);

  const clear = useCallback(() => {
    setAttachments((prev) => {
      for (const item of prev) {
        if (!item.previewUrl) continue;
        try {
          URL.revokeObjectURL(item.previewUrl);
        } catch {
          // revoke is best-effort
        }
      }
      attachmentsRef.current = [];
      return [];
    });
  }, []);

  const restoreReadyImages = useCallback((restored: RestoredReadyImage[]) => {
    const toRestore = restored
      .map((img) => ({ img, file: dataUrlToFile(img.dataUrl, img.name) }))
      .filter(({ file }) => uploadKindFor(file) === "image")
      .slice(0, UPLOAD_LIMITS.maxImages)
      .map(({ img, file }): Attachment => ({
        id: uuid(),
        kind: "image",
        file,
        name: file.name,
        size: file.size,
        previewUrl: img.dataUrl,
        status: "ready",
        dataUrl: img.dataUrl,
        encodedBytes: file.size,
      }));
    setAttachments((prev) => {
      for (const item of prev) {
        if (!item.previewUrl) continue;
        try {
          URL.revokeObjectURL(item.previewUrl);
        } catch {
          // revoke is best-effort
        }
      }
      attachmentsRef.current = toRestore;
      return toRestore;
    });
  }, []);

  // Final safety net: revoke any outstanding blob URLs on unmount. Safe
  // under StrictMode double-invoke because revoked blob URLs are only
  // referenced from in-hook chip state, which is rebuilt on remount.
  useEffect(() => {
    return () => {
      for (const item of attachmentsRef.current) {
        if (!item.previewUrl) continue;
        try {
          URL.revokeObjectURL(item.previewUrl);
        } catch {
          // best-effort cleanup on unmount
        }
      }
    };
  }, []);

  const encoding = attachments.some((item) => item.status === "encoding");
  const plannedBytes = attachments.reduce((sum, item) => sum + payloadBytesOf(item), 0);
  const imagesFull = attachments.filter((item) => item.kind === "image").length
    >= UPLOAD_LIMITS.maxImages;
  const videosFull = attachments.filter((item) => item.kind === "video").length
    >= UPLOAD_LIMITS.maxVideos;
  const documentsFull = attachments.filter(
    (item) => item.kind !== "image" && item.kind !== "video",
  ).length >= UPLOAD_LIMITS.maxDocuments;
  const full = imagesFull && videosFull && documentsFull;

  return {
    attachments,
    enqueue,
    remove,
    clear,
    restoreReadyImages,
    encoding,
    full,
    plannedBytes,
  };
}
