"""自定义供应商 Backend 工厂（按 endpoint 派发）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lib.custom_provider.backends import (
    CustomAudioBackend,
    CustomImageBackend,
    CustomTextBackend,
    CustomVideoBackend,
)
from lib.custom_provider.capabilities import synthesize_video_capabilities_with_overrides
from lib.custom_provider.endpoints import get_endpoint_spec

if TYPE_CHECKING:
    from lib.db.models.custom_provider import CustomProvider


def create_custom_backend(
    *,
    provider: CustomProvider,
    model_id: str,
    endpoint: str,
    capability_overrides: object | None = None,
    model_settings: object | None = None,
) -> CustomTextBackend | CustomImageBackend | CustomVideoBackend | CustomAudioBackend:
    """按 endpoint 查 ENDPOINT_REGISTRY 并构造 Backend。

    视频后端额外注入生效能力（endpoint spec 系统判定 ⊕ 模型级用户覆盖），执行层据此门控，
    不再直接读被包装 backend 的声明。

    Args:
        provider: 自定义供应商 ORM 对象（需 base_url / api_key / provider_id 属性）
        model_id: 该次调用使用的具体模型 id
        endpoint: ENDPOINT_REGISTRY 的键
        capability_overrides: 该模型的 `capability_overrides` 原始值；None = 全部跟随系统判定
        model_settings: 该模型的模型级配置（当前仅 comfyui-video 的覆盖工作流 JSON dict）；
            endpoint 不声明 accepts_model_settings 时忽略。None = 无配置。

    Raises:
        ValueError: endpoint 不在 ENDPOINT_REGISTRY 中
    """
    spec = get_endpoint_spec(endpoint)
    if spec.accepts_model_settings:
        # 只有声明 accepts_model_settings 的闭包（当前仅 comfyui-video）接受第三个参数；
        # 其余闭包都是 2 参，标准 Callable 类型表达不了这一异变，故在此按键窄化。
        backend = spec.build_backend(provider, model_id, model_settings)  # type: ignore[call-arg]
    else:
        backend = spec.build_backend(provider, model_id)
    if isinstance(backend, CustomVideoBackend):
        merged, applied = synthesize_video_capabilities_with_overrides(
            endpoint=endpoint, model_id=model_id, overrides=capability_overrides
        )
        # 一并记住 endpoint：它是 backend 构造的真正输入粒度，已提交任务的续跑据此比对协议
        # 是否已被换掉（docs/adr/0054）。
        return backend.with_endpoint(endpoint).with_video_capabilities(merged, overrides=applied)
    return backend
