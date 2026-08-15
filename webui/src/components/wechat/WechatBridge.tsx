import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { QRCodeSVG } from "qrcode.react";
import { Check, Loader2, RefreshCw, Smartphone } from "lucide-react";

import { Button } from "@/components/ui/button";
import { fetchWeixinLoginQr, pollWeixinLoginStatus } from "@/lib/api";

type WechatStage =
  | "idle"
  | "loading"
  | "qr"
  | "scanned"
  | "confirmed"
  | "expired"
  | "error";

/** 可选「接入微信」区块：扫码 → 轮询 → 确认。可复用于引导页与设置页。 */
export function WechatBridge({
  token,
  onConfirmed,
}: {
  token: string;
  onConfirmed?: () => void;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, options?: Record<string, unknown>) =>
    t(key, { ...(options ?? {}), defaultValue: fallback });

  const [stage, setStage] = useState<WechatStage>("idle");
  const [qrcodeId, setQrcodeId] = useState<string | null>(null);
  const [qrContent, setQrContent] = useState("");
  const [error, setError] = useState<string | null>(null);

  const onConfirmedRef = useRef(onConfirmed);
  useEffect(() => {
    onConfirmedRef.current = onConfirmed;
  }, [onConfirmed]);

  const start = async () => {
    setStage("loading");
    setError(null);
    setQrcodeId(null);
    try {
      const qr = await fetchWeixinLoginQr(token);
      setQrcodeId(qr.qrcode_id);
      setQrContent(qr.qr_content);
      setStage("qr");
    } catch (e) {
      setError((e as Error).message);
      setStage("error");
    }
  };

  useEffect(() => {
    if (!qrcodeId) return;
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      if (cancelled) return;
      try {
        const status = await pollWeixinLoginStatus(token, qrcodeId);
        if (cancelled) return;
        if (status.confirmed) {
          setStage("confirmed");
          onConfirmedRef.current?.();
          return;
        }
        if (status.expired) {
          setStage("expired");
          return;
        }
        setStage(status.status === "scaned_but_redirect" ? "scanned" : "qr");
        timer = window.setTimeout(poll, 2000);
      } catch (e) {
        if (cancelled) return;
        setError((e as Error).message);
        setStage("error");
      }
    };

    poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [qrcodeId, token]);

  if (stage === "idle") {
    return (
      <div className="rounded-2xl border border-border/50 bg-muted/20 p-4">
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="flex items-center gap-1.5 text-[13px] font-semibold text-foreground">
              <Smartphone className="h-4 w-4 text-muted-foreground" aria-hidden />
              {tx("setup.wechat.title", "接入微信（可选）")}
            </p>
            <p className="mt-0.5 text-[12px] leading-5 text-muted-foreground">
              {tx(
                "setup.wechat.subtitle",
                "扫码登录后，即可在微信中与 biscuitbot 对话。",
              )}
            </p>
          </div>
          <Button type="button" variant="outline" size="sm" onClick={start}>
            {tx("setup.wechat.start", "扫码连接")}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-2xl border border-border/50 bg-muted/20 p-4">
      {stage === "loading" ? (
        <div className="flex items-center justify-center gap-2 py-6 text-[13px] text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          {tx("setup.wechat.loading", "正在获取二维码…")}
        </div>
      ) : stage === "qr" || stage === "scanned" ? (
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="rounded-xl bg-white p-2">
            <QRCodeSVG value={qrContent} size={168} level="M" />
          </div>
          <p className="text-[13px] font-medium text-foreground">
            {stage === "scanned"
              ? tx("setup.wechat.scanned", "已扫码，请在手机上确认登录")
              : tx("setup.wechat.scanHint", "请使用微信扫一扫二维码")}
          </p>
          <p className="text-[12px] text-muted-foreground">
            {tx("setup.wechat.pollHint", "二维码有效期有限，过期后会自动刷新。")}
          </p>
        </div>
      ) : stage === "confirmed" ? (
        <div className="flex flex-col items-center gap-2 py-4 text-center">
          <span className="grid h-10 w-10 place-items-center rounded-full bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
            <Check className="h-5 w-5" aria-hidden />
          </span>
          <p className="text-[13px] font-semibold text-foreground">
            {tx("setup.wechat.confirmed", "微信已连接成功")}
          </p>
          <p className="text-[12px] text-muted-foreground">
            {tx(
              "setup.wechat.confirmedHint",
              "重启网关后，即可在微信中与 biscuitbot 对话。",
            )}
          </p>
        </div>
      ) : stage === "expired" ? (
        <div className="flex flex-col items-center gap-2 py-4 text-center">
          <p className="text-[13px] text-foreground">
            {tx("setup.wechat.expired", "二维码已过期")}
          </p>
          <Button type="button" variant="outline" size="sm" onClick={start}>
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            {tx("setup.wechat.refresh", "重新获取")}
          </Button>
        </div>
      ) : (
        <div className="flex flex-col items-center gap-2 py-4 text-center">
          <p className="text-[13px] text-destructive">
            {tx("setup.wechat.error", "获取二维码失败：{{message}}", {
              message: error ?? "",
            })}
          </p>
          <Button type="button" variant="outline" size="sm" onClick={start}>
            {tx("setup.wechat.retry", "重试")}
          </Button>
        </div>
      )}
    </div>
  );
}
