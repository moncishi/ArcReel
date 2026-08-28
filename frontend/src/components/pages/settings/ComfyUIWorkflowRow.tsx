import { useMemo, useState } from "react";
import { ChevronDown, Braces, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { ComfyUIWorkflowGraph } from "@/types";

// ---------------------------------------------------------------------------
// ComfyUIWorkflowRow —— 视频模型行内的 ComfyUI 工作流配置（仅 comfyui-video endpoint）
//
// 内置模型（minimax-h3-*）与内置模板一一对应（BUILTIN_COMFYUI_MODELS 即模板键集）：
// 后端按 model_id 查 _BUILTIN_MODEL_BUILDERS 构造，无需也不接受覆盖工作流，行内只展示
// 「跟随内置模板」的静态说明，不提供选择器（选了也写不进 comfyui_workflow——写入侧会按
// 内置模型重名 422）。
//
// 自定义模型没有内置模板，必须提供覆盖工作流（API-format 图 JSON），后端把它装进
// configured_workflows，构造期校验（含 MiniMaxH3ReferenceToVideo + SaveVideo 节点）失败
// 即 422。「按内置模板起步」把选中模板的图快照填入 JSON 编辑区作为起点，用户可在此基础上
// 改（不改也会被后端接受——模板图本身就是合法覆盖图）。当前工作流等于某内置模板快照时下拉
// 回显该模板，便于「我又改回模板原样」的状态识别。
// ---------------------------------------------------------------------------

export interface ComfyUIWorkflowRowProps {
  /** 当前覆盖工作流图；null = 未配置覆盖（内置模型跟随内置模板）。 */
  value: ComfyUIWorkflowGraph | null;
  /** 内置模板图快照（template_key → API-format workflow），来自 endpoint catalog。 */
  builtinTemplates: Record<string, ComfyUIWorkflowGraph>;
  /** 当前模型是否为内置模型（minimax-h3-*）：内置模型无需覆盖工作流。 */
  isBuiltinModel: boolean;
  onChange: (next: ComfyUIWorkflowGraph | null) => void;
}

export function ComfyUIWorkflowRow({
  value,
  builtinTemplates,
  isBuiltinModel,
  onChange,
}: ComfyUIWorkflowRowProps) {
  const { t } = useTranslation("dashboard");
  const [expanded, setExpanded] = useState(false);

  const templateKeys = Object.keys(builtinTemplates);

  // 当前工作流等于某内置模板快照时，回显该模板键；否则（自定义/无）为 undefined。
  const activeTemplate = useMemo(() => {
    if (!value) return undefined;
    return templateKeys.find((key) => JSON.stringify(builtinTemplates[key]) === JSON.stringify(value));
  }, [value, builtinTemplates, templateKeys]);

  const selectTemplate = (key: string) => {
    // 「按模板起步」：把模板图快照整体作为覆盖工作流（结构快照，请求期参数化）。
    onChange(builtinTemplates[key]);
  };

  // JSON 编辑区：诚实解析，解析失败只提示、不改写已存值（保持「写什么存什么」）。
  const [jsonError, setJsonError] = useState<string | null>(null);

  const handleJsonChange = (raw: string) => {
    const trimmed = raw.trim();
    if (!trimmed) {
      onChange(null);
      setJsonError(null);
      return;
    }
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (!isGraphObject(parsed)) {
        setJsonError(t("comfyui_workflow_json_not_graph"));
        return;
      }
      onChange(parsed);
      setJsonError(null);
    } catch {
      setJsonError(t("comfyui_workflow_json_invalid"));
    }
  };

  const resetToTemplate = () => {
    if (activeTemplate) selectTemplate(activeTemplate);
  };

  if (templateKeys.length === 0) return null;

  const jsonText = value ? JSON.stringify(value, null, 2) : "";

  return (
    <div className="mt-2 flex flex-col gap-1 pl-6">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[10px] font-bold uppercase tracking-[0.14em] text-text-3 whitespace-nowrap">
          {t("comfyui_workflow_label")}
        </span>

        {isBuiltinModel ? (
          // 内置模型跟随同名内置模板，无覆盖配置可选（写入侧拒绝内置模型重名覆盖）。
          <span className="rounded-[6px] border border-hairline bg-bg-grad-a/55 px-2 py-1 text-[11.5px] text-text-2">
            {t("comfyui_workflow_follow_default")}
          </span>
        ) : (
          <>
            <div className="relative">
              <select
                value={activeTemplate ?? ""}
                onChange={(e) => {
                  if (e.target.value) selectTemplate(e.target.value);
                }}
                aria-label={t("comfyui_workflow_template_label")}
                className="rounded-[6px] border border-hairline bg-bg-grad-a/55 px-2 py-1 text-[11.5px] text-text-2 hover:border-hairline-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                <option value="">{t("comfyui_workflow_no_template")}</option>
                {templateKeys.map((key) => (
                  <option key={key} value={key}>
                    {t(`comfyui_workflow_template_${key.replace(/-/g, "_")}`)}
                  </option>
                ))}
              </select>
              <ChevronDown
                aria-hidden="true"
                className="pointer-events-none absolute right-1.5 top-1/2 h-3 w-3 -translate-y-1/2 text-text-4"
              />
            </div>

            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
              className="flex items-center gap-1 rounded px-1.5 py-1 font-mono text-[10px] font-bold uppercase tracking-[0.08em] text-text-3 transition-colors hover:text-accent-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              <Braces className="h-3 w-3" />
              {expanded ? t("comfyui_workflow_hide_json") : t("comfyui_workflow_edit_json")}
            </button>

            {activeTemplate && (
              <button
                type="button"
                onClick={resetToTemplate}
                title={t("comfyui_workflow_reset_hint")}
                className="rounded p-1 text-text-4 transition-colors hover:text-accent-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                <RotateCcw className="h-3 w-3" />
              </button>
            )}
          </>
        )}
      </div>

      {!isBuiltinModel && (
        <p className="text-[11px] text-text-4">{t("comfyui_workflow_help_custom")}</p>
      )}

      {expanded && !isBuiltinModel && (
        <textarea
          value={jsonText}
          onChange={(e) => handleJsonChange(e.target.value)}
          spellCheck={false}
          aria-label={t("comfyui_workflow_json_label")}
          placeholder={t("comfyui_workflow_json_placeholder")}
          className="min-h-[140px] w-full resize-y rounded-[6px] border border-hairline bg-bg-grad-a/55 px-2.5 py-2 font-mono text-[11px] leading-relaxed text-text placeholder:text-text-4 transition-colors hover:border-hairline-strong focus:border-accent/55 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        />
      )}

      {jsonError && <p className="text-[11px] text-warm-bright">{jsonError}</p>}
    </div>
  );
}

function isGraphObject(value: unknown): value is ComfyUIWorkflowGraph {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  return Object.values(value).every((node): boolean => {
    if (typeof node !== "object" || node === null || Array.isArray(node)) return false;
    const record: Record<string, unknown> = node as Record<string, unknown>;
    return (
      typeof record.class_type === "string" &&
      typeof record.inputs === "object" &&
      record.inputs !== null &&
      !Array.isArray(record.inputs)
    );
  });
}
