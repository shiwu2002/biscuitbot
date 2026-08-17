import "@testing-library/jest-dom/vitest";
import { beforeEach } from "vitest";

import i18n from "@/i18n";

function createTestStorage(): Storage {
  const store = new Map<string, string>();
  return {
    get length() {
      return store.size;
    },
    clear() {
      store.clear();
    },
    getItem(key: string) {
      return store.get(String(key)) ?? null;
    },
    key(index: number) {
      return Array.from(store.keys())[index] ?? null;
    },
    removeItem(key: string) {
      store.delete(String(key));
    },
    setItem(key: string, value: string) {
      store.set(String(key), String(value));
    },
  };
}

if (typeof window !== "undefined" && typeof localStorage.setItem !== "function") {
  const storage = createTestStorage();
  Object.defineProperty(window, "localStorage", {
    value: storage,
    configurable: true,
  });
  Object.defineProperty(globalThis, "localStorage", {
    value: storage,
    configurable: true,
    writable: true,
  });
}

// happy-dom doesn't ship with ``crypto.randomUUID``; shim a tiny v4-ish helper.
if (!("randomUUID" in globalThis.crypto)) {
  Object.defineProperty(globalThis.crypto, "randomUUID", {
    value: () =>
      "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        const v = c === "x" ? r : (r & 0x3) | 0x8;
        return v.toString(16);
      }),
    configurable: true,
  });
}

// happy-dom 默认 viewport 为 1024px，`(min-width: 1024px)` 命中会令 useIsDesktop
// 误判为桌面端、启用全宽顶栏布局。这里默认提供「窄屏」matchMedia，需要桌面端
// 行为的测试再自行 vi.stubGlobal("matchMedia", ...) 覆盖。
function defaultMatchMedia(query: string): MediaQueryList {
  return {
    matches: false,
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {
      return false;
    },
  } as MediaQueryList;
}

Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: defaultMatchMedia,
});

beforeEach(async () => {
  // 应用默认中文（defaultLocale=zh-CN，仅支持 zh-CN/zh-TW）。
  // 旧测试按英文产品编写，本处切到英文会触发 i18next 回退中文，
  // 导致断言拿英文比中文而失败。
  await i18n.changeLanguage("zh-CN");
  document.documentElement.lang = "zh-CN";
  document.title = "biscuitbot";
  localStorage.setItem("biscuitbot.locale", "zh-CN");
});
