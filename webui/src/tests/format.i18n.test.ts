import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setAppLanguage } from "@/i18n";
import { fmtDateTime, formatTurnLatency, relativeTime } from "@/lib/format";

describe("localized format helpers", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-04-18T12:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("formats relative time using the active locale", async () => {
    const value = "2026-04-18T11:59:00Z";

    await setAppLanguage("zh-CN");
    const simplified = relativeTime(value);

    await setAppLanguage("zh-TW");
    const traditional = relativeTime(value);

    expect(simplified).toBe(
      new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" }).format(
        -1,
        "minute",
      ),
    );
    expect(traditional).toBe(
      new Intl.RelativeTimeFormat("zh-TW", { numeric: "auto" }).format(
        -1,
        "minute",
      ),
    );
    expect(simplified).not.toBe(traditional);
  });

  it("formats date-time using the active locale", async () => {
    const value = "2026-04-18T08:30:00Z";
    const date = new Date(value);

    await setAppLanguage("zh-CN");
    const simplified = fmtDateTime(value);

    await setAppLanguage("zh-TW");
    const traditional = fmtDateTime(value);

    expect(simplified).toBe(
      new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(date),
    );
    expect(traditional).toBe(
      new Intl.DateTimeFormat("zh-TW", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(date),
    );
    expect(simplified).not.toBe(traditional);
  });

  it("formats turn latency with locale-aware units", async () => {
    await setAppLanguage("zh-CN");
    const subMinute = formatTurnLatency(2400, "zh-CN");
    expect(subMinute).toBe(
      new Intl.NumberFormat("zh-CN", {
        style: "unit",
        unit: "second",
        unitDisplay: "narrow",
        maximumFractionDigits: 1,
        minimumFractionDigits: 0,
      }).format(2.4),
    );

    const minutePlus = formatTurnLatency(90_000, "zh-CN");
    expect(minutePlus).toContain("分");
    expect(minutePlus).toContain("秒");
  });
});
