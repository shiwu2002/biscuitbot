import { useCallback, useEffect, useState } from "react";

import { fetchEmployees } from "@/lib/api";
import type { Employee } from "@/lib/types";

/** 数字人员工目录加载：拉取 ``employees.json`` 里的员工列表并暴露 reload。 */
export function useEmployees(token: string): {
  employees: Employee[];
  reload: () => void;
} {
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [reloadCount, setReloadCount] = useState(0);

  const reload = useCallback(() => setReloadCount((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;
    fetchEmployees(token)
      .then(({ employees: nextEmployees }) =>
        !cancelled && setEmployees(nextEmployees),
      )
      .catch(() => !cancelled && setEmployees([]));
    return () => {
      cancelled = true;
    };
  }, [token, reloadCount]);

  return { employees, reload };
}
