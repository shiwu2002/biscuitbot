import { useEffect, useRef, useState } from "react";
import { AudioLines, Pause, Play } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { Asset } from "@/lib/types";

/** mm:ss —— 音频时长够用，不引时间库。 */
function formatClock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "--:--";
  const total = Math.floor(seconds);
  const mm = Math.floor(total / 60);
  const ss = total % 60;
  return `${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
}

/**
 * 音频资产卡面：自带播放/暂停与进度条的紧凑播放器。
 *
 * 不用原生 ``<audio controls>``：塞进统一高度的卡面会留一大片空白，各浏览器控件
 * 外观也不一致。这里只留播放键、进度条、时间，音频元素本身隐藏。
 * 播放状态全部由媒体事件驱动（onPlay/onPause/onEnded）而不是手动置位 —— 这样系统
 * 暂停、播完、seek 结束都能对上。
 */
export function AudioCard({ asset }: { asset: Asset }) {
  const { t } = useTranslation();
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState(0);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setPlaying(false);
    setCurrent(0);
    setDuration(0);
    setFailed(false);
  }, [asset.media_url]);

  const toggle = () => {
    const audio = audioRef.current;
    if (!audio || failed) return;
    if (audio.paused) {
      // jsdom 里 play() 没有返回值，所以不能直接 .catch()
      const started = audio.play();
      if (started && typeof started.catch === "function") {
        started.catch(() => setFailed(true));
      }
    } else {
      audio.pause();
    }
  };

  return (
    <div className="flex h-full w-full flex-col items-center justify-center gap-2.5 bg-gradient-to-br from-emerald-500/12 via-muted/30 to-muted/45 px-3.5">
      <button
        type="button"
        onClick={toggle}
        aria-label={
          playing
            ? t("knowledge.assets.pause", { name: asset.name, defaultValue: "暂停 {{name}}" })
            : t("knowledge.assets.play", { name: asset.name, defaultValue: "播放 {{name}}" })
        }
        className="flex h-10 w-10 items-center justify-center rounded-full bg-background/80 text-emerald-600 shadow-sm ring-1 ring-border/60 transition-transform duration-150 hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring dark:text-emerald-300"
      >
        {failed ? (
          <AudioLines className="h-4 w-4" aria-hidden />
        ) : playing ? (
          <Pause className="h-4 w-4 fill-current" aria-hidden />
        ) : (
          <Play className="ml-0.5 h-4 w-4 fill-current" aria-hidden />
        )}
      </button>

      <input
        type="range"
        min={0}
        max={duration || 0}
        step={0.1}
        value={current}
        disabled={duration === 0}
        onChange={(event) => {
          const audio = audioRef.current;
          const next = Number(event.target.value);
          if (audio) audio.currentTime = next;
          setCurrent(next);
        }}
        aria-label={t("knowledge.assets.seek", { defaultValue: "播放进度" })}
        className="w-full max-w-[9.5rem] accent-emerald-500 disabled:opacity-50"
      />

      <div className="flex w-full max-w-[9.5rem] items-center justify-between text-[10.5px] tabular-nums leading-4 text-muted-foreground">
        <span>{formatClock(current)}</span>
        {/* 元数据还没到时总时长是 0：宁可不显示，也别摆一个假的 00:00 */}
        <span>{duration > 0 ? formatClock(duration) : "--:--"}</span>
      </div>

      <audio
        ref={audioRef}
        src={asset.media_url}
        preload="metadata"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onTimeUpdate={(event) => setCurrent(event.currentTarget.currentTime)}
        onLoadedMetadata={(event) => setDuration(event.currentTarget.duration)}
        onError={() => setFailed(true)}
        className="hidden"
      />
    </div>
  );
}
