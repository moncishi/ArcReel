import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "@/i18n";
import type { ComfyUIWorkflowGraph } from "@/types";
import { ComfyUIWorkflowRow } from "./ComfyUIWorkflowRow";

const TEMPLATES: Record<string, ComfyUIWorkflowGraph> = {
  "minimax-h3-ref2va": {
    "92": { class_type: "SaveVideo", inputs: { filename_prefix: "video/MiniMax_H3/arcreel" } },
  },
  "minimax-h3-ref2va-8step": {
    "92": { class_type: "SaveVideo", inputs: { filename_prefix: "video/MiniMax_H3/arcreel_8step" } },
  },
};

describe("ComfyUIWorkflowRow", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders nothing when no builtin templates are available", () => {
    const { container } = render(
      <ComfyUIWorkflowRow value={null} builtinTemplates={{}} isBuiltinModel={false} onChange={() => {}} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("builtin model: shows follow-default label, no selector, no JSON editing", () => {
    const onChange = vi.fn();
    render(
      <ComfyUIWorkflowRow
        value={null}
        builtinTemplates={TEMPLATES}
        isBuiltinModel={true}
        onChange={onChange}
      />,
    );

    // 静态说明「跟随内置模板」，不提供选择器：内置模型与内置模板一一对应，
    // 写入侧按内置模型重名拒绝覆盖，前端不提供会必然 422 的入口。
    expect(screen.getByText(/follow|theo|跟随/i)).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /json/i })).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("custom model: selecting a template starts the override workflow, JSON editing available", async () => {
    const onChange = vi.fn();
    render(
      <ComfyUIWorkflowRow
        value={null}
        builtinTemplates={TEMPLATES}
        isBuiltinModel={false}
        onChange={onChange}
      />,
    );

    const select = screen.getByRole("combobox", { name: /mẫu|template|模板/i });
    await userEvent.selectOptions(select, "minimax-h3-ref2va-8step");
    expect(onChange).toHaveBeenCalledWith(TEMPLATES["minimax-h3-ref2va-8step"]);
  });

  it("custom model: editing JSON propagates parsed workflow and surfaces invalid JSON", async () => {
    const onChange = vi.fn();
    render(
      <ComfyUIWorkflowRow value={null} builtinTemplates={TEMPLATES} isBuiltinModel={false} onChange={onChange} />,
    );

    await userEvent.click(screen.getByRole("button", { name: /json/i }));
    const textarea = screen.getByRole("textbox", { name: /json/i });

    // 非法 JSON → 提示错误，不触发 onChange
    fireEvent.change(textarea, { target: { value: "{not valid" } });
    expect(screen.getByText(/JSON 解析失败|Invalid JSON|JSON không hợp lệ/i)).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();

    // 合法 JSON 图 → onChange 传出解析后的对象
    const graph: ComfyUIWorkflowGraph = {
      "92": { class_type: "SaveVideo", inputs: { filename_prefix: "x" } },
    };
    fireEvent.change(textarea, { target: { value: JSON.stringify(graph) } });
    expect(onChange).toHaveBeenCalledWith(graph);
  });

  it("custom model without workflow shows the required help text", () => {
    render(
      <ComfyUIWorkflowRow value={null} builtinTemplates={TEMPLATES} isBuiltinModel={false} onChange={() => {}} />,
    );
    expect(screen.getByText(/custom|tùy chỉnh|自定义/i)).toBeInTheDocument();
  });
});
