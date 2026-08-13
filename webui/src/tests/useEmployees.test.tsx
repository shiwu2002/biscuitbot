import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useEmployees } from "@/hooks/useEmployees";
import { fetchEmployees } from "@/lib/api";
import type { Employee } from "@/lib/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchEmployees: vi.fn(),
  };
});

const CLIP_MASTER: Employee = {
  id: "clip-master",
  name: "剪辑高手",
  avatar: "🎬",
  system_prompt: "你是一名剪辑高手数字人员工……",
  skills: ["jianying-editor"],
  enabled: true,
  created_at: "2026-08-13T00:00:00Z",
};

describe("useEmployees", () => {
  beforeEach(() => {
    vi.mocked(fetchEmployees).mockReset();
  });

  it("fetches the employee catalog on mount", async () => {
    vi.mocked(fetchEmployees).mockResolvedValue({ employees: [CLIP_MASTER] });
    const { result } = renderHook(() => useEmployees("tok"));
    await waitFor(() => expect(result.current.employees).toHaveLength(1));
    expect(result.current.employees[0]?.id).toBe("clip-master");
    expect(result.current.employees[0]?.name).toBe("剪辑高手");
  });

  it("falls back to an empty list when the request fails", async () => {
    vi.mocked(fetchEmployees).mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useEmployees("tok"));
    await waitFor(() => expect(result.current.employees).toEqual([]));
  });

  it("reloads on request", async () => {
    vi.mocked(fetchEmployees).mockResolvedValueOnce({ employees: [] });
    const { result } = renderHook(() => useEmployees("tok"));
    await waitFor(() => expect(result.current.employees).toEqual([]));

    vi.mocked(fetchEmployees).mockResolvedValueOnce({ employees: [CLIP_MASTER] });
    act(() => result.current.reload());
    await waitFor(() => expect(result.current.employees).toHaveLength(1));
  });
});
