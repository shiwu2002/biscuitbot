import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { EmployeePicker } from "@/components/thread/EmployeePicker";
import type { Employee } from "@/lib/types";

const CLIP_MASTER: Employee = {
  id: "clip-master",
  name: "剪辑高手",
  avatar: "🎬",
  system_prompt: "你是一名剪辑高手……",
  skills: ["jianying-editor"],
  enabled: true,
  created_at: "2026-08-13T00:00:00Z",
};

const WRITER: Employee = {
  id: "copywriter",
  name: "文案写手",
  avatar: "✍️",
  system_prompt: "你是一名文案写手……",
  skills: [],
  enabled: true,
  created_at: "2026-08-13T00:00:00Z",
};

const DISABLED: Employee = {
  id: "on-leave",
  name: "休假员工",
  system_prompt: "……",
  skills: [],
  enabled: false,
  created_at: "2026-08-13T00:00:00Z",
};

describe("EmployeePicker", () => {
  it("renders the picker entry when no employee is selected", () => {
    render(
      <EmployeePicker employees={[CLIP_MASTER]} selected={null} onChange={vi.fn()} />,
    );
    expect(screen.getByText("数字人员工")).toBeInTheDocument();
  });

  it("renders nothing when no employees are available", () => {
    render(<EmployeePicker employees={[]} selected={null} onChange={vi.fn()} />);
    expect(screen.queryByText("数字人员工")).not.toBeInTheDocument();
  });

  it("renders nothing without an onChange handler", () => {
    render(<EmployeePicker employees={[CLIP_MASTER]} selected={null} />);
    expect(screen.queryByText("数字人员工")).not.toBeInTheDocument();
  });

  it("selects an employee from the dropdown", async () => {
    const onChange = vi.fn();
    render(
      <EmployeePicker employees={[CLIP_MASTER, WRITER]} selected={null} onChange={onChange} />,
    );
    fireEvent.pointerDown(screen.getByText("数字人员工"));
    fireEvent.click(await screen.findByRole("menuitem", { name: "文案写手" }));
    expect(onChange).toHaveBeenCalledWith(WRITER);
  });

  it("offers the plain main-agent option from the dropdown", async () => {
    const onChange = vi.fn();
    render(
      <EmployeePicker employees={[CLIP_MASTER]} selected={null} onChange={onChange} />,
    );
    fireEvent.pointerDown(screen.getByText("数字人员工"));
    fireEvent.click(await screen.findByRole("menuitem", { name: "主智能体（不绑定员工）" }));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("hides disabled employees from the dropdown", async () => {
    render(
      <EmployeePicker employees={[CLIP_MASTER, DISABLED]} selected={null} onChange={vi.fn()} />,
    );
    fireEvent.pointerDown(screen.getByText("数字人员工"));
    expect(await screen.findByRole("menuitem", { name: "剪辑高手" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "休假员工" })).not.toBeInTheDocument();
  });

  it("shows the talking-with chip once an employee is selected", () => {
    const onChange = vi.fn();
    render(
      <EmployeePicker employees={[CLIP_MASTER]} selected={CLIP_MASTER} onChange={onChange} />,
    );
    expect(screen.getByText("正在与 🎬 剪辑高手 对话")).toBeInTheDocument();
    expect(screen.queryByText("数字人员工")).not.toBeInTheDocument();
  });

  it("clears the employee back to the main agent", () => {
    const onChange = vi.fn();
    render(
      <EmployeePicker employees={[CLIP_MASTER]} selected={CLIP_MASTER} onChange={onChange} />,
    );
    fireEvent.click(screen.getByText("正在与 🎬 剪辑高手 对话"));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("readonly 模式：绑定员工时显示只读徽标，不可清除", () => {
    const onChange = vi.fn();
    render(
      <EmployeePicker mode="readonly" employees={[CLIP_MASTER]} selected={CLIP_MASTER} onChange={onChange} />,
    );
    expect(screen.getByText("正在与 🎬 剪辑高手 对话")).toBeInTheDocument();
    // 只读徽标不是按钮：点击无 onChange，也没有下拉入口
    expect(screen.queryByRole("button", { name: "数字人员工" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("正在与 🎬 剪辑高手 对话"));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("readonly 模式：提供 onOpenEmployee 时徽标可点击跳转专属页，且不触发 onChange", () => {
    const onChange = vi.fn();
    const onOpenEmployee = vi.fn();
    render(
      <EmployeePicker
        mode="readonly"
        employees={[CLIP_MASTER]}
        selected={CLIP_MASTER}
        onChange={onChange}
        onOpenEmployee={onOpenEmployee}
      />,
    );
    const badge = screen.getByRole("button", { name: "正在与 🎬 剪辑高手 对话" });
    expect(badge).toBeInTheDocument();
    fireEvent.click(badge);
    expect(onOpenEmployee).toHaveBeenCalledWith(CLIP_MASTER);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("readonly 模式：未绑定员工时不显示任何标记（默认主智能体）", () => {
    render(
      <EmployeePicker mode="readonly" employees={[CLIP_MASTER]} selected={null} onChange={vi.fn()} />,
    );
    expect(screen.queryByText("数字人员工")).not.toBeInTheDocument();
    expect(screen.queryByText(/正在与/)).not.toBeInTheDocument();
  });
});
