//! biscuitbot 桌面壳。
//!
//! 启动无头 gateway sidecar（PyInstaller 打包的 Python 进程），解析其 stdout
//! 握手行 ``BISCUITBOT_GATEWAY_READY <host> <port>`` 后把窗口导航到 WebUI。
//! 应用退出时杀掉 sidecar 子进程，避免残留后台 gateway。

use std::sync::Mutex;

use tauri::{AppHandle, Manager, RunEvent, Url};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// sidecar 就绪握手行前缀（后接空格 + host + 空格 + port）
const READY_PREFIX: &str = "BISCUITBOT_GATEWAY_READY";
/// sidecar 启动失败错误行前缀
const ERROR_PREFIX: &str = "BISCUITBOT_GATEWAY_ERROR";
/// 启动失败/网关意外退出时窗口回退到的内嵌错误页
const ERROR_PAGE: &str = "tauri://localhost/index.html?gateway_error=1";

/// 保存 sidecar 子进程句柄，便于退出时清理。
struct SidecarHandle(Mutex<Option<CommandChild>>);

fn navigate_to(app: &AppHandle, url: &str) {
    if let Some(window) = app.get_webview_window("main") {
        if let Ok(parsed) = Url::parse(url) {
            let _ = window.navigate(parsed);
        }
    }
}

/// 启动 sidecar 并在就绪后把主窗口导航到 WebUI。
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

    let sidecar = match app.shell().sidecar("biscuitbot-sidecar") {
        Ok(cmd) => cmd,
        Err(err) => {
            eprintln!("[biscuitbot] failed to create sidecar command: {err}");
            navigate_to(app, ERROR_PAGE);
            return;
        }
    };

    // PyInstaller onefile 是 bootstrap→runtime 两级进程，sidecar 内的 getppid()
    // 指向 bootstrap 而非本壳；把壳自身 PID 经环境变量传给 sidecar，其看门狗
    // 据此在壳被强杀时自清理，避免残留后台 gateway。
    let sidecar = sidecar.env("BISCUITBOT_PARENT_PID", std::process::id().to_string());

    let (mut rx, child) = match sidecar.spawn() {
        Ok(pair) => pair,
        Err(err) => {
            eprintln!("[biscuitbot] failed to spawn sidecar: {err}");
            navigate_to(app, ERROR_PAGE);
            return;
        }
    };
    app.manage(SidecarHandle(Mutex::new(Some(child))));

    let app = app.clone();
    let mut ready = false;
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(bytes) => {
                    let line = String::from_utf8_lossy(&bytes);
                    if let Some(rest) = line.strip_prefix(READY_PREFIX) {
                        ready = true;
                        let mut parts = rest.split_whitespace();
                        if let (Some(host), Some(port)) = (parts.next(), parts.next()) {
                            navigate_to(&app, &format!("http://{host}:{port}"));
                        }
                    } else if line.contains(ERROR_PREFIX) {
                        navigate_to(&app, ERROR_PAGE);
                    }
                }
                CommandEvent::Stderr(bytes) => {
                    eprintln!("[sidecar] {}", String::from_utf8_lossy(&bytes).trim_end());
                }
                CommandEvent::Terminated(_) => {
                    // 就绪后退出属于被清理（应用退出）；就绪前退出视为启动失败。
                    if !ready {
                        navigate_to(&app, ERROR_PAGE);
                    }
                    break;
                }
                _ => {}
            }
        }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_process::init())
        .setup(|app| {
            spawn_sidecar(app.handle());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building biscuitbot tauri application");

    app.run(|app_handle, event| {
        if let RunEvent::ExitRequested { .. } | RunEvent::Exit = event {
            if let Some(state) = app_handle.try_state::<SidecarHandle>() {
                if let Some(child) = state.0.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        }
    });
}
