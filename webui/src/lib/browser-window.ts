/**
 * 桌面壳「内置浏览器」独立窗口的命令封装。
 *
 * 形态：**顶层原生窗口**（不是主窗口里的子 webview），自带标题栏，可拖动、
 * 缩放、贴边吸附，因此能与主窗口并排同屏。窗口的位置与尺寸完全交给操作系统，
 * 前端不再测量任何矩形——从根上避免了「原生子 webview 与占位元素错位 →
 * 矩形盖住界面吞掉点击」这类问题。
 *
 * 主界面只保留一条紧凑控制栏（地址输入 / 后退 / 前进 / 刷新 / 系统浏览器打开 /
 * 关闭），通过这里的 invoke 驱动那个窗口。浏览器本体（含登录态与 CDP 调试端口）
 * 仍由 Rust 壳负责，gateway 的浏览器管理器照旧以 embedded 模式附着，因此
 * 智能体自动化与用户看到的是同一个浏览器实例。
 *
 * 非桌面端（浏览器直接访问 WebUI）没有 `window.__TAURI__`，这些函数不适用，
 * 该场景继续走「驾驶舱页」（BrowserView）那套托管实例控制。
 */

type TauriInvoke = (cmd: string, args?: unknown) => Promise<unknown>;

/** 桌面壳（withGlobalTauri）暴露的 invoke；浏览器宿主下为 null。 */
function tauriInvoke(): TauriInvoke | null {
  if (typeof window === "undefined") return null;
  return window.__TAURI__?.core?.invoke ?? null;
}

/** 当前宿主是否支持独立浏览器窗口（只有桌面端支持）。 */
export function supportsBrowserWindow(): boolean {
  return tauriInvoke() !== null;
}

/** 打开（或聚焦）内置浏览器窗口；`url` 为空时沿用窗口当前页面。 */
export async function openBrowserWindow(url?: string | null): Promise<void> {
  const invoke = tauriInvoke();
  if (!invoke) throw new Error("browser window unavailable");
  await invoke("open_browser_window", { url: url ?? null });
}

/** 关闭内置浏览器窗口（端口文件由 Rust 侧窗口销毁回调清理）。 */
export async function closeBrowserWindow(): Promise<void> {
  await tauriInvoke()?.("close_browser_window");
}

/** 让浏览器窗口导航到指定地址。 */
export async function navigateBrowserWindow(url: string): Promise<void> {
  await tauriInvoke()?.("navigate_browser_window", { url });
}

/** 刷新浏览器窗口当前页。 */
export async function reloadBrowserWindow(): Promise<void> {
  await tauriInvoke()?.("reload_browser_window");
}

/** 浏览器窗口前进 / 后退。 */
export async function browserWindowHistory(direction: "back" | "forward"): Promise<void> {
  await tauriInvoke()?.("browser_window_history", { direction });
}

/** 用系统默认浏览器打开（`url` 为空时取窗口当前页）。 */
export async function openBrowserExternal(url?: string | null): Promise<void> {
  await tauriInvoke()?.("open_browser_external", { url: url ?? null });
}

/** 浏览器窗口当前是否打开（WebUI 重新加载后据此恢复控制栏）。 */
export async function isBrowserWindowOpen(): Promise<boolean> {
  const invoke = tauriInvoke();
  if (!invoke) return false;
  try {
    return (await invoke("browser_window_open")) === true;
  } catch {
    return false;
  }
}

/**
 * 订阅「浏览器窗口已关闭」——用户点窗口自己的〔X〕关闭时也要让控制栏收起。
 * 返回取消订阅函数；宿主不支持时返回空函数。
 */
export function onBrowserWindowClosed(handler: () => void): () => void {
  const listen = window.__TAURI__?.event?.listen;
  if (!listen) return () => {};
  let unlisten: (() => void) | null = null;
  let disposed = false;
  void listen("browser-window-closed", () => handler())
    .then((fn) => {
      if (disposed) fn();
      else unlisten = fn;
    })
    .catch(() => {});
  return () => {
    disposed = true;
    unlisten?.();
  };
}
