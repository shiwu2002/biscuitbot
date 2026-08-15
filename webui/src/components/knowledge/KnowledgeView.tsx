import { useCallback, useEffect, useRef, useState } from "react";
import {
  BookOpen,
  ChevronLeft,
  FileText,
  ImageIcon,
  Loader2,
  Trash2,
  Upload,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { deleteKnowledgeDocument, fetchKnowledgeDocuments } from "@/lib/api";
import type { KnowledgeDocument } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("read file failed"));
    reader.readAsDataURL(file);
  });
}

/**
 * 个人知识库视图（由主侧边栏「知识库」入口打开）。
 *
 * 上传文档/图片：文档在上传时抽取文本、图片由云端多模态模型生成描述，
 * 均建立本地 FTS 索引；智能体通过 ``search_knowledge`` 工具检索并读取。
 */
export function KnowledgeView({
  onBackToChat,
  hostChromeInset,
}: {
  onBackToChat: () => void;
  hostChromeInset?: boolean;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const { client, token } = useClient();

  const [docs, setDocs] = useState<KnowledgeDocument[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      const payload = await fetchKnowledgeDocuments(token);
      setDocs(payload.documents);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [token]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const payload = await fetchKnowledgeDocuments(token);
        if (!cancelled) setDocs(payload.documents);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const handleFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      for (const file of Array.from(files)) {
        const dataUrl = await readFileAsDataUrl(file);
        await client.uploadKnowledgeFile(file.name, dataUrl);
      }
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const handleDelete = async (doc: KnowledgeDocument) => {
    if (deleting) return;
    const ok = window.confirm(
      `${tx("knowledge.deleteConfirm", "确认删除")}「${doc.doc_id}」？`,
    );
    if (!ok) return;
    setDeleting(doc.doc_id);
    try {
      const payload = await deleteKnowledgeDocument(token, doc.doc_id);
      setDocs(payload.documents);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDeleting(null);
    }
  };

  return (
    <main className="min-w-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]">
      <div
        className={cn(
          "mx-auto w-full max-w-[920px] px-4 py-6 sm:px-8 sm:py-8 lg:py-12",
          hostChromeInset && "pt-[4.25rem] sm:pt-[4.25rem] lg:pt-[4.75rem]",
        )}
      >
        <div className="mb-7">
          <button
            type="button"
            onClick={onBackToChat}
            className="mb-4 inline-flex items-center gap-1.5 rounded-full px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground lg:hidden"
          >
            <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
            {t("settings.backToChat")}
          </button>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
            <div className="min-w-0">
              <h1 className="text-[24px] font-normal leading-tight tracking-tight text-foreground sm:text-[28px]">
                {tx("knowledge.title", "知识库")}
              </h1>
              <p className="mt-2 max-w-[680px] text-[13px] leading-5 text-muted-foreground">
                {tx(
                  "knowledge.description",
                  "上传文档或图片。文档会被抽取文本、图片由多模态模型生成描述并建立索引，智能体可通过 search_knowledge 检索并读取它们。",
                )}
              </p>
            </div>
            <Button
              type="button"
              size="sm"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
              className="h-9 shrink-0 rounded-[10px] px-4"
            >
              {uploading ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : (
                <Upload className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              )}
              {tx("knowledge.upload", "上传文件")}
            </Button>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => handleFiles(e.target.files)}
            />
          </div>
        </div>

        {loading ? (
          <div className="cyber-glass-panel relative flex h-48 items-center justify-center overflow-hidden rounded-[24px] text-sm text-muted-foreground">
            <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
            {tx("knowledge.loading", "加载知识库…")}
          </div>
        ) : error && !docs ? (
          <div className="cyber-glass-panel relative overflow-hidden rounded-[24px] px-5 py-4 text-sm text-muted-foreground">
            <span className="max-w-[520px]">{error}</span>
          </div>
        ) : null}

        {error && docs ? (
          <div className="mb-4 rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
            {error}
          </div>
        ) : null}

        {!loading && (docs?.length ?? 0) === 0 ? (
          <div className="cyber-glass-panel relative flex h-48 flex-col items-center justify-center gap-2 overflow-hidden rounded-[24px] border border-dashed border-border/60 text-sm text-muted-foreground">
            <BookOpen className="h-8 w-8 opacity-40" aria-hidden />
            {tx("knowledge.empty", "还没有上传任何文档。点击上方「上传文件」开始。")}
          </div>
        ) : null}

        {!loading && docs && docs.length > 0 ? (
          <div className="cyber-glass-panel relative overflow-hidden rounded-[24px]">
            <div className="divide-y divide-border/45">
              {docs.map((doc) => (
                <div
                  key={doc.doc_id}
                  className="flex min-h-[62px] flex-col gap-3 px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:px-5"
                >
                  <div className="flex min-w-0 items-center gap-2.5">
                    {doc.kind === "image" ? (
                      <ImageIcon className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
                    ) : (
                      <FileText className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
                    )}
                    <div className="min-w-0">
                      <div className="truncate text-[14px] font-medium leading-5 text-foreground">
                        {doc.doc_id}
                      </div>
                      <div className="mt-0.5 max-w-[28rem] truncate text-[12px] leading-5 text-muted-foreground">
                        {doc.kind === "image" && doc.caption
                          ? doc.caption
                          : `${tx("knowledge.size", "大小")} ${formatBytes(doc.size)}`}
                      </div>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2.5">
                    <span
                      className={cn(
                        "rounded-full px-1.5 py-0.5 text-[10px] font-semibold leading-none",
                        doc.kind === "image"
                          ? "bg-amber-500/10 text-amber-700 dark:text-amber-300"
                          : "bg-muted text-muted-foreground",
                      )}
                    >
                      {doc.kind === "image"
                        ? tx("knowledge.image", "图片")
                        : tx("knowledge.document", "文档")}
                    </span>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => handleDelete(doc)}
                      disabled={deleting === doc.doc_id}
                      className="h-8 rounded-[9px] text-[12.5px] text-destructive"
                    >
                      {deleting === doc.doc_id ? (
                        <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
                      ) : (
                        <Trash2 className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                      )}
                      {tx("knowledge.delete", "删除")}
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </main>
  );
}
