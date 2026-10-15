fn main() {
    // 远程 URL 上下文（gateway 服务的 WebUI，http://127.0.0.1:*）调用应用自定义
    // 命令必须在 ACL manifest 里显式声明，否则 invoke 报
    // "not allowed. Plugin not found"（本地 tauri:// 页面不受此限制）。
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "retry_gateway",
                "open_browser_window",
                "close_browser_window",
                "navigate_browser_window",
                "reload_browser_window",
                "browser_window_history",
                "open_browser_external",
                "browser_window_open",
            ]),
        ),
    )
    .unwrap();
}
