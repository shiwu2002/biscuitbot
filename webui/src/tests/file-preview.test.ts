import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearMediaBufferCache,
  fetchMediaBuffer,
  formatCellValue,
  getDocumentPreviewKind,
  MAX_PREVIEW_BYTES,
  parseCsv,
  PreviewTooLargeError,
} from "@/lib/file-preview";

beforeEach(() => clearMediaBufferCache());
afterEach(() => vi.unstubAllGlobals());

describe("getDocumentPreviewKind", () => {
  it("maps supported extensions case-insensitively", () => {
    expect(getDocumentPreviewKind("deck.pptx")).toBe("pptx");
    expect(getDocumentPreviewKind("report.DOCX")).toBe("docx");
    expect(getDocumentPreviewKind("data.xlsx")).toBe("xlsx");
    expect(getDocumentPreviewKind("data.csv")).toBe("csv");
    expect(getDocumentPreviewKind("page.html")).toBe("html");
    expect(getDocumentPreviewKind("page.htm")).toBe("html");
  });

  it("returns null for unsupported extensions and empty names", () => {
    expect(getDocumentPreviewKind("doc.pdf")).toBeNull();
    expect(getDocumentPreviewKind("archive.zip")).toBeNull();
    expect(getDocumentPreviewKind("noext")).toBeNull();
    expect(getDocumentPreviewKind("")).toBeNull();
    expect(getDocumentPreviewKind(undefined)).toBeNull();
  });

  it("strips query strings from urls before matching", () => {
    expect(getDocumentPreviewKind("/api/media/sig/data.csv?x=1&y=2")).toBe("csv");
  });
});

describe("parseCsv", () => {
  it("parses basic rows", () => {
    expect(parseCsv("a,b\n1,2")).toEqual([
      ["a", "b"],
      ["1", "2"],
    ]);
  });

  it("handles CRLF and trailing newline", () => {
    expect(parseCsv("a,b\r\n1,2\r\n")).toEqual([
      ["a", "b"],
      ["1", "2"],
    ]);
  });

  it("handles quoted fields with commas and escaped quotes", () => {
    expect(parseCsv('a,"x,y","he said ""hi"""\n1,2,3')).toEqual([
      ["a", "x,y", 'he said "hi"'],
      ["1", "2", "3"],
    ]);
  });

  it("drops trailing blank rows", () => {
    expect(parseCsv("a,b\n\n\n")).toEqual([["a", "b"]]);
  });

  it("recovers from an unclosed quote", () => {
    expect(parseCsv('a,"unclosed\nb,c')).toEqual([["a", "unclosed\nb,c"]]);
  });

  it("returns [] for empty input", () => {
    expect(parseCsv("")).toEqual([]);
  });
});

describe("formatCellValue", () => {
  it("normalizes nullish, primitives and dates", () => {
    expect(formatCellValue(null)).toBe("");
    expect(formatCellValue(undefined)).toBe("");
    expect(formatCellValue(true)).toBe("true");
    expect(formatCellValue(42)).toBe("42");
    expect(formatCellValue("x")).toBe("x");
    const date = new Date("2026-08-24T00:00:00Z");
    expect(formatCellValue(date)).toBe(date.toLocaleString());
  });
});

describe("fetchMediaBuffer", () => {
  it("fetches and caches by url", async () => {
    const fetchMock = vi.fn(async () => new Response(new ArrayBuffer(4)));
    vi.stubGlobal("fetch", fetchMock);
    const first = await fetchMediaBuffer("/m/a");
    const second = await fetchMediaBuffer("/m/a");
    expect(first.byteLength).toBe(4);
    expect(second).toBe(first);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("throws PreviewTooLargeError on oversized content-length and does not cache", async () => {
    const oversized = String(MAX_PREVIEW_BYTES + 1);
    const fetchMock = vi.fn(
      async () => new Response(new ArrayBuffer(8), { headers: { "content-length": oversized } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchMediaBuffer("/m/big")).rejects.toBeInstanceOf(PreviewTooLargeError);
    // 失败不缓存，可重试
    await expect(fetchMediaBuffer("/m/big")).rejects.toBeInstanceOf(PreviewTooLargeError);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("throws PreviewTooLargeError when the actual body exceeds the limit", async () => {
    const fetchMock = vi.fn(
      async () => new Response(new ArrayBuffer(MAX_PREVIEW_BYTES + 1)),
    );
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchMediaBuffer("/m/big2")).rejects.toBeInstanceOf(PreviewTooLargeError);
  });

  it("rejects when the response is not ok", async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchMediaBuffer("/m/missing")).rejects.toThrow("HTTP 404");
  });
});
