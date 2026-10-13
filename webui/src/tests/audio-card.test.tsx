import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AudioCard } from "@/components/knowledge/AudioCard";
import type { Asset } from "@/lib/types";

const AUDIO: Asset = {
  id: "dHRzLm1wMw",
  name: "tts_1234567890ab.mp3",
  kind: "audio",
  size: 4096,
  created_at: "2026-08-17T12:00:00+08:00",
  caption: "",
  media_url: "/api/sessions/websocket%3Achat-1/workspace-media/sig/mp3",
};

describe("AudioCard 音频卡面", () => {
  it("给出播放键与进度条，元数据到达后显示总时长", () => {
    render(<AudioCard asset={AUDIO} />);

    expect(screen.getByRole("button", { name: "播放 tts_1234567890ab.mp3" })).toBeInTheDocument();

    const audio = document.querySelector("audio");
    expect(audio).not.toBeNull();
    expect(audio).toHaveAttribute("src", AUDIO.media_url);

    // jsdom 不会真的解码音频：手动给个时长再触发 loadedmetadata，等同于真实浏览器。
    Object.defineProperty(audio, "duration", { value: 65.4, configurable: true });
    fireEvent.loadedMetadata(audio!);

    expect(screen.getByText("01:05")).toBeInTheDocument();
    expect(screen.getByText("00:00")).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "播放进度" })).toBeEnabled();
  });

  it("时长未知时进度条禁用，总时长留空而不是假的 00:00", () => {
    render(<AudioCard asset={AUDIO} />);

    expect(screen.getByRole("slider", { name: "播放进度" })).toBeDisabled();
    expect(screen.getByText("00:00")).toBeInTheDocument();
    expect(screen.getByText("--:--")).toBeInTheDocument();
  });
});
