import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "@/i18n";
import { API } from "@/api";
import { useAppStore } from "@/stores/app-store";
import { useEndpointCatalogStore } from "@/stores/endpoint-catalog-store";
import { ComfyUIPoolSection } from "./ComfyUIPoolSection";
import type { ComfyUIWorkflowGraph, CustomProviderInfo } from "@/types";

const TEMPLATES: Record<string, ComfyUIWorkflowGraph> = {
  "minimax-h3-ref2va": {
    "92": { class_type: "SaveVideo", inputs: { filename_prefix: "video/MiniMax_H3/arcreel" } },
  },
};

function comfyuiProvider(overrides: Partial<CustomProviderInfo> = {}): CustomProviderInfo {
  return {
    id: 1,
    display_name: "GPU-1",
    discovery_format: "openai",
    base_url: "http://gpu-1:8188",
    api_key_masked: "sk-***",
    models: [
      {
        id: 11,
        model_id: "minimax-h3-ref2va",
        display_name: "Standard Ref2VA",
        endpoint: "comfyui-video",
        is_default: true,
        is_enabled: true,
        price_unit: null,
        price_input: null,
        price_output: null,
        currency: null,
        supported_durations: null,
        resolution: null,
        system_capabilities: null,
        capability_overrides: null,
        comfyui_workflow: null,
        global_bucket_refs: null,
      },
    ],
    created_at: "2026-01-01T00:00:00Z",
    image_max_workers: null,
    video_max_workers: null,
    audio_max_workers: null,
    ...overrides,
  };
}

const REACHABLE_TEST = {
  success: true,
  message: "ComfyUI 连接成功，1 台主机可达",
  nodes: [
    {
      base_url: "http://gpu-1:8188",
      reachable: true,
      device: "NVIDIA RTX 4090",
      version: "0.3.50",
      running: 1,
      pending: 1,
      error: null,
    },
  ],
};

const UNREACHABLE_TEST = {
  success: false,
  message: "ComfyUI 全部主机不可达",
  nodes: [
    {
      base_url: "http://gpu-1:8188",
      reachable: false,
      device: null,
      version: null,
      running: null,
      pending: null,
      error: "Connection refused",
    },
  ],
};

function setEndpointCatalog() {
  useEndpointCatalogStore.setState({
    endpointToComfyuiTemplates: { "comfyui-video": TEMPLATES },
    initialized: true,
    loading: false,
  });
}

describe("ComfyUIPoolSection", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    useAppStore.setState(useAppStore.getInitialState(), true);
    useEndpointCatalogStore.setState(useEndpointCatalogStore.getInitialState(), true);
    setEndpointCatalog();
  });

  it("renders empty hint when no ComfyUI providers exist", async () => {
    vi.spyOn(API, "listCustomProviders").mockResolvedValue({ providers: [] });
    render(<ComfyUIPoolSection />);
    expect(await screen.findByText(/尚未配置 ComfyUI 主机|No ComfyUI hosts|Chưa cấu hình máy chủ ComfyUI/i)).toBeInTheDocument();
  });

  it("lists each ComfyUI host with reachable status and load after batch test", async () => {
    vi.spyOn(API, "listCustomProviders").mockResolvedValue({ providers: [comfyuiProvider()] });
    vi.spyOn(API, "testComfyUIConnection").mockResolvedValue(REACHABLE_TEST as never);
    render(<ComfyUIPoolSection />);

    expect(await screen.findByText("GPU-1")).toBeInTheDocument();
    // 可达徽章 + 测试结果消息（消息含「可达」二字，故用 distinct 消息断言）
    expect((await screen.findAllByText(/可达|Reachable|Truy cập được/i)).length).toBeGreaterThan(0);
    expect(await screen.findByText(/ComfyUI 连接成功，1 台主机可达/i)).toBeInTheDocument();
    // load badge: running 1 + pending 1 = 2
    expect(await screen.findByText(/负载 2|Load 2|Tải 2/i)).toBeInTheDocument();
    // device/version from /system_stats
    expect(screen.getByText("NVIDIA RTX 4090")).toBeInTheDocument();
    expect(await screen.findByText(/v0\.3\.50/)).toBeInTheDocument();
  });

  it("shows unreachable status when test fails", async () => {
    vi.spyOn(API, "listCustomProviders").mockResolvedValue({ providers: [comfyuiProvider()] });
    vi.spyOn(API, "testComfyUIConnection").mockResolvedValue(UNREACHABLE_TEST as never);
    render(<ComfyUIPoolSection />);

    expect((await screen.findAllByText(/不可达|Unreachable|Không truy cập được/i)).length).toBeGreaterThan(0);
    expect(screen.getByText(/ComfyUI 全部主机不可达/i)).toBeInTheDocument();
  });

  it("per-host test button triggers connectivity test and shows result", async () => {
    vi.spyOn(API, "listCustomProviders").mockResolvedValue({ providers: [comfyuiProvider()] });
    const testSpy = vi.spyOn(API, "testComfyUIConnection").mockResolvedValue(REACHABLE_TEST as never);
    render(<ComfyUIPoolSection />);

    // 等初始批量探测结束后，逐主机按钮仍可用
    const button = await screen.findByRole("button", { name: /测试连接|Test connection|Kiểm tra kết nối/i });
    await userEvent.click(button);
    await waitFor(() => expect(testSpy).toHaveBeenCalledWith(1));
  });

  it("shows workflow editing for custom comfyui models", async () => {
    const custom = comfyuiProvider({
      models: [
        {
          id: 12,
          model_id: "my-local-h3",
          display_name: "My Local H3",
          endpoint: "comfyui-video",
          is_default: true,
          is_enabled: true,
          price_unit: null,
          price_input: null,
          price_output: null,
          currency: null,
          supported_durations: null,
          resolution: null,
          system_capabilities: null,
          capability_overrides: null,
          comfyui_workflow: null,
          global_bucket_refs: null,
        },
      ],
    });
    vi.spyOn(API, "listCustomProviders").mockResolvedValue({ providers: [custom] });
    vi.spyOn(API, "testComfyUIConnection").mockResolvedValue(REACHABLE_TEST as never);
    render(<ComfyUIPoolSection />);

    // 自定义模型：显示工作流模板选择器（ComfyUIWorkflowRow）
    expect(await screen.findByRole("combobox", { name: /内置模板|Built-in template|Mẫu tích hợp/i })).toBeInTheDocument();
  });
});
