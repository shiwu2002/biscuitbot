"use client";

import { useState } from "react";

import { cn } from "@/lib/utils";

/** 头像制：avatar 字段为图片文件名（如 img_xxx.jpg）或 http(s) 图片 URL 时按图片渲染，否则按 emoji 文本渲染。 */
const IMAGE_AVATAR_RE =
  /^[A-Za-z0-9][A-Za-z0-9._-]*\.(jpg|jpeg|png|gif|webp|bmp|avif)$/i;
const REMOTE_AVATAR_RE = /^https?:\/\/[^\s/]+\S*$/i;

export function isImageAvatar(
  avatar: string | null | undefined,
): avatar is string {
  if (typeof avatar !== "string") return false;
  const a = avatar.trim();
  if (REMOTE_AVATAR_RE.test(a)) return true;
  return IMAGE_AVATAR_RE.test(a);
}

/** 图片头像的访问地址：本地文件名走网关 /api/avatars 路由（静态资源免鉴权），http(s) URL 直接用。 */
export function employeeAvatarSrc(avatar: string): string {
  const a = avatar.trim();
  if (REMOTE_AVATAR_RE.test(a)) return a;
  return `/api/avatars/${encodeURIComponent(a)}`;
}

/**
 * 数字员工头像。
 *
 * avatar 为图片文件名时渲染 <img>（加载失败降级为默认占位符），
 * 否则按 emoji 文本渲染。className 沿用原包装盒子的样式（flex 居中 + 圆角），
 * 图片通过 rounded-[inherit] 继承盒子的圆角。
 */
export function EmployeeAvatar({
  avatar,
  className,
  imgClassName,
  fallback = "🧑‍💼",
  alt = "avatar",
}: {
  avatar?: string;
  className?: string;
  imgClassName?: string;
  fallback?: string;
  alt?: string;
}) {
  const [failed, setFailed] = useState(false);
  if (isImageAvatar(avatar) && !failed) {
    return (
      <span className={cn("overflow-hidden", className)} aria-hidden>
        <img
          src={employeeAvatarSrc(avatar)}
          alt={alt}
          className={cn("h-full w-full rounded-[inherit] object-cover", imgClassName)}
          onError={() => setFailed(true)}
        />
      </span>
    );
  }
  return (
    <span className={className} aria-hidden>
      {avatar || fallback}
    </span>
  );
}
