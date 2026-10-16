//! xianaibot 桌面壳。
//!
//! 启动无头 gateway sidecar（PyInstaller 打包的 Python 进程），解析其 stdout
//! 握手行 ``XIANAIBOT_GATEWAY_READY <host> <port>`` 后把窗口导航到 WebUI。
//! 关闭〔X〕按键时窗口**隐藏到系统托盘**，壳与 gateway（sidecar）进程继续存活，
//! 后台 cron/自动化任务因此持续执行；仅托盘菜单「退出」才杀掉 sidecar 并退出应用，
//! 避免残留后台 gateway。sidecar 请求引擎重启（``/api/desktop/restart``）退出后，
//! 壳会自动重新拉起并导航回 WebUI。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, RunEvent, Url};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// 托盘菜单项 id：重新显示主窗口。
const TRAY_SHOW: &str = "show";
/// 托盘菜单项 id：真正退出应用（杀掉 sidecar）。
const TRAY_QUIT: &str = "quit";

/// sidecar 就绪握手行前缀（后接空格 + host + 空格 + port）
const READY_PREFIX: &str = "XIANAIBOT_GATEWAY_READY";
/// sidecar 启动失败错误行前缀
const ERROR_PREFIX: &str = "XIANAIBOT_GATEWAY_ERROR";
/// 启动失败/网关意外退出时窗口回退到的内嵌错误页
const ERROR_PAGE: &str = "tauri://localhost/index.html?gateway_error=1";
/// 内嵌加载页（手动重试时先导航回这里显示加载动画）
const LOADING_PAGE: &str = "tauri://localhost/index.html";

/// 保存 sidecar 子进程句柄与退出标记，便于退出时清理、退出后自动重启。
struct SidecarState {
    child: Mutex<Option<CommandChild>>,
    exiting: AtomicBool,
    /// 防止 respawn 循环与手动重试并发拉起多个 sidecar 的重入锁。
    respawning: AtomicBool,
}

/// 单次 sidecar 生命周期结束后的去向。
enum SidecarOutcome {
    /// 就绪后意外退出（引擎重启请求），需要重新拉起。
    Respawn,
    /// 就绪前退出，视为启动失败。
    StartupFailed,
    /// 应用正在退出，停止循环。
    Stopped,
}

fn navigate_to(app: &AppHandle, url: &str) {
    if let Some(window) = app.get_webview_window("main") {
        if let Ok(parsed) = Url::parse(url) {
            let _ = window.navigate(parsed);
        }
    }
}

fn is_exiting(app: &AppHandle) -> bool {
    app.try_state::<SidecarState>()
        .map(|s| s.exiting.load(Ordering::SeqCst))
        .unwrap_or(false)
}

/// 显示并聚焦主窗口（托盘菜单「打开」与 macOS Dock 重开事件共用）。
fn reveal_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// 确保 SidecarState 已注册（只注册一次，重复 manage 会丢失子进程句柄）。
fn ensure_sidecar_state(app: &AppHandle) {
    if app.try_state::<SidecarState>().is_none() {
        app.manage(SidecarState {
            child: Mutex::new(None),
            exiting: AtomicBool::new(false),
            respawning: AtomicBool::new(false),
        });
    }
}

/// 启动 sidecar 并在就绪后把主窗口导航到 WebUI；退出后按需重新拉起。
fn spawn_sidecar(app: &AppHandle) {
    // 开发模式旁路：设置 XIANAIBOT_DEV_GATEWAY_URL 时直接导航，不启动 sidecar。
    // 便于本地用 ``xianaibot gateway`` 手动跑网关后调试壳本身。
    if let Ok(url) = std::env::var("XIANAIBOT_DEV_GATEWAY_URL") {
        let url = url.trim();
        if !url.is_empty() {
            navigate_to(app, url);
            return;
        }
    }

    ensure_sidecar_state(app);
    spawn_sidecar_loop(app);
}

/// sidecar 生命周期循环：respawning 重入锁保证同一时刻只有一个循环在跑
/// （自动 respawn 退避期间与手动重试不会并发拉起第二个 sidecar）。
fn spawn_sidecar_loop(app: &AppHandle) {
    if is_exiting(app) {
        return;
    }
    {
        let Some(state) = app.try_state::<SidecarState>() else {
            return;
        };
        if state.respawning.swap(true, Ordering::SeqCst) {
            return; // 已有循环在跑（含退避睡眠中）
        }
    }

    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let mut respawn_count: u32 = 0;
        loop {
            if is_exiting(&app) {
                break;
            }
            match run_sidecar_once(&app).await {
                SidecarOutcome::Respawn => {
                    respawn_count = respawn_count.saturating_add(1);
                    // 指数退避：1s → 2s → 4s → 8s → 16s → 30s（上限）。
                    // 没有 backoff 时，sidecar 启动后立即崩溃会形成紧密的
                    // 无限重启循环：每轮都 spawn 新进程、绑定端口、再退出，
                    // CPU 占用持续飙升且每轮残留孤儿进程占用端口（"死线程"）。
                    let delay_secs = 1u64
                        .saturating_mul(1u64 << respawn_count.min(5))
                        .min(30);
                    eprintln!(
                        "[xianaibot] sidecar exited after ready; respawning #{} in {}s",
                        respawn_count, delay_secs
                    );
                    tokio::time::sleep(std::time::Duration::from_secs(delay_secs)).await;
                    continue;
                }
                SidecarOutcome::StartupFailed | SidecarOutcome::Stopped => break,
            }
        }
        if let Some(state) = app.try_state::<SidecarState>() {
            state.respawning.store(false, Ordering::SeqCst);
        }
    });
}

/// 错误页「重试」按钮：把窗口切回加载页并重新拉起 sidecar。
/// sidecar 仍在运行、respawn 流程进行中（引擎重启退避）或应用正在退出时不动作。
#[tauri::command]
fn retry_gateway(app: AppHandle) {
    // 开发模式旁路同 spawn_sidecar：直接导航回手动启动的网关。
    if let Ok(url) = std::env::var("XIANAIBOT_DEV_GATEWAY_URL") {
        let url = url.trim();
        if !url.is_empty() {
            navigate_to(&app, url);
            return;
        }
    }
    if is_exiting(&app) {
        return;
    }
    if let Some(state) = app.try_state::<SidecarState>() {
        if state.child.lock().unwrap().is_some()
            || state.respawning.load(Ordering::SeqCst)
        {
            return;
        }
    }
    navigate_to(&app, LOADING_PAGE);
    spawn_sidecar_loop(&app);
}

async fn run_sidecar_once(app: &AppHandle) -> SidecarOutcome {
    // onedir 打包：sidecar 是「可执行文件 + _internal/」目录，经 bundle.resources
    // 随 app 分发（不再走 externalBin），需从 resource 目录解析可执行文件后手动启动。
    let sidecar = match app.path().resource_dir() {
        Ok(dir) => {
            let bin_name = format!("xianaibot-sidecar{}", std::env::consts::EXE_SUFFIX);
            app.shell().command(
                dir.join("binaries")
                    .join("xianaibot-sidecar")
                    .join(bin_name),
            )
        }
        Err(err) => {
            eprintln!("[xianaibot] failed to resolve resource dir: {err}");
            navigate_to(app, ERROR_PAGE);
            return SidecarOutcome::StartupFailed;
        }
    };

    // 把壳自身 PID 经环境变量传给 sidecar，其看门狗据此在壳被强杀时自清理，避免
    // 残留后台 gateway。（onefile 时代因 bootstrap→runtime 两级结构必须如此；onedir
    // 下 getppid() 已直接指向壳，此环境变量仍保留以保持看门狗逻辑不变。）
    let sidecar = sidecar.env("XIANAIBOT_PARENT_PID", std::process::id().to_string());

    let (mut rx, child) = match sidecar.spawn() {
        Ok(pair) => pair,
        Err(err) => {
            eprintln!("[xianaibot] failed to spawn sidecar: {err}");
            navigate_to(app, ERROR_PAGE);
            return SidecarOutcome::StartupFailed;
        }
    };
    if let Some(state) = app.try_state::<SidecarState>() {
        *state.child.lock().unwrap() = Some(child);
    }

    let mut ready = false;
    while let Some(event) = rx.recv().await {
        match event {
            CommandEvent::Stdout(bytes) => {
                let line = String::from_utf8_lossy(&bytes);
                if let Some(rest) = line.strip_prefix(READY_PREFIX) {
                    ready = true;
                    let mut parts = rest.split_whitespace();
                    if let (Some(host), Some(port)) = (parts.next(), parts.next()) {
                        navigate_to(app, &format!("http://{host}:{port}"));
                    }
                } else if line.contains(ERROR_PREFIX) {
                    navigate_to(app, ERROR_PAGE);
                }
            }
            CommandEvent::Stderr(bytes) => {
                eprintln!("[sidecar] {}", String::from_utf8_lossy(&bytes).trim_end());
            }
            CommandEvent::Terminated(_) => {
                if let Some(state) = app.try_state::<SidecarState>() {
                    state.child.lock().unwrap().take();
                }
                if is_exiting(app) {
                    return SidecarOutcome::Stopped;
                }
                if !ready {
                    navigate_to(app, ERROR_PAGE);
                    return SidecarOutcome::StartupFailed;
                }
                // 就绪后退出：引擎重启请求，重新拉起 sidecar。
                return SidecarOutcome::Respawn;
            }
            _ => {}
        }
    }
    // stdout 通道关闭（recv 返回 None）而未收到 Terminated：进程可能还活着
    // （tauri-plugin-shell 的 pipe 管理在某些竞态下会提前关闭通道）。此时必须
    // kill 进程，否则 sidecar 变成孤儿：主进程仍在跑（看门狗探测到壳活着不会
    // 退出），gateway 线程持有着端口但无人服务 → 下次 respawn 的新 sidecar
    // 遇端口被占又避让 → 端口不断漂移、CPU 占用攀升（"死线程占用端口"）。
    if let Some(state) = app.try_state::<SidecarState>() {
        if let Some(child) = state.child.lock().unwrap().take() {
            let _ = child.kill();  // 已退出时 kill 返回错误但无害
        }
    }
    if is_exiting(app) {
        SidecarOutcome::Stopped
    } else if ready {
        // 曾就绪过：可能是引擎重启请求或 stdout pipe 竞态，允许 respawn。
        SidecarOutcome::Respawn
    } else {
        // 从未就绪且 stdout 通道就关闭：视为启动失败，避免无限 respawn 空转。
        navigate_to(app, ERROR_PAGE);
        SidecarOutcome::StartupFailed
    }
}

/// 选一个本机空闲 TCP 端口（探测时立刻释放，存在极小竞态，可接受）。
fn free_local_port() -> Option<u16> {
    std::net::TcpListener::bind(("127.0.0.1", 0))
        .ok()?
        .local_addr()
        .ok()?
        .port()
        .into()
}

/// 内嵌浏览器抽屉 webview 的 CDP 端口文件：``~/.xianaibot/browser-cdp-port``。
///
/// gateway 侧 ``xianaibot.browser.manager`` 读取该文件附着内嵌浏览器（embedded
/// 模式）；抽屉关闭时由壳删除，避免 gateway 附着到已死的端口。
fn embedded_port_file() -> Option<std::path::PathBuf> {
    std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .ok()
        .map(|home| std::path::Path::new(&home).join(".xianaibot").join("browser-cdp-port"))
}

fn write_embedded_port_file(port: u16) {
    if let Some(path) = embedded_port_file() {
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let _ = std::fs::write(&path, port.to_string());
    }
}

fn remove_embedded_port_file() {
    if let Some(path) = embedded_port_file() {
        let _ = std::fs::remove_file(path);
    }
}

/// 解析用户输入的目标网址（缺 scheme 时补 https://）。
fn parse_target_url(raw: Option<&str>) -> Result<Option<Url>, String> {
    match raw.map(str::trim).filter(|s| !s.is_empty()) {
        Some(text) => {
            let with_scheme = if text.contains("://") {
                text.to_string()
            } else {
                format!("https://{text}")
            };
            tauri::Url::parse(&with_scheme).map(Some).map_err(|e| e.to_string())
        }
        None => Ok(None),
    }
}

fn browser_webview_args(port: u16) -> String {
    format!(
        "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection --remote-debugging-port={port}"
    )
}

/// 「内置浏览器」独立窗口的标签。
const BROWSER_WINDOW: &str = "browser";
/// 浏览器窗口内「工具栏」webview 的标签（加载本壳的 `browser-shell.html`）。
const BROWSER_CHROME: &str = "browser-chrome";
/// 浏览器窗口内「目标页面」webview 的标签（加载外部网址）。
const BROWSER_PAGE: &str = "browser-page";
/// 工具栏高度（逻辑像素；布局时按窗口缩放换算为物理像素）。
const BROWSER_CHROME_HEIGHT: f64 = 48.0;
/// 浏览器窗口销毁时通知主界面同步入口状态的事件名。
const BROWSER_WINDOW_CLOSED_EVENT: &str = "browser-window-closed";
/// 目标页面 URL 变化 → 推给工具栏地址栏（载荷为 URL 字符串）。
const BROWSER_WINDOW_URL_EVENT: &str = "browser-window-url-changed";
/// 目标页面加载开始/结束 → 推给工具栏显示进度（载荷为 bool）。
const BROWSER_WINDOW_LOADING_EVENT: &str = "browser-window-loading";
/// 开窗但未带地址 → 让工具栏地址框聚焦（豆包式「空标签页」体验）。
const BROWSER_WINDOW_FOCUS_ADDRESS_EVENT: &str = "browser-window-focus-address";
/// 浏览器窗口默认尺寸（逻辑像素；实际打开时按主窗口所在显示器夹取）。
const BROWSER_DEFAULT_WIDTH: f64 = 1180.0;
const BROWSER_DEFAULT_HEIGHT: f64 = 820.0;
/// 窗口与屏幕边缘/主窗口之间留的间隙（物理像素）。
const BROWSER_WINDOW_GAP: f64 = 8.0;

/// 打开「内置浏览器」**独立窗口**（幂等；已存在则聚焦并可选导航）。
///
/// 形态参考豆包 / Trae 的内置浏览器：**顶层无边框原生窗口**，窗口内自带完整
/// 浏览器 chrome——地址栏、前进/后退/刷新、系统浏览器打开、窗口控制（最小化/
/// 最大化/关闭）全部住在窗口自己的工具栏里，主界面不再放任何遥控条。
///
/// 实现上是一个窗口挂**两个 webview**（tauri `unstable` 多 webview）：
/// - `browser-chrome`（工具栏）：加载本壳静态页 `browser-shell.html`
///   （`frontendDist` = `window/`），本地源因此持有 IPC 权限（见
///   `capabilities/browser-chrome.json`）；
/// - `browser-page`（页面）：加载目标网址，铺满工具栏以下全部空间。
///
/// 关键收益：**彻底不依赖前端量矩形**。位置/尺寸交给操作系统，不存在
/// 「原生子 webview 与前端占位元素错位 → 矩形盖住界面吞掉点击」这类问题；
/// 窗口 resize 时由 Rust 侧重新铺排两个 webview（`layout_browser_webviews`）。
///
/// 环境隔离与自动化共享（沿用既有设计，gateway 侧无需改动）：
/// - 独立 `data_directory`（app_local_data_dir/browser-profile）→ 独立浏览器
///   进程与登录态，与主 UI 完全隔离；两个 webview 同目录同参数 → 同进程环境；
/// - `additional_browser_args` 追加 `--remote-debugging-port=<动态空闲端口>`，
///   只作用于该独立环境的浏览器进程。注意该参数会**整体替换** Tauri 默认值，
///   必须带上默认的 `--disable-features=...` 三项；
/// - 端口写入 ``~/.xianaibot/browser-cdp-port``，gateway 的浏览器管理器据此以
///   embedded 模式附着（智能体自动化默认走这个窗口，与用户同屏共享登录态）；
///   窗口销毁时删除，避免 gateway 附着到已死的端口。
///
/// 打开位置：贴主窗口右侧并排（同高），按主窗口所在显示器可用区夹取。
#[tauri::command]
async fn open_browser_window(app: AppHandle, url: Option<String>) -> Result<(), String> {
    let target_url = parse_target_url(url.as_deref())?;

    // 已存在：聚焦 + 按需导航，不重复创建。
    if let Some(existing) = app.get_window(BROWSER_WINDOW) {
        let _ = existing.show();
        let _ = existing.unminimize();
        let _ = existing.set_focus();
        if let Some(parsed) = target_url {
            if let Some(page) = app.get_webview(BROWSER_PAGE) {
                let _ = page.navigate(parsed);
            }
        } else {
            // 没带地址：让工具栏地址框聚焦，提示用户输入。
            let _ = app.emit_to(BROWSER_CHROME, BROWSER_WINDOW_FOCUS_ADDRESS_EVENT, ());
        }
        return Ok(());
    }

    let port = free_local_port().ok_or("no free local port")?;
    let data_dir = app
        .path()
        .app_local_data_dir()
        .map_err(|e| e.to_string())?
        .join("browser-profile");
    let initial_url = target_url
        .unwrap_or_else(|| tauri::Url::parse("about:blank").expect("about:blank is valid"));

    // 「新标签页 / 新窗口」请求处理（`target="_blank"`、`window.open()` 等）。
    //
    // 必须显式注册：wry 0.55 在**没有** new-window 处理器时走
    // `args.SetHandled(true)` 分支（wry/src/webview2/mod.rs 的
    // `add_NewWindowRequested`），即把请求直接丢弃——不导航、不报错、不留痕，
    // 表现为「同标签链接能点、新标签链接点了没反应」。
    // 单窗口语义 → 拒绝弹窗，就地导航页面 webview。
    let new_window_app = app.clone();
    let nav_app = app.clone();
    let load_app = app.clone();

    let browser_args = browser_webview_args(port);

    // 工具栏 webview：本地静态页，风格与启动加载页一致（深色 HUD）。
    let chrome = tauri::webview::WebviewBuilder::new(
        BROWSER_CHROME,
        tauri::WebviewUrl::App("browser-shell.html".into()),
    )
    .additional_browser_args(&browser_args)
    .data_directory(data_dir.clone());

    // 页面 webview：目标站点本体。
    let page = tauri::webview::WebviewBuilder::new(
        BROWSER_PAGE,
        tauri::WebviewUrl::External(initial_url),
    )
    .additional_browser_args(&browser_args)
    .data_directory(data_dir)
    .on_new_window(move |url, _features| {
        if matches!(url.scheme(), "http" | "https") {
            if let Some(page) = new_window_app.get_webview(BROWSER_PAGE) {
                let _ = page.navigate(url);
            }
        }
        tauri::webview::NewWindowResponse::Deny
    })
    // 导航 → 同步工具栏地址栏（on_navigation 抓不到 SPA pushState 路由，
    // 由下方 page load 事件兜底）。
    .on_navigation(move |url| {
        let _ = nav_app.emit_to(BROWSER_CHROME, BROWSER_WINDOW_URL_EVENT, url.to_string());
        true
    })
    .on_page_load(move |_webview, payload| {
        // 页面加载开始/结束 → 工具栏进度条；Finished 时再同步一次地址。
        let started = matches!(payload.event(), tauri::webview::PageLoadEvent::Started);
        let _ = load_app.emit_to(BROWSER_CHROME, BROWSER_WINDOW_LOADING_EVENT, started);
        if !started {
            let _ = load_app.emit_to(
                BROWSER_CHROME,
                BROWSER_WINDOW_URL_EVENT,
                payload.url().to_string(),
            );
        }
    })
    // 站点标题同步到窗口标题，方便任务栏/窗口列表里辨认（无边框窗口自身不显示）。
    .on_document_title_changed(|webview, title| {
        let title = title.trim().to_string();
        let label = if title.is_empty() {
            "夏奈儿 · 内置浏览器".to_string()
        } else {
            format!("{title} · 夏奈儿")
        };
        let _ = webview.window().set_title(&label);
    });

    // 无边框窗口：chrome 完全交给工具栏 webview（拖拽区 + 窗口控制都在里面）。
    let window = tauri::window::WindowBuilder::new(&app, BROWSER_WINDOW)
        .title("夏奈儿 · 内置浏览器")
        .inner_size(BROWSER_DEFAULT_WIDTH, BROWSER_DEFAULT_HEIGHT)
        .min_inner_size(480.0, 360.0 + BROWSER_CHROME_HEIGHT)
        // 先摆好位置再显示，避免在默认位置闪一下。
        .visible(false)
        .decorations(false)
        .build()
        .map_err(|e| e.to_string())?;

    // 按当前窗口尺寸铺排两个 webview（此刻窗口隐藏，无闪烁）。
    {
        let scale = window.scale_factor().unwrap_or(1.0);
        if let Ok(inner) = window.inner_size() {
            let chrome_h = ((BROWSER_CHROME_HEIGHT * scale).round() as u32).min(inner.height);
            let width = inner.width;
            let _ = window.add_child(
                chrome,
                tauri::PhysicalPosition::new(0, 0),
                tauri::PhysicalSize::new(width, chrome_h),
            );
            let _ = window.add_child(
                page,
                tauri::PhysicalPosition::new(0, chrome_h as i32),
                tauri::PhysicalSize::new(width, inner.height - chrome_h),
            );
        }
    }

    // 窗口尺寸/位置变化 → 双 webview 重新铺满；销毁 → 清理端口文件 +
    // 通知主界面同步入口状态（用户点工具栏〔X〕也要同步）。
    let app_for_event = app.clone();
    window.on_window_event(move |event| match event {
        tauri::WindowEvent::Resized(_) => {
            if let Some(win) = app_for_event.get_window(BROWSER_WINDOW) {
                layout_browser_webviews(&win);
            }
        }
        tauri::WindowEvent::Destroyed { .. } => {
            remove_embedded_port_file();
            let _ = app_for_event.emit_to("main", BROWSER_WINDOW_CLOSED_EVENT, ());
        }
        _ => {}
    });

    place_browser_window(&app, &window);
    let _ = window.show();

    write_embedded_port_file(port);
    eprintln!(
        "[xianaibot] browser window created, CDP port {port} written to {}",
        embedded_port_file()
            .map(|p| p.display().to_string())
            .unwrap_or_default()
    );
    Ok(())
}

/// 双 webview 布局：工具栏铺顶部（恒定逻辑高），页面占余下全部。
/// 全程用**物理像素**（inner_size / scale_factor 都是物理量），避免
/// 125%/150% 缩放下的换算误差。
fn layout_browser_webviews(window: &tauri::Window) {
    let Ok(scale) = window.scale_factor() else {
        return;
    };
    let Ok(size) = window.inner_size() else {
        return;
    };
    let width = size.width;
    let chrome_h = ((BROWSER_CHROME_HEIGHT * scale).round() as u32).min(size.height);

    if let Some(chrome) = window.get_webview(BROWSER_CHROME) {
        let _ = chrome.set_bounds(tauri::Rect {
            position: tauri::PhysicalPosition::new(0, 0).into(),
            size: tauri::PhysicalSize::new(width, chrome_h).into(),
        });
    }
    if let Some(page) = window.get_webview(BROWSER_PAGE) {
        let _ = page.set_bounds(tauri::Rect {
            position: tauri::PhysicalPosition::new(0, chrome_h as i32).into(),
            size: tauri::PhysicalSize::new(width, size.height - chrome_h).into(),
        });
    }
}

/// 把浏览器窗口摆到主窗口右侧并排（同高），按主窗口所在显示器可用区夹取。
///
/// 全程用**物理像素**：outer_position / outer_size / current_monitor 都是物理量，
/// 不做逻辑↔物理换算，避免 125%/150% 缩放下的偏移误差。
fn place_browser_window(app: &AppHandle, window: &tauri::Window) {
    let Some(main) = app.get_webview_window("main") else {
        let _ = window.center();
        return;
    };
    let (Ok(main_pos), Ok(main_size)) = (main.outer_position(), main.outer_size()) else {
        let _ = window.center();
        return;
    };

    let mut width = BROWSER_DEFAULT_WIDTH;
    let mut height = main_size.height as f64;
    let mut x = main_pos.x as f64 + main_size.width as f64 + BROWSER_WINDOW_GAP;
    let mut y = main_pos.y as f64;

    if let Ok(Some(monitor)) = main.current_monitor() {
        let m_pos = monitor.position();
        let m_size = monitor.size();
        let left = m_pos.x as f64;
        let top = m_pos.y as f64;
        let right = left + m_size.width as f64;
        let bottom = top + m_size.height as f64;

        // 宽度别超过显示器；右侧放不下就回收，至少不越界。
        width = width.min(right - left - BROWSER_WINDOW_GAP * 2.0);
        if x + width > right - BROWSER_WINDOW_GAP {
            x = right - BROWSER_WINDOW_GAP - width;
        }
        if x < left + BROWSER_WINDOW_GAP {
            x = left + BROWSER_WINDOW_GAP;
        }
        // 高度对齐主窗口，同样夹取进可用区。
        height = height.min(bottom - top - BROWSER_WINDOW_GAP * 2.0);
        if y + height > bottom - BROWSER_WINDOW_GAP {
            y = bottom - BROWSER_WINDOW_GAP - height;
        }
        if y < top + BROWSER_WINDOW_GAP {
            y = top + BROWSER_WINDOW_GAP;
        }
    }

    let _ = window.set_position(tauri::PhysicalPosition::new(
        x.round() as i32,
        y.round() as i32,
    ));
    let _ = window.set_size(tauri::PhysicalSize::new(
        width.round() as u32,
        height.round() as u32,
    ));
}

/// 让浏览器窗口的页面 webview 导航到指定地址（工具栏地址栏用）。
#[tauri::command]
async fn navigate_browser_window(app: AppHandle, url: String) -> Result<(), String> {
    let parsed = parse_target_url(Some(url.as_str()))?.ok_or("empty url")?;
    let Some(page) = app.get_webview(BROWSER_PAGE) else {
        return Err("browser window not open".to_string());
    };
    page.navigate(parsed).map_err(|e| e.to_string())
}

/// 刷新浏览器窗口当前页。
#[tauri::command]
async fn reload_browser_window(app: AppHandle) -> Result<(), String> {
    let Some(page) = app.get_webview(BROWSER_PAGE) else {
        return Err("browser window not open".to_string());
    };
    page.reload().map_err(|e| e.to_string())
}

/// 浏览器窗口前进/后退（`direction` 取 "back" | "forward"）。
#[tauri::command]
async fn browser_window_history(app: AppHandle, direction: String) -> Result<(), String> {
    let Some(page) = app.get_webview(BROWSER_PAGE) else {
        return Err("browser window not open".to_string());
    };
    let script = match direction.as_str() {
        "back" => "history.back()",
        "forward" => "history.forward()",
        _ => return Err("unknown direction".to_string()),
    };
    page.eval(script).map_err(|e| e.to_string())
}

/// 用系统默认程序打开一个 URL。
///
/// 刻意不用 `tauri_plugin_shell` 的 `Shell::open`——该 API 在 2.x 已废弃（官方
/// 指向 tauri-plugin-opener），而为一个「在系统浏览器打开」引入新插件不划算；
/// 三个平台各一条命令足矣。
fn open_url_in_system_browser(url: &str) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    let mut cmd = {
        use std::os::windows::process::CommandExt;
        let mut c = std::process::Command::new("cmd");
        // 第二个参数是 start 的窗口标题占位；CREATE_NO_WINDOW(0x0800) 防黑框闪现。
        c.args(["/C", "start", "", url]);
        c.creation_flags(0x0800);
        c
    };
    #[cfg(target_os = "macos")]
    let mut cmd = {
        let mut c = std::process::Command::new("open");
        c.arg(url);
        c
    };
    #[cfg(all(unix, not(target_os = "macos")))]
    let mut cmd = {
        let mut c = std::process::Command::new("xdg-open");
        c.arg(url);
        c
    };
    cmd.spawn().map(|_| ()).map_err(|e| e.to_string())
}

/// 用系统默认浏览器打开当前页（拿不到当前页时回退到传入的 `url`）。
#[tauri::command]
async fn open_browser_external(app: AppHandle, url: Option<String>) -> Result<(), String> {
    let explicit = url.as_deref().map(str::trim).filter(|s| !s.is_empty());
    let target = match explicit {
        Some(raw) => raw.to_string(),
        None => app
            .get_webview(BROWSER_PAGE)
            .and_then(|w| w.url().ok())
            .map(|u| u.to_string())
            .ok_or("no url")?,
    };
    open_url_in_system_browser(&target)
}

/// 关闭「内置浏览器」窗口（端口文件由窗口 Destroyed 回调清理）。
#[tauri::command]
async fn close_browser_window(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_window(BROWSER_WINDOW) {
        window.close().map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// 浏览器窗口当前是否打开（WebUI 重新加载后据此恢复入口高亮状态）。
#[tauri::command]
async fn browser_window_open(app: AppHandle) -> bool {
    app.get_window(BROWSER_WINDOW).is_some()
}

/// 浏览器窗口页面 webview 的当前地址（工具栏地址栏初始化用）。
#[tauri::command]
async fn browser_window_current_url(app: AppHandle) -> Result<String, String> {
    let Some(page) = app.get_webview(BROWSER_PAGE) else {
        return Err("browser window not open".to_string());
    };
    page.url().map(|u| u.to_string()).map_err(|e| e.to_string())
}

/// 创建系统托盘图标，提供「打开 xianaibot / 退出」菜单。
///
/// icon 复用窗口默认图标；``TrayIconBuilder::build`` 内部会将 ``TrayIcon`` clone
/// 进 app 资源表，因此无需保留返回句柄。菜单项 id 见 :const:`TRAY_SHOW` /
/// :const:`TRAY_QUIT`。
fn create_tray(app: &AppHandle) -> tauri::Result<()> {
    let show = MenuItemBuilder::with_id(TRAY_SHOW, "打开夏奈儿").build(app)?;
    let quit = MenuItemBuilder::with_id(TRAY_QUIT, "退出").build(app)?;
    let menu = MenuBuilder::new(app).items(&[&show, &quit]).build()?;

    let icon = app
        .default_window_icon()
        .cloned()
        .expect("missing default window icon");

    TrayIconBuilder::with_id("xianaibot-main-tray")
        .icon(icon)
        .tooltip("夏奈儿")
        .menu(&menu)
        // 左键 = 打开主窗口（Windows 惯例），右键 = 菜单；显式设为 false，
        // 避免 macOS 默认值不同导致两端行为不一致。
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                reveal_main_window(tray.app_handle());
            }
        })
        .on_menu_event(|app, event| match event.id().as_ref() {
            TRAY_SHOW => reveal_main_window(app),
            TRAY_QUIT => app.exit(0),
            _ => {}
        })
        .build(app)?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        // 单实例插件必须是**第一个**注册——第二实例启动时会先经它检测到主实例，
        // 触发回调后立即退出，不会再走到 .setup()，因此不会拉起第二个 sidecar，
        // 杜绝「重复点击 → 多个壳 + 多个后台网关」的残留。app 隐藏到托盘（关闭
        // 〔X〕不退出，仅托盘「退出」才杀 sidecar），故回调里把主窗口显示并聚焦到
        // 前台，而非什么都不做。
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            reveal_main_window(app);
        }))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            retry_gateway,
            open_browser_window,
            close_browser_window,
            navigate_browser_window,
            reload_browser_window,
            browser_window_history,
            open_browser_external,
            browser_window_open,
            browser_window_current_url
        ])
        .setup(|app| {
            spawn_sidecar(app.handle());

            // 关闭主窗口 → 隐藏到托盘（不退出进程，gateway 继续跑自动化任务）。
            if let Some(window) = app.get_webview_window("main") {
                let win = window.clone();
                window.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = win.hide();
                    }
                });
            }

            create_tray(app.handle())?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building xianaibot tauri application");

    app.run(|app_handle, event| match event {
        #[cfg(target_os = "macos")]
        // 点击 Dock 图标重开应用：窗口被关闭〔X〕隐藏到托盘后已无可见窗口，
        // macOS 会经 applicationShouldHandleReopen 触发 Reopen，但默认不会自动
        // 重新显示程序化 hide() 的窗口——此前只能靠托盘菜单「打开 xianaibot」，
        // 现于此处显式 show/focus 主窗口。
        RunEvent::Reopen { .. } => reveal_main_window(app_handle),
        RunEvent::ExitRequested { .. } | RunEvent::Exit => {
            remove_embedded_port_file();
            if let Some(state) = app_handle.try_state::<SidecarState>() {
                state.exiting.store(true, Ordering::SeqCst);
                if let Some(child) = state.child.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        }
        _ => {}
    });
}
