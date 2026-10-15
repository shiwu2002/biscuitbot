fn main() {
    // 远程 URL 上下文（gateway 服务的 WebUI，http://127.0.0.1:*）调用应用自定义
    // 命令必须在 ACL manifest 里显式声明，否则 invoke 报
    // "not allowed. Plugin not found"（本地 tauri:// 页面不受此限制）。
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "retry_gateway",
                "open_browser_drawer",
                "update_browser_drawer",
                "close_browser_drawer",
            ]),
        ),
    )
    .unwrap();
}
