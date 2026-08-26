/**
 * Pure helpers for inline document previews in chat messages.
 *
 * Rendering happens entirely in the browser: the signed media URL is fetched,
 * decoded, and parsed client-side, so no backend changes (or server-side
 * LibreOffice) are required — which matters for the desktop bundle where no
 * document converters ship.
 */

export type DocumentPreviewKind = "pptx" | "docx" | "xlsx" | "csv" | "html";

/** 预览失败的错误码：太大 / 拉取失败 / 解析渲染失败。 */
export type PreviewErrorCode = "too-large" | "fetch" | "parse";

/**
 * 从文件名推断文档预览类型；不支持的类型返回 null（保持下载 chip）。
 * 大小写不敏感；name 缺失时回退到 url（url 可能带查询参数，需先剥离）。
 */
export function getDocumentPreviewKind(name?: string): DocumentPreviewKind | null {
  if (!name) return null;
  const base = name.split(/[?#]/, 1)[0] ?? "";
  const dot = base.lastIndexOf(".");
  if (dot < 0) return null;
  const suffix = base.slice(dot).toLowerCase();
  switch (suffix) {
    case ".pptx":
      return "pptx";
    case ".docx":
      return "docx";
    case ".xlsx":
      return "xlsx";
    case ".csv":
      return "csv";
    case ".html":
    case ".htm":
      return "html";
    default:
      return null;
  }
}

/**
 * 极简 CSV 解析（RFC-4180 子集）：支持引号字段、`""` 转义、CRLF/LF。
 * 对畸形输入容错：未闭合引号把剩余行当作一个字段，末尾空行丢弃。
 */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  let i = 0;
  const n = text.length;
  while (i < n) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 2;
        } else {
          inQuotes = false;
          i += 1;
        }
      } else {
        field += ch;
        i += 1;
      }
      continue;
    }
    if (ch === '"') {
      inQuotes = true;
      i += 1;
    } else if (ch === ",") {
      row.push(field);
      field = "";
      i += 1;
    } else if (ch === "\r") {
      if (text[i + 1] === "\n") i += 2;
      else i += 1;
      row.push(field);
      field = "";
      rows.push(row);
      row = [];
    } else if (ch === "\n") {
      i += 1;
      row.push(field);
      field = "";
      rows.push(row);
      row = [];
    } else {
      field += ch;
      i += 1;
    }
  }
  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  // 丢弃末尾的空白行（多个连续换行产生的空数组）
  while (rows.length > 0 && rows[rows.length - 1].every((cell) => cell === "")) {
    rows.pop();
  }
  return rows;
}

/** 表格单元格显示归一化（xlsx 与 csv 共用）。 */
export function formatCellValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (value instanceof Date) return value.toLocaleString();
  return String(value);
}

/** 预览文件体积上限，超过则拒绝并提示下载。 */
export const MAX_PREVIEW_BYTES = 25 * 1024 * 1024; // 25 MiB

export class PreviewTooLargeError extends Error {
  constructor() {
    super("file too large to preview");
    this.name = "PreviewTooLargeError";
  }
}

const bufferCache = new Map<string, Promise<ArrayBuffer>>();

/**
 * 拉取签名媒体 URL 的原始字节，并按 url 缓存（重复展开/重挂载不重下载，
 * 并发展开共享同一次请求）。失败不缓存，允许重试；超限抛
 * {@link PreviewTooLargeError} 且不缓存。
 */
export function fetchMediaBuffer(url: string): Promise<ArrayBuffer> {
  const cached = bufferCache.get(url);
  if (cached) return cached;

  const promise = (async () => {
    const resp = await fetch(url);
    if (!resp.ok) {
      throw new Error(`failed to fetch media: HTTP ${resp.status}`);
    }
    const lengthHeader = resp.headers.get("content-length");
    if (lengthHeader && Number(lengthHeader) > MAX_PREVIEW_BYTES) {
      throw new PreviewTooLargeError();
    }
    const buffer = await resp.arrayBuffer();
    if (buffer.byteLength > MAX_PREVIEW_BYTES) {
      throw new PreviewTooLargeError();
    }
    return buffer;
  })();

  bufferCache.set(url, promise);
  promise.catch(() => {
    if (bufferCache.get(url) === promise) bufferCache.delete(url);
  });
  return promise;
}

/** 测试钩子：清空已缓存的字节。 */
export function clearMediaBufferCache(): void {
  bufferCache.clear();
}
