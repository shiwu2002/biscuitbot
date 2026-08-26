import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { formatCellValue, parseCsv } from "@/lib/file-preview";

import { PreviewError, PreviewLoading } from "./PreviewStates";

interface SheetData {
  name: string;
  rows: unknown[][];
}

/**
 * Excel (.xlsx) / CSV 表格预览。
 *
 * - CSV：TextDecoder 解码后走内置 `parseCsv`。
 * - XLSX：动态 import `xlsx`，`sheet_to_json` 只取单元格值，然后手工构建
 *   React `<table>` —— 不信任 `sheet_to_html`，绝不把 HTML 字符串注入 React 树。
 */
export function SpreadsheetViewer({
  buffer,
  kind,
  url,
}: {
  buffer: ArrayBuffer;
  kind: "xlsx" | "csv";
  url?: string;
}) {
  const { t } = useTranslation();
  const [sheets, setSheets] = useState<SheetData[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setError(false);
    setSheets(null);
    (async () => {
      try {
        if (kind === "csv") {
          const text = new TextDecoder("utf-8", { fatal: false }).decode(buffer);
          const rows = parseCsv(text);
          if (!cancelled) setSheets([{ name: "", rows }]);
          return;
        }
        const XLSX = await import("xlsx");
        if (cancelled) return;
        const workbook = XLSX.read(buffer, { type: "array" });
        const parsed: SheetData[] = workbook.SheetNames.map((name) => {
          const worksheet = workbook.Sheets[name];
          const rows = XLSX.utils.sheet_to_json(worksheet, {
            header: 1,
            defval: "",
          }) as unknown[][];
          return { name, rows };
        });
        if (!cancelled) setSheets(parsed);
      } catch {
        if (!cancelled) setError(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [buffer, kind]);

  if (error) {
    return (
      <PreviewError
        url={url}
        message={t("documentPreview.loadFailed", { defaultValue: "预览失败，请下载查看" })}
      />
    );
  }
  if (!sheets) {
    return (
      <PreviewLoading label={t("documentPreview.loading", { defaultValue: "正在加载预览…" })} />
    );
  }
  if (sheets.length === 0) {
    return (
      <p className="px-4 py-3 text-xs text-muted-foreground">
        {t("documentPreview.emptySheet", { defaultValue: "此工作表为空" })}
      </p>
    );
  }

  return (
    <div className="max-h-[36rem] overflow-y-auto px-3 py-2">
      {sheets.map((sheet, sheetIndex) => (
        <section key={`${sheet.name}-${sheetIndex}`} className="mb-3 last:mb-0">
          {sheet.name ? (
            <h3 className="mb-1.5 px-1 text-xs font-semibold text-muted-foreground">
              {sheet.name}
            </h3>
          ) : null}
          {sheet.rows.length === 0 ? (
            <p className="px-1 py-3 text-xs text-muted-foreground">
              {t("documentPreview.emptySheet", { defaultValue: "此工作表为空" })}
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-xs">
                <thead>
                  <tr>
                    {sheet.rows[0].map((cell, columnIndex) => (
                      <th
                        key={columnIndex}
                        className="border border-border/60 bg-muted/50 px-2 py-1 text-left font-medium text-muted-foreground"
                      >
                        {formatCellValue(cell)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sheet.rows.slice(1).map((row, rowIndex) => (
                    <tr key={rowIndex}>
                      {row.map((cell, columnIndex) => (
                        <td
                          key={columnIndex}
                          className="border border-border/60 px-2 py-1 text-foreground/90"
                        >
                          {formatCellValue(cell)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      ))}
    </div>
  );
}
