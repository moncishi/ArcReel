"""ComfyUI 池发现与聚合。

一个 ComfyUI 池由多个「comfyui-video endpoint 的启用自定义供应商」组成，每台主机一个
自定义供应商（``custom-N``，各有独立 base_url 与 video_max_workers）。本模块负责：

- 发现池成员（``discover_comfyui_pool``）：列出全部启用的 comfyui-video 供应商。
- 聚合池并发上限（``pool_capacity``）：各主机 video_max_workers 之和——ArcReel 的
  claim 层按池总量限流，池满任务保持 queued；实际「每主机 ≤2」由池调度器（issue #5
  的 ``acquire_comfyui_node``）在执行期强制。
- 池身份（``COMFYUI_POOL_PROVIDER_ID``）：claim 投影与容量表使用该虚拟 provider_id，
  与任何 ``custom-N`` 不冲突。

worker claim 层的池容量语义：池的并发能力是各主机容量之和（N 台主机各 2 → 池容量
2N），任务被 claim 后由执行入口经调度器分到具体主机；「单主机 ≤2」由调度器租约保证，
两层叠加得到「池内任意主机不超载、池总并发 = 各主机之和」。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lib.custom_provider import make_provider_id
from lib.custom_provider.comfyui_scheduler import ComfyUINodeLease, acquire_comfyui_node

if TYPE_CHECKING:
    from lib.db.models.custom_provider import CustomProviderModel

logger = logging.getLogger(__name__)

#: 池在 claim 投影与容量表中的虚拟 provider_id。不与真实 ``custom-N`` 冲突。
COMFYUI_POOL_PROVIDER_ID = "comfyui-pool"

#: comfyui-video 端点的注册表键（与 lib/custom_provider/endpoints.py 的 EndpointSpec.key 一致）。
COMFYUI_VIDEO_ENDPOINT = "comfyui-video"


@dataclass(frozen=True)
class ComfyUIPoolHost:
    """池中的一个 ComfyUI 主机（一个自定义供应商）。"""

    provider_id: str
    base_url: str
    api_key: str
    video_max_workers: int
    default_model_id: str


async def discover_comfyui_pool() -> list[ComfyUIPoolHost]:
    """列出全部启用的 comfyui-video 自定义供应商（池成员）。

    成员条件：供应商有至少一个启用的 comfyui-video 视频模型、base_url 非空、
    video_max_workers 非零。按 provider_id 升序，保证池列表稳定（调度器按地址列表
    顺序决胜）。
    """
    from lib.db import safe_session_factory
    from lib.db.repositories.custom_provider_repo import CustomProviderRepository

    hosts: list[ComfyUIPoolHost] = []
    async with safe_session_factory() as session:
        repo = CustomProviderRepository(session)
        for provider, models in await repo.list_providers_with_models():
            if not models:
                continue
            video_models = [m for m in models if m.is_enabled and m.endpoint == COMFYUI_VIDEO_ENDPOINT]
            if not video_models:
                continue
            base_url = (provider.base_url or "").strip().rstrip("/")
            if not base_url:
                logger.warning("ComfyUI 供应商 %s base_url 为空，排除出池", provider.provider_id)
                continue
            max_workers = provider.video_max_workers or 0
            if max_workers <= 0:
                logger.warning("ComfyUI 供应商 %s video_max_workers=%s，排除出池", provider.provider_id, max_workers)
                continue
            hosts.append(
                ComfyUIPoolHost(
                    provider_id=make_provider_id(provider.id),
                    base_url=base_url,
                    api_key=provider.api_key or "",
                    video_max_workers=max_workers,
                    default_model_id=_pick_default_model(video_models),
                )
            )
    hosts.sort(key=lambda host: host.provider_id)
    return hosts


def _pick_default_model(video_models: list[CustomProviderModel]) -> str:
    """取启用的 comfyui-video 模型中的默认模型（is_default 优先，否则第一个）。"""
    for model in video_models:
        if getattr(model, "is_default", False):
            return model.model_id
    return video_models[0].model_id


def pool_base_urls(hosts: list[ComfyUIPoolHost]) -> str:
    """池成员地址列表（换行分隔，供调度器 ``acquire_comfyui_node`` 使用）。"""
    return "\n".join(host.base_url for host in hosts)


def pool_api_key(hosts: list[ComfyUIPoolHost]) -> str:
    """池的统一 api_key：全部成员一致时取该值，否则取第一个（调度器按单 key 探测）。

    实际 ComfyUI 多为本地无鉴权部署（api_key 空）；若各主机 key 不同，健康检查按
    第一个 key 探测，执行时仍按各主机自身 key 构造 backend——探测用 key 只影响
    「可达性」判定，不影响提交鉴权。
    """
    if not hosts:
        return ""
    first = hosts[0].api_key
    if any(host.api_key != first for host in hosts):
        logger.warning("ComfyUI 池各主机 api_key 不一致，健康检查统一用第一个")
    return first


def pool_capacity(hosts: list[ComfyUIPoolHost]) -> int:
    """池并发上限 = 各主机 video_max_workers 之和。"""
    return sum(host.video_max_workers for host in hosts)


def find_host_by_base_url(hosts: list[ComfyUIPoolHost], base_url: str) -> ComfyUIPoolHost | None:
    """按 base_url 反查主机（调度器租约返回 base_url 后定位具体供应商）。"""
    normalized = (base_url or "").strip().rstrip("/")
    for host in hosts:
        if host.base_url == normalized:
            return host
    return None


class ComfyUIPoolUnavailableError(RuntimeError):
    """ComfyUI 池无可用成员（未配置、全部不可达或全部满载）。"""


async def select_pool_host(
    *,
    is_cancelled: Callable[[], Awaitable[bool]] | None = None,
    exclude_base_urls: frozenset[str] = frozenset(),
) -> tuple[ComfyUIPoolHost, ComfyUINodeLease]:
    """池选择：发现成员 → 经调度器租约选主机 → 反查主机。

    返回 ``(host, lease)``：调用方在提交完成（无论成败）后必须 ``lease.release()``。
    池无成员或全部不可达时抛 ``ComfyUIPoolUnavailableError``（不进入等待）。
    ``exclude_base_urls`` 用于 failover：提交失败的主机从候选剔除。
    """
    hosts = await discover_comfyui_pool()
    remaining = [host for host in hosts if host.base_url not in exclude_base_urls]
    if not remaining:
        raise ComfyUIPoolUnavailableError("未发现可用的 ComfyUI 池成员")
    lease = await acquire_comfyui_node(
        pool_base_urls(remaining),
        pool_api_key(remaining),
        is_cancelled=is_cancelled,
    )
    host = find_host_by_base_url(remaining, lease.base_url)
    if host is None:
        lease.release()
        raise ComfyUIPoolUnavailableError(f"租约主机 {lease.base_url} 不在池成员中")
    return host, lease


def pin_pool_host_payload(payload: dict, host: ComfyUIPoolHost, capability: str) -> dict:
    """把选中主机 pin 进执行 payload（``video_provider_<cap> = <provider>/<model>``）。

    ``resolve_video_backend`` 的 payload 恒为最高优先级：pin 后重解析会命中选中主机，
    使 checkpoint 冻结的主机身份、backend 构造、续跑绑定三者一致。返回新 dict，不改原 payload。
    """
    pinned = dict(payload or {})
    pinned[f"video_provider_{capability}"] = f"{host.provider_id}/{host.default_model_id}"
    return pinned
