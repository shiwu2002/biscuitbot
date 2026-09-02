import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getHostApi } from "@/lib/runtime";

/**
 * 验证桌面壳未注入 `window.biscuitbotHost` 时，getHostApi() 会从 `window.__TAURI__`
 * 派生出一个仅含 pickFolder 的宿主对象（走系统目录选择框）；浏览器下二者皆无时返回 null。
 */
describe("getHostApi tauri derivation", () => {
  beforeEach(() => {
    Reflect.deleteProperty(window, "biscuitbotHost");
    Reflect.deleteProperty(window, "__TAURI__");
  });

  afterEach(() => {
    Reflect.deleteProperty(window, "biscuitbotHost");
    Reflect.deleteProperty(window, "__TAURI__");
  });

  it("returns null when both biscuitbotHost and __TAURI__ are absent (browser)", () => {
    expect(getHostApi()).toBeNull();
  });

  it("prefers an already-injected window.biscuitbotHost", () => {
    const injected = { pickFolder: vi.fn(async () => "/host/path") };
    Object.defineProperty(window, "biscuitbotHost", { configurable: true, value: injected });
    expect(getHostApi()).toBe(injected);
  });

  it("derives a pickFolder host from window.__TAURI__ and normalizes a single path", async () => {
    const invoke = vi.fn(async () => "/Users/test/native-project");
    Object.defineProperty(window, "__TAURI__", {
      configurable: true,
      value: { core: { invoke } },
    });

    const host = getHostApi();
    expect(host).not.toBeNull();
    await expect(host!.pickFolder()).resolves.toBe("/Users/test/native-project");
    expect(invoke).toHaveBeenCalledWith("plugin:dialog|open", {
      options: { directory: true, multiple: false, title: "选择项目工作目录" },
    });
  });

  it("returns the first path when the dialog returns an array", async () => {
    const invoke = vi.fn(async () => ["/a", "/b"] as unknown);
    Object.defineProperty(window, "__TAURI__", {
      configurable: true,
      value: { core: { invoke } },
    });
    await expect(getHostApi()!.pickFolder()).resolves.toBe("/a");
  });

  it("returns null when the dialog is cancelled", async () => {
    const invoke = vi.fn(async () => null);
    Object.defineProperty(window, "__TAURI__", {
      configurable: true,
      value: { core: { invoke } },
    });
    await expect(getHostApi()!.pickFolder()).resolves.toBeNull();
  });

  it("caches the derived host so subsequent calls are stable", () => {
    Object.defineProperty(window, "__TAURI__", {
      configurable: true,
      value: { core: { invoke: vi.fn(async () => null) } },
    });
    const first = getHostApi();
    expect(getHostApi()).toBe(first);
  });
});
