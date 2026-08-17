import { useEffect, useRef, useState } from "react";
import { Film, Play } from "lucide-react";

import type { Asset } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 视频资产卡面：用隐藏的 video 抓取首帧（canvas → dataURL）作为封面，
 * 点击后切到可播放的 controls 视频。避免直接黑幕。
 */
export function VideoCard({ asset }: { asset: Asset }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [poster, setPoster] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    let cancelled = false;

    const grabFirstFrame = () => {
      if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;
      try {
        video.currentTime = 0.5; // 跳到 0.5s，跳过 AIGC 视频常见的淡入黑屏
      } catch {
        /* 该格式不支持 seek，保持占位 */
      }
    };
    const drawFrame = () => {
      if (cancelled) return;
      try {
        const { videoWidth, videoHeight } = video;
        if (!videoWidth || !videoHeight) return;
        const canvas = document.createElement("canvas");
        canvas.width = videoWidth;
        canvas.height = videoHeight;
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        setPoster(canvas.toDataURL("image/jpeg", 0.7));
      } catch {
        /* 抓帧失败（如跨域污染），保持占位 */
      }
    };

    video.addEventListener("loadeddata", grabFirstFrame);
    video.addEventListener("seeked", drawFrame);
    return () => {
      cancelled = true;
      video.removeEventListener("loadeddata", grabFirstFrame);
      video.removeEventListener("seeked", drawFrame);
    };
  }, [asset.media_url]);

  const handlePlay = () => {
    if (failed) return;
    setPlaying(true);
    videoRef.current?.play().catch(() => {});
  };

  return (
    <div className="relative aspect-square w-full overflow-hidden bg-black">
      {/* 抓帧用；播放时显示并接管 */}
      <video
        ref={videoRef}
        src={asset.media_url}
        preload="metadata"
        controls={playing}
        autoPlay={playing}
        onError={() => setFailed(true)}
        className={cn(
          "h-full w-full object-cover",
          !playing && "pointer-events-none absolute inset-0 h-0 w-0 opacity-0",
        )}
      />
      {!playing ? (
        <button
          type="button"
          onClick={handlePlay}
          aria-label={`播放 ${asset.name}`}
          className="group/video relative block h-full w-full"
        >
          {poster ? (
            <img
              src={poster}
              alt={asset.caption || asset.name}
              className="h-full w-full object-cover"
            />
          ) : (
            <div className="flex h-full w-full flex-col items-center justify-center gap-1.5 bg-muted/40 text-muted-foreground">
              <Film className="h-6 w-6 opacity-50" aria-hidden />
            </div>
          )}
          {!failed ? (
            <div className="absolute inset-0 flex items-center justify-center bg-black/15 transition-colors group-hover/video:bg-black/25">
              <span className="flex h-11 w-11 items-center justify-center rounded-full bg-black/60 text-white shadow-lg ring-1 ring-white/20">
                <Play className="ml-0.5 h-5 w-5 fill-current" aria-hidden />
              </span>
            </div>
          ) : null}
        </button>
      ) : null}
    </div>
  );
}
