import { useCallback, useEffect, useState } from "react";

import { fetchSkills } from "@/lib/api";
import type { SkillSummary } from "@/lib/types";

export function useSkills(token: string): {
  skills: SkillSummary[];
  reload: () => void;
} {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [reloadCount, setReloadCount] = useState(0);

  const reload = useCallback(() => setReloadCount((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;
    fetchSkills(token)
      .then(({ skills: nextSkills }) => !cancelled && setSkills(nextSkills))
      .catch(() => !cancelled && setSkills([]));
    return () => {
      cancelled = true;
    };
  }, [token, reloadCount]);

  return { skills, reload };
}
