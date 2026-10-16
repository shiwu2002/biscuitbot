/**
 * 桌面壳「内置浏览器」独立窗口的命令封装。
 *
 * 形态：**顶层无边框原生窗口**，窗口内自带完整浏览器 chrome（地址栏、前进/
 * 后退/刷新、系统浏览器打开、窗口控制，见本仓库 `src-tauri/window/
 * browser-shell.html`），像豆包 / Trae 的内置浏览器一样自成一体；
 * **主界面不再放任何遥控条**。
 *
 * 窗口内部是双 webview：工具栏（browser-chrome，本地静态页）+ 目标页面
 * （browser-page）。页面 webview 与 gateway 共享 CDP 端口，因此智能体自动化
 * 与用户看到的是同一个浏览器实例。
 *
 * 本模块只暴露「开 / 关 / 状态」；地址栏等导航交互全部发生在浏览器窗口自己的
 * 工具栏里（工具栏走 raw invoke，不经过本模块）。
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

/** 浏览器窗口当前是否打开（WebUI 重新加载后据此恢复入口高亮）。 */
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
 * 订阅「浏览器窗口已关闭」——用户点浏览器窗口工具栏的〔X〕关闭时，
 * 主界面入口也要取消高亮。返回取消订阅函数；宿主不支持时返回空函数。
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
