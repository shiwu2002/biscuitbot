import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = env.BISCUITBOT_API_URL ?? "http://127.0.0.1:8765";
  const hmrPath = "/__biscuitbot_vite_hmr";

  return {
    plugins: [react()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    optimizeDeps: {
      // Radix dialog was introduced mid-session for the mobile sidebar sheet.
      // When Vite re-optimizes it on a running dev server, the browser can race
      // and request stale chunk paths from `.vite/deps`. Excluding it keeps dev
      // reloads stable instead of rewriting those chunk filenames under us.
      exclude: ["@radix-ui/react-dialog"],
    },
    build: {
      outDir: path.resolve(__dirname, "../biscuitbot/web/dist"),
      emptyOutDir: true,
      sourcemap: false,
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (id.includes("node_modules/refractor/lang/")) {
              return;
            }
            // React/ReactDOM/scheduler 必须整体落在同一个 chunk：一旦被
            // react-syntax-highlighter / react-markdown 等 CJS 依赖链拆分进各自
            // chunk，就会出现两份 React 实例——react-markdown 走 B 实例的 hooks、
            // 组件树走 A 实例，生产构建抛 "Rendered more hooks"（#310）、开发构建
            // 抛 __SECRET_INTERNALS 未定义。这里在分 vendor chunk 之前先把 React
            // 家族收敛进 react-vendor，其余 chunk 统一从它 import。
            if (
              id.includes("node_modules/react/")
              || id.includes("node_modules/react-dom/")
              || id.includes("node_modules/scheduler/")
            ) {
              return "react-vendor";
            }
            if (
              id.includes("node_modules/react-syntax-highlighter")
              || id.includes("node_modules/refractor/core")
            ) {
              return "syntax-highlight";
            }
            if (
              id.includes("node_modules/react-markdown")
              || id.includes("node_modules/remark-")
              || id.includes("node_modules/rehype-")
              || id.includes("node_modules/unified")
              || id.includes("node_modules/mdast-")
              || id.includes("node_modules/hast-")
              || id.includes("node_modules/micromark")
              || id.includes("node_modules/unist-")
            ) {
              return "markdown-vendor";
            }
            if (id.includes("node_modules/katex")) {
              return "katex";
            }
          },
        },
      },
    },
    server: {
      host: "127.0.0.1",
      port: 5199,
      strictPort: true,
      // Keep Vite's HMR socket on a dedicated path. Biscuitbot's app WebSocket is
      // opened directly from the browser to the gateway, so the dev server
      // should never proxy WebSocket upgrades.
      hmr: {
        host: "127.0.0.1",
        path: hmrPath,
      },
      proxy: {
        "/webui": { target, changeOrigin: true },
        "/api": { target, changeOrigin: true },
        "/auth": { target, changeOrigin: true },
      },
    },
    test: {
      environment: "happy-dom",
      globals: true,
      setupFiles: ["./src/tests/setup.ts"],
    },
  };
});
