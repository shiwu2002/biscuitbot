import { useEffect, useState } from "react";

const DESKTOP_QUERY = "(min-width: 1024px)";

function matchDesktop(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(DESKTOP_QUERY).matches;
}

/**
 * 视口是否处于桌面断点（≥1024px）。用于让浏览器桌面端复用与桌面壳（native）
 * 相同的全宽顶栏布局，而移动端继续走 Sheet 抽屉。
 */
export function useIsDesktop(): boolean {
  const [isDesktop, setIsDesktop] = useState<boolean>(matchDesktop);

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const query = window.matchMedia(DESKTOP_QUERY);
    const onChange = (event: MediaQueryListEvent) => setIsDesktop(event.matches);
    setIsDesktop(query.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  return isDesktop;
}
