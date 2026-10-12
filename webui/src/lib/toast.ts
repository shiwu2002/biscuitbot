/**
 * 轻量全局 toast：任何模块都可以 showToast，ToastHost（挂在 App 根部）负责渲染。
 * 不引入第三方依赖，样式与 App.tsx 里 restartToast 的顶部居中气泡保持一致。
 */

export type ToastKind = "success" | "error" | "info";

export interface AppToast {
  id: number;
  kind: ToastKind;
  message: string;
}

type ToastListener = (toast: AppToast) => void;

const listeners = new Set<ToastListener>();
let nextToastId = 1;

export function showToast(message: string, kind: ToastKind = "info"): void {
  const toast: AppToast = { id: nextToastId, kind, message };
  nextToastId += 1;
  listeners.forEach((listener) => listener(toast));
}

export function subscribeToasts(listener: ToastListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
