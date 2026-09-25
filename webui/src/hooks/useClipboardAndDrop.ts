import { useCallback, useRef, useState } from "react";

/** Extract attached ``File``s from a paste / drop event.
 *
 * Deliberate behaviour:
 *   - Only clipboard items whose ``kind === "file"`` are returned. ``<img>``
 *     tags inside an HTML fragment are ignored (defending against remote URL
 *     fetch + XSS surfaces), and so is a copied *link* — pasting a URL as text
 *     must keep reaching the textarea, never become an attachment (URL 由模型
 *     自己读并按 ``video-understanding`` 技能处理).
 *   - The MIME filter is gone: 图片之外的文档 / 视频 / 音频现在也能作为附件，
 *     由 ``useAttachedMedia`` 按扩展名分派并对白名单外的类型给出内联提示。
 *   - Plain text pasted alongside files is *not* consumed by this helper,
 *     so the caller can still let the textarea receive it naturally.
 */
export function extractFilesFromPaste(
  event: ClipboardEvent | React.ClipboardEvent,
): File[] {
  const clipboard = (event as ClipboardEvent).clipboardData
    ?? (event as React.ClipboardEvent).clipboardData;
  if (!clipboard) return [];
  const files: File[] = [];
  for (const item of Array.from(clipboard.items)) {
    if (item.kind !== "file") continue;
    const file = item.getAsFile();
    if (file) files.push(file);
  }
  return files;
}

/** Extract dropped files, mirroring ``extractFilesFromPaste``. */
export function extractFilesFromDrop(
  event: DragEvent | React.DragEvent,
): File[] {
  const dt = (event as DragEvent).dataTransfer
    ?? (event as React.DragEvent).dataTransfer;
  if (!dt) return [];
  return Array.from(dt.files);
}

export interface UseClipboardAndDropApi {
  /** Whether a drag is currently hovering the drop zone (toggle dragover UI). */
  isDragging: boolean;
  onPaste: (
    event: React.ClipboardEvent,
  ) => void;
  onDragEnter: (event: React.DragEvent) => void;
  onDragOver: (event: React.DragEvent) => void;
  onDragLeave: (event: React.DragEvent) => void;
  onDrop: (event: React.DragEvent) => void;
}

/** Wire paste + drag-and-drop to a callback.
 *
 * The hook owns ``isDragging`` state and the refcount that keeps it accurate
 * across nested ``dragenter`` / ``dragleave`` events (a known DOM gotcha: the
 * text cursor inside a textarea fires ``dragleave`` on entry, flicking the
 * highlight off otherwise). */
export function useClipboardAndDrop(
  onFiles: (files: File[]) => void,
): UseClipboardAndDropApi {
  const [isDragging, setIsDragging] = useState(false);
  const dragDepth = useRef(0);

  const onPaste = useCallback(
    (event: React.ClipboardEvent) => {
      const files = extractFilesFromPaste(event);
      if (files.length === 0) return;
      // Consume only when a file is actually present; plain-text paste still
      // reaches the textarea unmolested.
      event.preventDefault();
      onFiles(files);
    },
    [onFiles],
  );

  const onDragEnter = useCallback((event: React.DragEvent) => {
    if (!Array.from(event.dataTransfer.types ?? []).includes("Files")) return;
    event.preventDefault();
    dragDepth.current += 1;
    setIsDragging(true);
  }, []);

  const onDragOver = useCallback((event: React.DragEvent) => {
    if (!Array.from(event.dataTransfer.types ?? []).includes("Files")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  }, []);

  const onDragLeave = useCallback((event: React.DragEvent) => {
    if (!Array.from(event.dataTransfer.types ?? []).includes("Files")) return;
    event.preventDefault();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setIsDragging(false);
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      dragDepth.current = 0;
      setIsDragging(false);
      const files = extractFilesFromDrop(event);
      if (files.length === 0) return;
      event.preventDefault();
      onFiles(files);
    },
    [onFiles],
  );

  return { isDragging, onPaste, onDragEnter, onDragOver, onDragLeave, onDrop };
}
