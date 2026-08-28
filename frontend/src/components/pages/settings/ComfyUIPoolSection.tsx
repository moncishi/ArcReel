import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, PlugZap, RefreshCw, Server, Activity, Wifi, WifiOff } from "lucide-react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { errMsg, voidCall } from "@/utils/async";
import { useAppStore } from "@/stores/app-store";
import type {
  ComfyUIConnectionTestResult,
  ComfyUIWorkflowGraph,
  CustomProviderFullUpdateRequest,
  CustomProviderInfo,
  CustomProviderModelInput,
} from "@/types";
import { useEndpointCatalogStore } from "@/stores/endpoint-catalog-store";
import { CARD_STYLE, GHOST_BTN_CLS } from "@/components/ui/darkroom-tokens";
import { ComfyUIWorkflowRow } from "./ComfyUIWorkflowRow";

// ---------------------------------------------------------------------------
// ComfyUIPoolSection —— 设置页的 ComfyUI 多主机管理视图
//
// 列出所有使用 comfyui-video endpoint 的自定义供应商（每台 ComfyUI 主机即一个供应商），
// 展示可达状态与当前负载（若可获取），支持逐主机「测试连通性」（后端代理 GET /system_stats
// 与 /queue），并为每个供应商的 comfyui-video 模型行复用 ComfyUIWorkflowRow 做工作流模板
// 选择与覆盖 JSON 编辑——改动经 PUT 全量更新持久化。
//
// 可达性与负载来自测试结果（POST /custom-providers/{id}/test），不持久化：进入区块时触发
// 一次批量探测，用户也可逐主机手动重测。
// ---------------------------------------------------------------------------

const LOAD_BADGE_STYLES: Record<"idle" | "busy" | "full" | "unknown", { bg: string; color: string; border: string }> = {
  idle: { bg: "oklch(0.30 0.10 155 / 0.18)", color: "var(--color-good)", border: "1px solid oklch(0.45 0.10 155 / 0.40)" },
  busy: { bg: "oklch(0.32 0.08 80 / 0.20)", color: "oklch(0.82 0.10 80)", border: "1px solid oklch(0.55 0.09 80 / 0.45)" },
  full: { bg: "oklch(0.30 0.09 25 / 0.22)", color: "var(--color-warm-bright)", border: "1px solid var(--color-warm-ring)" },
  unknown: { bg: "var(--color-bg-grad-a)", color: "var(--color-text-3)", border: "1px solid var(--color-hairline)" },
};

interface HostRow {
  provider: CustomProviderInfo;
  test: ComfyUIConnectionTestResult | null;
  testing: boolean;
  /** 正在保存某模型行的覆盖工作流（保存中不可重复提交）。 */
  savingModelKey: number | null;
}

type LoadKind = "idle" | "busy" | "full" | "unknown";

function loadKind(load: number | null): LoadKind {
  if (load === null) return "unknown";
  if (load === 0) return "idle";
  if (load < 2) return "busy";
  return "full";
}

/** 模型响应字段 → PUT 输入字段：回显专用字段（system_capabilities / global_bucket_refs 等）剔除。 */
function toModelInput(m: CustomProviderInfo["models"][number]): CustomProviderModelInput {
  return {
    model_id: m.model_id,
    display_name: m.display_name,
    endpoint: m.endpoint,
    is_default: m.is_default,
    is_enabled: m.is_enabled,
    price_unit: m.price_unit ?? undefined,
    price_input: m.price_input ?? undefined,
    price_output: m.price_output ?? undefined,
    currency: m.currency ?? undefined,
    supported_durations: m.supported_durations,
    resolution: m.resolution ?? undefined,
    capability_overrides: m.capability_overrides,
    comfyui_workflow: m.comfyui_workflow ?? null,
  };
}

export function ComfyUIPoolSection() {
  const { t } = useTranslation("dashboard");
  const endpointToComfyuiTemplates = useEndpointCatalogStore((s) => s.endpointToComfyuiTemplates);
  const builtinTemplates = endpointToComfyuiTemplates["comfyui-video"] ?? {};

  const [rows, setRows] = useState<HostRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [batchTesting, setBatchTesting] = useState(false);
  // 每个供应商的工作流保存序号：只应用最新一次请求的响应，避免旧响应晚到覆盖新值。
  const saveSeqRef = useRef<Record<number, number>>({});

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const res = await API.listCustomProviders();
      const providers = res.providers.filter((p) => p.models.some((m) => m.endpoint === "comfyui-video"));
      setRows(providers.map((provider) => ({ provider, test: null, testing: false, savingModelKey: null })));
    } catch (e) {
      setError(errMsg(e));
    }
  }, []);

  useEffect(() => {
    // mount/依赖变更时异步拉取供应商列表，回调内 setRows 等（异步 fetch 后回写）
    // eslint-disable-next-line react-hooks/set-state-in-effect
    voidCall(refresh());
  }, [refresh, reloadKey]);

  const testHost = useCallback(async (providerId: number) => {
    setRows((prev) => prev?.map((r) => (r.provider.id === providerId ? { ...r, testing: true } : r)) ?? null);
    try {
      const test = await API.testComfyUIConnection(providerId);
      setRows((prev) => prev?.map((r) => (r.provider.id === providerId ? { ...r, test, testing: false } : r)) ?? null);
    } catch (e) {
      const test: ComfyUIConnectionTestResult = { success: false, message: errMsg(e), nodes: [] };
      setRows((prev) => prev?.map((r) => (r.provider.id === providerId ? { ...r, test, testing: false } : r)) ?? null);
    }
  }, []);

  const testAll = useCallback(async () => {
    setBatchTesting(true);
    try {
      await Promise.all(rows?.map((r) => testHost(r.provider.id)) ?? []);
    } finally {
      setBatchTesting(false);
    }
  }, [rows, testHost]);

  // 首次就绪（所有行尚未探测、不在探测中）时批量探测一次。
  useEffect(() => {
    if (rows && rows.length > 0 && rows.every((r) => r.test === null && !r.testing) && !batchTesting) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      voidCall(testAll());
    }
  }, [rows, testAll, batchTesting]);

  const saveWorkflow = useCallback(
    async (providerId: number, modelId: string, workflow: ComfyUIWorkflowGraph | null) => {
      const row = rows?.find((r) => r.provider.id === providerId);
      if (!row) return;
      const provider = row.provider;
      const modelIdNum = row.provider.models.find((m) => m.model_id === modelId)?.id ?? null;
      const seq = (saveSeqRef.current[providerId] ?? 0) + 1;
      saveSeqRef.current[providerId] = seq;
      setRows((prev) => prev?.map((r) => (r.provider.id === providerId ? { ...r, savingModelKey: modelIdNum } : r)) ?? null);
      const payload: CustomProviderFullUpdateRequest = {
        display_name: provider.display_name,
        base_url: provider.base_url,
        api_key: undefined,
        models: provider.models.map((m) =>
          m.model_id === modelId ? { ...toModelInput(m), comfyui_workflow: workflow } : toModelInput(m),
        ),
        image_max_workers: provider.image_max_workers,
        video_max_workers: provider.video_max_workers,
        audio_max_workers: provider.audio_max_workers,
      };
      try {
        const updated = await API.fullUpdateCustomProvider(providerId, payload);
        // 旧响应晚到时不覆盖新值（只应用最新一次请求的结果）
        if (saveSeqRef.current[providerId] !== seq) return;
        setRows(
          (prev) =>
            prev?.map((r) => (r.provider.id === providerId ? { ...r, provider: updated, savingModelKey: null } : r)) ??
            null,
        );
        useAppStore.getState().pushToast(t("comfyui_workflow_saved"), "success");
      } catch (e) {
        if (saveSeqRef.current[providerId] !== seq) return;
        setRows((prev) => prev?.map((r) => (r.provider.id === providerId ? { ...r, savingModelKey: null } : r)) ?? null);
        useAppStore.getState().pushToast(t("save_failed", { message: errMsg(e) }), "error");
      }
    },
    [rows, t],
  );

  if (error) {
    return (
      <div role="alert" className="rounded-[10px] border border-hairline p-5" style={CARD_STYLE}>
        <div className="font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-warm">
          {t("comfyui_pool_load_failed")}
        </div>
        <p className="mt-2 text-[12.5px] text-text-2">{error}</p>
        <button type="button" onClick={() => setReloadKey((k) => k + 1)} className={`mt-3 ${GHOST_BTN_CLS}`}>
          <RefreshCw className="h-3.5 w-3.5" />
          {t("common:retry")}
        </button>
      </div>
    );
  }

  if (rows === null) {
    return (
      <div className="flex items-center gap-2 px-1 py-12 text-text-3">
        <Loader2 className="h-3.5 w-3.5 motion-safe:animate-spin text-accent-2" aria-hidden />
        <span className="font-mono text-[11px] uppercase tracking-[0.14em]">{t("common:loading")}</span>
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div className="rounded-[10px] border border-hairline p-5" style={CARD_STYLE}>
        <p className="text-[12.5px] text-text-3">{t("comfyui_pool_empty_hint")}</p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-accent-2">
          <Server className="h-3.5 w-3.5" aria-hidden="true" />
          {t("comfyui_pool_title")}
          <span className="rounded-full border border-hairline-soft bg-bg-grad-a/55 px-2 py-0.5 text-[10px] text-text-3">
            {rows.length}
          </span>
        </div>
        <button
          type="button"
          onClick={() => voidCall(testAll())}
          disabled={batchTesting || rows.some((r) => r.testing)}
          className={GHOST_BTN_CLS}
        >
          {batchTesting ? (
            <Loader2 className="h-3.5 w-3.5 motion-safe:animate-spin" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5" />
          )}
          {t("comfyui_pool_test_all")}
        </button>
      </div>

      {rows.map((row) => {
        const node = row.test?.nodes[0] ?? null;
        const reachable = node?.reachable ?? null;
        const load = node ? (node.running ?? 0) + (node.pending ?? 0) : null;
        const kind = reachable === null ? "unknown" : reachable ? loadKind(load) : "unknown";
        const badge = LOAD_BADGE_STYLES[kind];
        const comfyuiModels = row.provider.models.filter((m) => m.endpoint === "comfyui-video");
        return (
          <div key={row.provider.id} className="rounded-[10px] border border-hairline p-4" style={CARD_STYLE}>
            {/* Header: name + host + reachability */}
            <div className="flex flex-wrap items-center gap-2.5">
              <span className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-[6px] border border-hairline-strong bg-bg-grad-a font-mono text-[10px] font-bold uppercase text-text-2">
                {Array.from(row.provider.display_name)[0] ?? "?"}
              </span>
              <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-text">
                {row.provider.display_name}
              </span>
              <span className="truncate font-mono text-[10.5px] text-text-4">{row.provider.base_url}</span>
              {reachable === null && (
                <span className="inline-flex items-center gap-1 rounded-full border border-hairline-soft bg-bg-grad-a/55 px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-text-4">
                  <WifiOff className="h-3 w-3" aria-hidden="true" />
                  {t("comfyui_host_unknown")}
                </span>
              )}
              {reachable === true && (
                <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-[0.12em]" style={badge}>
                  <Wifi className="h-3 w-3" aria-hidden="true" />
                  {t("comfyui_host_reachable")}
                </span>
              )}
              {reachable === false && (
                <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-[0.12em]" style={LOAD_BADGE_STYLES.full}>
                  <WifiOff className="h-3 w-3" aria-hidden="true" />
                  {t("comfyui_host_unreachable")}
                </span>
              )}
            </div>

            {/* Meta row: device / version / load */}
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11.5px]">
              {node?.device && (
                <span className="inline-flex items-center gap-1 text-text-3">
                  <Server className="h-3 w-3 text-text-4" aria-hidden="true" />
                  {node.device}
                </span>
              )}
              {node?.version && (
                <span className="font-mono text-[10.5px] text-text-4">v{node.version}</span>
              )}
              {reachable === true && (
                <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-[0.12em]" style={badge}>
                  <Activity className="h-3 w-3" aria-hidden="true" />
                  {load === null
                    ? t("comfyui_load_unknown")
                    : t("comfyui_load_label", { count: load })}
                </span>
              )}
            </div>

            {/* Test result message */}
            {row.test && (
              <p
                className={`mt-2 text-[11.5px] ${row.test.success ? "text-good" : "text-warm-bright"}`}
                role="status"
              >
                {row.test.message}
              </p>
            )}

            {/* Per-host test button */}
            <div className="mt-3">
              <button
                type="button"
                onClick={() => voidCall(testHost(row.provider.id))}
                disabled={row.testing}
                className={GHOST_BTN_CLS}
              >
                {row.testing ? (
                  <Loader2 className="h-3.5 w-3.5 motion-safe:animate-spin" />
                ) : (
                  <PlugZap className="h-3.5 w-3.5" />
                )}
                {row.testing ? t("testing_connection") : t("test_connection")}
              </button>
            </div>

            {/* Workflow editing per model (reuses ComfyUIWorkflowRow, persisted via PUT) */}
            {comfyuiModels.length > 0 && (
              <div className="mt-3 border-t border-hairline pt-3">
                <div className="mb-1.5 font-mono text-[9.5px] font-bold uppercase tracking-[0.16em] text-text-4">
                  {t("comfyui_pool_workflows")}
                </div>
                {comfyuiModels.map((m) => {
                  const saving = row.savingModelKey === m.id;
                  return (
                    <div key={m.id}>
                      <div className="flex items-center gap-2">
                        <span className="min-w-0 truncate font-mono text-[11px] text-text-2">{m.model_id}</span>
                        {!m.is_enabled && (
                          <span className="font-mono text-[9.5px] uppercase tracking-[0.12em] text-text-4">
                            {t("model_disabled")}
                          </span>
                        )}
                        {saving && <Loader2 className="h-3 w-3 motion-safe:animate-spin text-accent-2" aria-hidden />}
                      </div>
                      <ComfyUIWorkflowRow
                        value={m.comfyui_workflow ?? null}
                        builtinTemplates={builtinTemplates}
                        isBuiltinModel={Object.keys(builtinTemplates).includes(m.model_id)}
                        onChange={(workflow) => voidCall(saveWorkflow(row.provider.id, m.model_id, workflow))}
                      />
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
