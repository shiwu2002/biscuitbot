//! biscuitbot 桌面壳。
//!
//! 启动无头 gateway sidecar（PyInstaller 打包的 Python 进程），解析其 stdout
//! 握手行 ``BISCUITBOT_GATEWAY_READY <host> <port>`` 后把窗口导航到 WebUI。
//! 关闭〔X〕按键时窗口**隐藏到系统托盘**，壳与 gateway（sidecar）进程继续存活，
//! 后台 cron/自动化任务因此持续执行；仅托盘菜单「退出」才杀掉 sidecar 并退出应用，
//! 避免残留后台 gateway。sidecar 请求引擎重启（``/api/desktop/restart``）退出后，
//! 壳会自动重新拉起并导航回 WebUI。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, RunEvent, Url};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// 托盘菜单项 id：重新显示主窗口。
const TRAY_SHOW: &str = "show";
/// 托盘菜单项 id：真正退出应用（杀掉 sidecar）。
const TRAY_QUIT: &str = "quit";

/// sidecar 就绪握手行前缀（后接空格 + host + 空格 + port）
const READY_PREFIX: &str = "BISCUITBOT_GATEWAY_READY";
/// sidecar 启动失败错误行前缀
const ERROR_PREFIX: &str = "BISCUITBOT_GATEWAY_ERROR";
/// 启动失败/网关意外退出时窗口回退到的内嵌错误页
const ERROR_PAGE: &str = "tauri://localhost/index.html?gateway_error=1";

/// 保存 sidecar 子进程句柄与退出标记，便于退出时清理、退出后自动重启。
struct SidecarState {
    child: Mutex<Option<CommandChild>>,
    exiting: AtomicBool,
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

/// 启动 sidecar 并在就绪后把主窗口导航到 WebUI；退出后按需重新拉起。
fn spawn_sidecar(app: &AppHandle) {
    // 开发模式旁路：设置 BISCUITBOT_DEV_GATEWAY_URL 时直接导航，不启动 sidecar。
    // 便于本地用 ``biscuitbot gateway`` 手动跑网关后调试壳本身。
    if let Ok(url) = std::env::var("BISCUITBOT_DEV_GATEWAY_URL") {
        let url = url.trim();
        if !url.is_empty() {
            navigate_to(app, url);
            return;
        }
    }

    app.manage(SidecarState {
        child: Mutex::new(None),
        exiting: AtomicBool::new(false),
    });

    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let mut respawn_count: u32 = 0;
        loop {
            if is_exiting(&app) {
                return;
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
                        "[biscuitbot] sidecar exited after ready; respawning #{} in {}s",
                        respawn_count, delay_secs
                    );
                    tokio::time::sleep(std::time::Duration::from_secs(delay_secs)).await;
                    continue;
                }
                SidecarOutcome::StartupFailed | SidecarOutcome::Stopped => return,
            }
        }
    });
}

async fn run_sidecar_once(app: &AppHandle) -> SidecarOutcome {
    // onedir 打包：sidecar 是「可执行文件 + _internal/」目录，经 bundle.resources
    // 随 app 分发（不再走 externalBin），需从 resource 目录解析可执行文件后手动启动。
    let sidecar = match app.path().resource_dir() {
        Ok(dir) => {
            let bin_name = format!("biscuitbot-sidecar{}", std::env::consts::EXE_SUFFIX);
            app.shell().command(
                dir.join("binaries")
                    .join("biscuitbot-sidecar")
                    .join(bin_name),
            )
        }
        Err(err) => {
            eprintln!("[biscuitbot] failed to resolve resource dir: {err}");
            navigate_to(app, ERROR_PAGE);
            return SidecarOutcome::StartupFailed;
        }
    };

    // 把壳自身 PID 经环境变量传给 sidecar，其看门狗据此在壳被强杀时自清理，避免
    // 残留后台 gateway。（onefile 时代因 bootstrap→runtime 两级结构必须如此；onedir
    // 下 getppid() 已直接指向壳，此环境变量仍保留以保持看门狗逻辑不变。）
    let sidecar = sidecar.env("BISCUITBOT_PARENT_PID", std::process::id().to_string());

    let (mut rx, child) = match sidecar.spawn() {
        Ok(pair) => pair,
        Err(err) => {
            eprintln!("[biscuitbot] failed to spawn sidecar: {err}");
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

/// 创建系统托盘图标，提供「打开 biscuitbot / 退出」菜单。
///
/// icon 复用窗口默认图标；``TrayIconBuilder::build`` 内部会将 ``TrayIcon`` clone
/// 进 app 资源表，因此无需保留返回句柄。菜单项 id 见 :const:`TRAY_SHOW` /
/// :const:`TRAY_QUIT`。
fn create_tray(app: &AppHandle) -> tauri::Result<()> {
    let show = MenuItemBuilder::with_id(TRAY_SHOW, "打开 biscuitbot").build(app)?;
    let quit = MenuItemBuilder::with_id(TRAY_QUIT, "退出").build(app)?;
    let menu = MenuBuilder::new(app).items(&[&show, &quit]).build()?;

    let icon = app
        .default_window_icon()
        .cloned()
        .expect("missing default window icon");

    TrayIconBuilder::with_id("biscuitbot-main-tray")
        .icon(icon)
        .tooltip("biscuitbot")
        .menu(&menu)
        .show_menu_on_left_click(true)
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
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_dialog::init())
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
        .expect("error while building biscuitbot tauri application");

    app.run(|app_handle, event| match event {
        #[cfg(target_os = "macos")]
        // 点击 Dock 图标重开应用：窗口被关闭〔X〕隐藏到托盘后已无可见窗口，
        // macOS 会经 applicationShouldHandleReopen 触发 Reopen，但默认不会自动
        // 重新显示程序化 hide() 的窗口——此前只能靠托盘菜单「打开 biscuitbot」，
        // 现于此处显式 show/focus 主窗口。
        RunEvent::Reopen { .. } => reveal_main_window(app_handle),
        RunEvent::ExitRequested { .. } | RunEvent::Exit => {
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
