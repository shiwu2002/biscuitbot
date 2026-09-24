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
use tauri::{AppHandle, Manager, RunEvent, Url};
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
        .invoke_handler(tauri::generate_handler![retry_gateway])
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
