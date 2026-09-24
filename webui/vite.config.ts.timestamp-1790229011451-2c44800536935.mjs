// vite.config.ts
import { defineConfig, loadEnv } from "file:///D:/trae/biscuitbot/webui/node_modules/vite/dist/node/index.js";
import react from "file:///D:/trae/biscuitbot/webui/node_modules/@vitejs/plugin-react/dist/index.js";
import path from "node:path";
var __vite_injected_original_dirname = "D:\\trae\\biscuitbot\\webui";
var vite_config_default = defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = env.XIANAIBOT_API_URL ?? "http://127.0.0.1:8765";
  const hmrPath = "/__xianaibot_vite_hmr";
  return {
    plugins: [react()],
    resolve: {
      alias: {
        "@": path.resolve(__vite_injected_original_dirname, "./src")
      }
    },
    optimizeDeps: {
      // Radix dialog was introduced mid-session for the mobile sidebar sheet.
      // When Vite re-optimizes it on a running dev server, the browser can race
      // and request stale chunk paths from `.vite/deps`. Excluding it keeps dev
      // reloads stable instead of rewriting those chunk filenames under us.
      exclude: ["@radix-ui/react-dialog"]
    },
    build: {
      outDir: path.resolve(__vite_injected_original_dirname, "../xianaibot/web/dist"),
      emptyOutDir: true,
      sourcemap: false,
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (id.includes("node_modules/refractor/lang/")) {
              return;
            }
            if (id.includes("node_modules/react/") || id.includes("node_modules/react-dom/") || id.includes("node_modules/scheduler/")) {
              return "react-vendor";
            }
            if (id.includes("node_modules/react-syntax-highlighter") || id.includes("node_modules/refractor/core")) {
              return "syntax-highlight";
            }
            if (id.includes("node_modules/react-markdown") || id.includes("node_modules/remark-") || id.includes("node_modules/rehype-") || id.includes("node_modules/unified") || id.includes("node_modules/mdast-") || id.includes("node_modules/hast-") || id.includes("node_modules/micromark") || id.includes("node_modules/unist-")) {
              return "markdown-vendor";
            }
            if (id.includes("node_modules/katex")) {
              return "katex";
            }
          }
        }
      }
    },
    server: {
      host: "127.0.0.1",
      port: 5199,
      strictPort: true,
      // Keep Vite's HMR socket on a dedicated path. Xianaibot's app WebSocket is
      // opened directly from the browser to the gateway, so the dev server
      // should never proxy WebSocket upgrades.
      hmr: {
        host: "127.0.0.1",
        path: hmrPath
      },
      proxy: {
        "/webui": { target, changeOrigin: true },
        "/api": { target, changeOrigin: true },
        "/auth": { target, changeOrigin: true }
      }
    },
    test: {
      environment: "happy-dom",
      globals: true,
      setupFiles: ["./src/tests/setup.ts"]
    }
  };
});
export {
  vite_config_default as default
};
//# sourceMappingURL=data:application/json;base64,ewogICJ2ZXJzaW9uIjogMywKICAic291cmNlcyI6IFsidml0ZS5jb25maWcudHMiXSwKICAic291cmNlc0NvbnRlbnQiOiBbImNvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9kaXJuYW1lID0gXCJEOlxcXFx0cmFlXFxcXGJpc2N1aXRib3RcXFxcd2VidWlcIjtjb25zdCBfX3ZpdGVfaW5qZWN0ZWRfb3JpZ2luYWxfZmlsZW5hbWUgPSBcIkQ6XFxcXHRyYWVcXFxcYmlzY3VpdGJvdFxcXFx3ZWJ1aVxcXFx2aXRlLmNvbmZpZy50c1wiO2NvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9pbXBvcnRfbWV0YV91cmwgPSBcImZpbGU6Ly8vRDovdHJhZS9iaXNjdWl0Ym90L3dlYnVpL3ZpdGUuY29uZmlnLnRzXCI7aW1wb3J0IHsgZGVmaW5lQ29uZmlnLCBsb2FkRW52IH0gZnJvbSBcInZpdGVcIjtcclxuaW1wb3J0IHJlYWN0IGZyb20gXCJAdml0ZWpzL3BsdWdpbi1yZWFjdFwiO1xyXG5pbXBvcnQgcGF0aCBmcm9tIFwibm9kZTpwYXRoXCI7XHJcblxyXG5leHBvcnQgZGVmYXVsdCBkZWZpbmVDb25maWcoKHsgbW9kZSB9KSA9PiB7XHJcbiAgY29uc3QgZW52ID0gbG9hZEVudihtb2RlLCBwcm9jZXNzLmN3ZCgpLCBcIlwiKTtcclxuICBjb25zdCB0YXJnZXQgPSBlbnYuWElBTkFJQk9UX0FQSV9VUkwgPz8gXCJodHRwOi8vMTI3LjAuMC4xOjg3NjVcIjtcclxuICBjb25zdCBobXJQYXRoID0gXCIvX194aWFuYWlib3Rfdml0ZV9obXJcIjtcclxuXHJcbiAgcmV0dXJuIHtcclxuICAgIHBsdWdpbnM6IFtyZWFjdCgpXSxcclxuICAgIHJlc29sdmU6IHtcclxuICAgICAgYWxpYXM6IHtcclxuICAgICAgICBcIkBcIjogcGF0aC5yZXNvbHZlKF9fZGlybmFtZSwgXCIuL3NyY1wiKSxcclxuICAgICAgfSxcclxuICAgIH0sXHJcbiAgICBvcHRpbWl6ZURlcHM6IHtcclxuICAgICAgLy8gUmFkaXggZGlhbG9nIHdhcyBpbnRyb2R1Y2VkIG1pZC1zZXNzaW9uIGZvciB0aGUgbW9iaWxlIHNpZGViYXIgc2hlZXQuXHJcbiAgICAgIC8vIFdoZW4gVml0ZSByZS1vcHRpbWl6ZXMgaXQgb24gYSBydW5uaW5nIGRldiBzZXJ2ZXIsIHRoZSBicm93c2VyIGNhbiByYWNlXHJcbiAgICAgIC8vIGFuZCByZXF1ZXN0IHN0YWxlIGNodW5rIHBhdGhzIGZyb20gYC52aXRlL2RlcHNgLiBFeGNsdWRpbmcgaXQga2VlcHMgZGV2XHJcbiAgICAgIC8vIHJlbG9hZHMgc3RhYmxlIGluc3RlYWQgb2YgcmV3cml0aW5nIHRob3NlIGNodW5rIGZpbGVuYW1lcyB1bmRlciB1cy5cclxuICAgICAgZXhjbHVkZTogW1wiQHJhZGl4LXVpL3JlYWN0LWRpYWxvZ1wiXSxcclxuICAgIH0sXHJcbiAgICBidWlsZDoge1xyXG4gICAgICBvdXREaXI6IHBhdGgucmVzb2x2ZShfX2Rpcm5hbWUsIFwiLi4veGlhbmFpYm90L3dlYi9kaXN0XCIpLFxyXG4gICAgICBlbXB0eU91dERpcjogdHJ1ZSxcclxuICAgICAgc291cmNlbWFwOiBmYWxzZSxcclxuICAgICAgcm9sbHVwT3B0aW9uczoge1xyXG4gICAgICAgIG91dHB1dDoge1xyXG4gICAgICAgICAgbWFudWFsQ2h1bmtzKGlkKSB7XHJcbiAgICAgICAgICAgIGlmIChpZC5pbmNsdWRlcyhcIm5vZGVfbW9kdWxlcy9yZWZyYWN0b3IvbGFuZy9cIikpIHtcclxuICAgICAgICAgICAgICByZXR1cm47XHJcbiAgICAgICAgICAgIH1cclxuICAgICAgICAgICAgLy8gUmVhY3QvUmVhY3RET00vc2NoZWR1bGVyIFx1NUZDNVx1OTg3Qlx1NjU3NFx1NEY1M1x1ODQzRFx1NTcyOFx1NTQwQ1x1NEUwMFx1NEUyQSBjaHVua1x1RkYxQVx1NEUwMFx1NjVFNlx1ODhBQlxyXG4gICAgICAgICAgICAvLyByZWFjdC1zeW50YXgtaGlnaGxpZ2h0ZXIgLyByZWFjdC1tYXJrZG93biBcdTdCNDkgQ0pTIFx1NEY5RFx1OEQ1Nlx1OTRGRVx1NjJDNlx1NTIwNlx1OEZEQlx1NTQwNFx1ODFFQVxyXG4gICAgICAgICAgICAvLyBjaHVua1x1RkYwQ1x1NUMzMVx1NEYxQVx1NTFGQVx1NzNCMFx1NEUyNFx1NEVGRCBSZWFjdCBcdTVCOUVcdTRGOEJcdTIwMTRcdTIwMTRyZWFjdC1tYXJrZG93biBcdThENzAgQiBcdTVCOUVcdTRGOEJcdTc2ODQgaG9va3NcdTMwMDFcclxuICAgICAgICAgICAgLy8gXHU3RUM0XHU0RUY2XHU2ODExXHU4RDcwIEEgXHU1QjlFXHU0RjhCXHVGRjBDXHU3NTFGXHU0RUE3XHU2Nzg0XHU1RUZBXHU2MjlCIFwiUmVuZGVyZWQgbW9yZSBob29rc1wiXHVGRjA4IzMxMFx1RkYwOVx1MzAwMVx1NUYwMFx1NTNEMVx1Njc4NFx1NUVGQVxyXG4gICAgICAgICAgICAvLyBcdTYyOUIgX19TRUNSRVRfSU5URVJOQUxTIFx1NjcyQVx1NUI5QVx1NEU0OVx1MzAwMlx1OEZEOVx1OTFDQ1x1NTcyOFx1NTIwNiB2ZW5kb3IgY2h1bmsgXHU0RTRCXHU1MjREXHU1MTQ4XHU2MjhBIFJlYWN0XHJcbiAgICAgICAgICAgIC8vIFx1NUJCNlx1NjVDRlx1NjUzNlx1NjU1Qlx1OEZEQiByZWFjdC12ZW5kb3JcdUZGMENcdTUxNzZcdTRGNTkgY2h1bmsgXHU3RURGXHU0RTAwXHU0RUNFXHU1QjgzIGltcG9ydFx1MzAwMlxyXG4gICAgICAgICAgICBpZiAoXHJcbiAgICAgICAgICAgICAgaWQuaW5jbHVkZXMoXCJub2RlX21vZHVsZXMvcmVhY3QvXCIpXHJcbiAgICAgICAgICAgICAgfHwgaWQuaW5jbHVkZXMoXCJub2RlX21vZHVsZXMvcmVhY3QtZG9tL1wiKVxyXG4gICAgICAgICAgICAgIHx8IGlkLmluY2x1ZGVzKFwibm9kZV9tb2R1bGVzL3NjaGVkdWxlci9cIilcclxuICAgICAgICAgICAgKSB7XHJcbiAgICAgICAgICAgICAgcmV0dXJuIFwicmVhY3QtdmVuZG9yXCI7XHJcbiAgICAgICAgICAgIH1cclxuICAgICAgICAgICAgaWYgKFxyXG4gICAgICAgICAgICAgIGlkLmluY2x1ZGVzKFwibm9kZV9tb2R1bGVzL3JlYWN0LXN5bnRheC1oaWdobGlnaHRlclwiKVxyXG4gICAgICAgICAgICAgIHx8IGlkLmluY2x1ZGVzKFwibm9kZV9tb2R1bGVzL3JlZnJhY3Rvci9jb3JlXCIpXHJcbiAgICAgICAgICAgICkge1xyXG4gICAgICAgICAgICAgIHJldHVybiBcInN5bnRheC1oaWdobGlnaHRcIjtcclxuICAgICAgICAgICAgfVxyXG4gICAgICAgICAgICBpZiAoXHJcbiAgICAgICAgICAgICAgaWQuaW5jbHVkZXMoXCJub2RlX21vZHVsZXMvcmVhY3QtbWFya2Rvd25cIilcclxuICAgICAgICAgICAgICB8fCBpZC5pbmNsdWRlcyhcIm5vZGVfbW9kdWxlcy9yZW1hcmstXCIpXHJcbiAgICAgICAgICAgICAgfHwgaWQuaW5jbHVkZXMoXCJub2RlX21vZHVsZXMvcmVoeXBlLVwiKVxyXG4gICAgICAgICAgICAgIHx8IGlkLmluY2x1ZGVzKFwibm9kZV9tb2R1bGVzL3VuaWZpZWRcIilcclxuICAgICAgICAgICAgICB8fCBpZC5pbmNsdWRlcyhcIm5vZGVfbW9kdWxlcy9tZGFzdC1cIilcclxuICAgICAgICAgICAgICB8fCBpZC5pbmNsdWRlcyhcIm5vZGVfbW9kdWxlcy9oYXN0LVwiKVxyXG4gICAgICAgICAgICAgIHx8IGlkLmluY2x1ZGVzKFwibm9kZV9tb2R1bGVzL21pY3JvbWFya1wiKVxyXG4gICAgICAgICAgICAgIHx8IGlkLmluY2x1ZGVzKFwibm9kZV9tb2R1bGVzL3VuaXN0LVwiKVxyXG4gICAgICAgICAgICApIHtcclxuICAgICAgICAgICAgICByZXR1cm4gXCJtYXJrZG93bi12ZW5kb3JcIjtcclxuICAgICAgICAgICAgfVxyXG4gICAgICAgICAgICBpZiAoaWQuaW5jbHVkZXMoXCJub2RlX21vZHVsZXMva2F0ZXhcIikpIHtcclxuICAgICAgICAgICAgICByZXR1cm4gXCJrYXRleFwiO1xyXG4gICAgICAgICAgICB9XHJcbiAgICAgICAgICB9LFxyXG4gICAgICAgIH0sXHJcbiAgICAgIH0sXHJcbiAgICB9LFxyXG4gICAgc2VydmVyOiB7XHJcbiAgICAgIGhvc3Q6IFwiMTI3LjAuMC4xXCIsXHJcbiAgICAgIHBvcnQ6IDUxOTksXHJcbiAgICAgIHN0cmljdFBvcnQ6IHRydWUsXHJcbiAgICAgIC8vIEtlZXAgVml0ZSdzIEhNUiBzb2NrZXQgb24gYSBkZWRpY2F0ZWQgcGF0aC4gWGlhbmFpYm90J3MgYXBwIFdlYlNvY2tldCBpc1xyXG4gICAgICAvLyBvcGVuZWQgZGlyZWN0bHkgZnJvbSB0aGUgYnJvd3NlciB0byB0aGUgZ2F0ZXdheSwgc28gdGhlIGRldiBzZXJ2ZXJcclxuICAgICAgLy8gc2hvdWxkIG5ldmVyIHByb3h5IFdlYlNvY2tldCB1cGdyYWRlcy5cclxuICAgICAgaG1yOiB7XHJcbiAgICAgICAgaG9zdDogXCIxMjcuMC4wLjFcIixcclxuICAgICAgICBwYXRoOiBobXJQYXRoLFxyXG4gICAgICB9LFxyXG4gICAgICBwcm94eToge1xyXG4gICAgICAgIFwiL3dlYnVpXCI6IHsgdGFyZ2V0LCBjaGFuZ2VPcmlnaW46IHRydWUgfSxcclxuICAgICAgICBcIi9hcGlcIjogeyB0YXJnZXQsIGNoYW5nZU9yaWdpbjogdHJ1ZSB9LFxyXG4gICAgICAgIFwiL2F1dGhcIjogeyB0YXJnZXQsIGNoYW5nZU9yaWdpbjogdHJ1ZSB9LFxyXG4gICAgICB9LFxyXG4gICAgfSxcclxuICAgIHRlc3Q6IHtcclxuICAgICAgZW52aXJvbm1lbnQ6IFwiaGFwcHktZG9tXCIsXHJcbiAgICAgIGdsb2JhbHM6IHRydWUsXHJcbiAgICAgIHNldHVwRmlsZXM6IFtcIi4vc3JjL3Rlc3RzL3NldHVwLnRzXCJdLFxyXG4gICAgfSxcclxuICB9O1xyXG59KTtcclxuIl0sCiAgIm1hcHBpbmdzIjogIjtBQUFrUSxTQUFTLGNBQWMsZUFBZTtBQUN4UyxPQUFPLFdBQVc7QUFDbEIsT0FBTyxVQUFVO0FBRmpCLElBQU0sbUNBQW1DO0FBSXpDLElBQU8sc0JBQVEsYUFBYSxDQUFDLEVBQUUsS0FBSyxNQUFNO0FBQ3hDLFFBQU0sTUFBTSxRQUFRLE1BQU0sUUFBUSxJQUFJLEdBQUcsRUFBRTtBQUMzQyxRQUFNLFNBQVMsSUFBSSxxQkFBcUI7QUFDeEMsUUFBTSxVQUFVO0FBRWhCLFNBQU87QUFBQSxJQUNMLFNBQVMsQ0FBQyxNQUFNLENBQUM7QUFBQSxJQUNqQixTQUFTO0FBQUEsTUFDUCxPQUFPO0FBQUEsUUFDTCxLQUFLLEtBQUssUUFBUSxrQ0FBVyxPQUFPO0FBQUEsTUFDdEM7QUFBQSxJQUNGO0FBQUEsSUFDQSxjQUFjO0FBQUE7QUFBQTtBQUFBO0FBQUE7QUFBQSxNQUtaLFNBQVMsQ0FBQyx3QkFBd0I7QUFBQSxJQUNwQztBQUFBLElBQ0EsT0FBTztBQUFBLE1BQ0wsUUFBUSxLQUFLLFFBQVEsa0NBQVcsdUJBQXVCO0FBQUEsTUFDdkQsYUFBYTtBQUFBLE1BQ2IsV0FBVztBQUFBLE1BQ1gsZUFBZTtBQUFBLFFBQ2IsUUFBUTtBQUFBLFVBQ04sYUFBYSxJQUFJO0FBQ2YsZ0JBQUksR0FBRyxTQUFTLDhCQUE4QixHQUFHO0FBQy9DO0FBQUEsWUFDRjtBQU9BLGdCQUNFLEdBQUcsU0FBUyxxQkFBcUIsS0FDOUIsR0FBRyxTQUFTLHlCQUF5QixLQUNyQyxHQUFHLFNBQVMseUJBQXlCLEdBQ3hDO0FBQ0EscUJBQU87QUFBQSxZQUNUO0FBQ0EsZ0JBQ0UsR0FBRyxTQUFTLHVDQUF1QyxLQUNoRCxHQUFHLFNBQVMsNkJBQTZCLEdBQzVDO0FBQ0EscUJBQU87QUFBQSxZQUNUO0FBQ0EsZ0JBQ0UsR0FBRyxTQUFTLDZCQUE2QixLQUN0QyxHQUFHLFNBQVMsc0JBQXNCLEtBQ2xDLEdBQUcsU0FBUyxzQkFBc0IsS0FDbEMsR0FBRyxTQUFTLHNCQUFzQixLQUNsQyxHQUFHLFNBQVMscUJBQXFCLEtBQ2pDLEdBQUcsU0FBUyxvQkFBb0IsS0FDaEMsR0FBRyxTQUFTLHdCQUF3QixLQUNwQyxHQUFHLFNBQVMscUJBQXFCLEdBQ3BDO0FBQ0EscUJBQU87QUFBQSxZQUNUO0FBQ0EsZ0JBQUksR0FBRyxTQUFTLG9CQUFvQixHQUFHO0FBQ3JDLHFCQUFPO0FBQUEsWUFDVDtBQUFBLFVBQ0Y7QUFBQSxRQUNGO0FBQUEsTUFDRjtBQUFBLElBQ0Y7QUFBQSxJQUNBLFFBQVE7QUFBQSxNQUNOLE1BQU07QUFBQSxNQUNOLE1BQU07QUFBQSxNQUNOLFlBQVk7QUFBQTtBQUFBO0FBQUE7QUFBQSxNQUlaLEtBQUs7QUFBQSxRQUNILE1BQU07QUFBQSxRQUNOLE1BQU07QUFBQSxNQUNSO0FBQUEsTUFDQSxPQUFPO0FBQUEsUUFDTCxVQUFVLEVBQUUsUUFBUSxjQUFjLEtBQUs7QUFBQSxRQUN2QyxRQUFRLEVBQUUsUUFBUSxjQUFjLEtBQUs7QUFBQSxRQUNyQyxTQUFTLEVBQUUsUUFBUSxjQUFjLEtBQUs7QUFBQSxNQUN4QztBQUFBLElBQ0Y7QUFBQSxJQUNBLE1BQU07QUFBQSxNQUNKLGFBQWE7QUFBQSxNQUNiLFNBQVM7QUFBQSxNQUNULFlBQVksQ0FBQyxzQkFBc0I7QUFBQSxJQUNyQztBQUFBLEVBQ0Y7QUFDRixDQUFDOyIsCiAgIm5hbWVzIjogW10KfQo=
