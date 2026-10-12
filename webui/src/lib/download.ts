/**
 * 触发浏览器保存文件的统一下载入口。
 *
 * 优先 fetch 字节后走 blob 下载：能确认文件完整拿到，给调用方一个真实的
 * 成功信号（用于「下载成功」提示）。跨域等导致 fetch 失败时回退为直接打开
 * 原始链接（与原来 <a download> 行为一致），调用方给出「已开始下载」提示。
 */

export type DownloadOutcome = "saved" | "fallback";

function triggerAnchorDownload(url: string, name: string): void {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.rel = "noreferrer";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

export async function downloadUrlWithFeedback(url: string, name: string): Promise<DownloadOutcome> {
  try {
    const response = await fetch(url, { credentials: "same-origin" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    triggerAnchorDownload(objectUrl, name);
    // 延迟回收：给浏览器留出读取 blob 开始保存的时间。
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 10_000);
    return "saved";
  } catch {
    triggerAnchorDownload(url, name);
    return "fallback";
  }
}
