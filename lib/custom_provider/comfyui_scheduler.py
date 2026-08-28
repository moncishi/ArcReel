"""ComfyUI 多节点池调度器。

职责：跨 ComfyUI 主机做健康检查、租约分配、并发上限（每节点 2 任务：
1 执行 + 1 等待）、提交间隔控制与租约过期释放。移植自已实战验证的
omnishift 参考实现（comfyui-scheduler.ts）。

本模块是纯调度逻辑层，不感知 ArcReel 的任务队列或 worker——worker 集成
由独立 issue 负责。调度器暴露三个入口：

- ``acquire_comfyui_node``：申请一个可用节点租约（无可用节点时异步等待）。
- ``cancel_comfyui_scheduling``：按 task_id 取消一次等待中的申请。
- ``inspect_comfyui_nodes``：探测一组节点当前队列状态（健康检查）。

并发语义：每个节点最多 ``COMFYUI_MAX_TASKS_PER_NODE=2`` 个在途任务
（队列中 running + pending 之和），且同一节点同时只允许一个租约持有者
（租约用于上传 + 提交的原子窗口）。租约持有超过 ``COMFYUI_MAX_RESERVATION_MS``
视为异常占用，自动过期释放，防止进程内残留锁死整个节点池。

时钟与等待均为显式 seam（``clock: AsyncClock``），生产用 ``SystemClock``，
测试注入假时钟，不依赖真实时间。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from lib.retry import AsyncClock, SystemClock

logger = logging.getLogger(__name__)

# 每个节点最多同时 2 个在途任务（1 执行 + 1 等待）。
COMFYUI_MAX_TASKS_PER_NODE = 2
# 同一节点两次提交的最小间隔。
COMFYUI_MIN_SUBMISSION_INTERVAL_MS = 8_000
# 健康检查超时。
COMFYUI_HEALTH_TIMEOUT_MS = 15_000
# 节点租约最长持有时间；超过视为异常占用并自动释放。
COMFYUI_MAX_RESERVATION_MS = 30 * 60_000
# 调度重试基准间隔。
_SCHEDULER_RETRY_MS = 5_000

_URL_SEPARATORS = re.compile(r"\r?\n|[,;]")


class ComfyUISchedulingCancelledError(Exception):
    """调度等待被取消。"""

    def __init__(self) -> None:
        super().__init__("ComfyUI 调度等待已取消")


@dataclass(frozen=True)
class ComfyUINodeState:
    """一次健康检查得到的节点状态。"""

    base_url: str
    reachable: bool
    running: int = 0
    pending: int = 0
    error: str | None = None

    @property
    def load(self) -> int:
        """在途任务数（running + pending），用于容量判断。"""
        return self.running + self.pending


def parse_comfyui_base_urls(value: str) -> list[str]:
    """解析节点地址列表：按换行/逗号/分号分隔，去重，校验 http/https。

    Raises:
        ValueError: 地址无效或协议不是 http/https。
    """
    unique: list[str] = []
    seen: set[str] = set()
    for raw in _URL_SEPARATORS.split(str(value or "")):
        candidate = raw.strip().rstrip("/")
        if not candidate:
            continue
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            raise ValueError(f"无效的 ComfyUI 节点地址: {candidate}") from None
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"无效的 ComfyUI 节点地址: {candidate}")
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"ComfyUI 节点只支持 HTTP/HTTPS: {candidate}")
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _pool_key(base_urls: str) -> str:
    return "\n".join(sorted(parse_comfyui_base_urls(base_urls)))


async def inspect_comfyui_node(
    base_url: str,
    api_key: str = "",
    *,
    timeout_ms: int = COMFYUI_HEALTH_TIMEOUT_MS,
) -> ComfyUINodeState:
    """探测单个节点：GET /queue 读取 running/pending，不可达标记错误。"""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout_ms / 1000.0) as client:
            response = await client.get(f"{base_url}/queue", headers=headers)
            response.raise_for_status()
            payload = response.json()
        running = payload.get("queue_running")
        pending = payload.get("queue_pending")
        return ComfyUINodeState(
            base_url=base_url,
            reachable=True,
            running=len(running) if isinstance(running, list) else 0,
            pending=len(pending) if isinstance(pending, list) else 0,
        )
    except Exception as exc:  # noqa: BLE001 - 探测失败即视为不可达
        return ComfyUINodeState(
            base_url=base_url,
            reachable=False,
            error=str(exc) or "连接失败",
        )


async def inspect_comfyui_nodes(
    base_urls: str,
    api_key: str = "",
    *,
    timeout_ms: int = COMFYUI_HEALTH_TIMEOUT_MS,
) -> list[ComfyUINodeState]:
    """并发探测一组节点。"""
    endpoints = parse_comfyui_base_urls(base_urls)
    if not endpoints:
        raise ValueError("至少配置一个 ComfyUI 节点地址")
    results = await asyncio.gather(*(inspect_comfyui_node(url, api_key, timeout_ms=timeout_ms) for url in endpoints))
    return list(results)


@dataclass
class ComfyUINodeLease:
    """节点租约：持有期间该节点对调度器不可再分配。"""

    base_url: str
    state: ComfyUINodeState
    _scheduler: ComfyUIPoolScheduler = field(repr=False)
    _released: bool = field(default=False, repr=False)

    def release(self) -> None:
        """释放租约；幂等。"""
        if self._released:
            return
        self._released = True
        self._scheduler._release_reservation(self.base_url)


@dataclass
class _Waiter:
    base_urls: str
    pool_key: str
    api_key: str
    task_id: int | None
    is_cancelled: Callable[[], Awaitable[bool]] | None
    future: asyncio.Future[ComfyUINodeLease]


class ComfyUIPoolScheduler:
    """ComfyUI 多节点池调度器实例。

    每个实例持有独立的租约/等待队列状态；生产用模块级默认实例，
    测试可创建独立实例并注入假时钟。
    """

    def __init__(self, *, clock: AsyncClock | None = None) -> None:
        self._clock: AsyncClock = clock if clock is not None else SystemClock()
        self._reservations: dict[str, int] = {}
        self._reservation_leased_at: dict[str, float] = {}
        self._last_admission_at: dict[str, float] = {}
        self._waiters: list[_Waiter] = []
        self._dispatching = False
        self._retry_task: asyncio.Task[None] | None = None
        self._dispatch_queued = False

    # ── 对外 API ─────────────────────────────────────────────

    async def acquire_comfyui_node(
        self,
        base_urls: str,
        api_key: str = "",
        *,
        task_id: int | None = None,
        is_cancelled: Callable[[], Awaitable[bool]] | None = None,
    ) -> ComfyUINodeLease:
        """申请一个可用节点租约；无可用节点时异步等待。

        ``base_urls`` 是节点地址列表（换行/逗号/分号分隔）。参数校验
        同步进行（地址非法立即抛错），不进入等待。
        """
        # 同步校验，避免无效配置永久等待。
        _pool_key(base_urls)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ComfyUINodeLease] = loop.create_future()
        waiter = _Waiter(
            base_urls=base_urls,
            pool_key=_pool_key(base_urls),
            api_key=api_key,
            task_id=task_id,
            is_cancelled=is_cancelled,
            future=future,
        )
        self._waiters.append(waiter)
        self._schedule_dispatch()
        return await future

    def cancel_comfyui_scheduling(self, task_id: int) -> bool:
        """按 task_id 取消一次等待中的申请。"""
        for index, waiter in enumerate(self._waiters):
            if waiter.task_id == task_id:
                self._waiters.pop(index)
                waiter.future.set_exception(ComfyUISchedulingCancelledError())
                self._schedule_dispatch()
                return True
        return False

    def reset(self) -> None:
        """清空全部状态并拒绝所有等待者（测试与清理用）。"""
        self._cancel_retry_task()
        self._dispatch_queued = False
        self._dispatching = False
        self._reservations.clear()
        self._reservation_leased_at.clear()
        self._last_admission_at.clear()
        while self._waiters:
            waiter = self._waiters.pop(0)
            if not waiter.future.done():
                waiter.future.set_exception(ComfyUISchedulingCancelledError())

    # ── 租约管理 ─────────────────────────────────────────────

    def _release_reservation(self, base_url: str) -> None:
        next_count = max(0, self._reservations.get(base_url, 1) - 1)
        if next_count:
            self._reservations[base_url] = next_count
        else:
            self._reservations.pop(base_url, None)
            self._reservation_leased_at.pop(base_url, None)
        self._schedule_dispatch()

    def _create_lease(self, selected: ComfyUINodeState) -> ComfyUINodeLease:
        now_ms = self._clock.monotonic() * 1000.0
        self._reservations[selected.base_url] = self._reservations.get(selected.base_url, 0) + 1
        self._reservation_leased_at[selected.base_url] = now_ms
        self._last_admission_at[selected.base_url] = now_ms
        return ComfyUINodeLease(
            base_url=selected.base_url,
            state=selected,
            _scheduler=self,
        )

    def _expire_stale_reservations(self) -> None:
        now_ms = self._clock.monotonic() * 1000.0
        for base_url, count in list(self._reservations.items()):
            leased_at = self._reservation_leased_at.get(base_url)
            if leased_at is None:
                continue
            if now_ms - leased_at < COMFYUI_MAX_RESERVATION_MS:
                continue
            self._reservations.pop(base_url, None)
            self._reservation_leased_at.pop(base_url, None)
            logger.warning(
                "[ComfyUIPoolScheduler] expire stale reservation: %s held %.0fs (count=%d)",
                base_url,
                (now_ms - leased_at) / 1000.0,
                count,
            )

    # ── 节点选择 ─────────────────────────────────────────────

    def _select_node(
        self,
        states: list[ComfyUINodeState],
    ) -> tuple[ComfyUINodeState | None, int]:
        now_ms = self._clock.monotonic() * 1000.0
        candidates = [
            state
            for state in states
            if state.reachable
            and self._reservations.get(state.base_url, 0) == 0
            and state.load + self._reservations.get(state.base_url, 0) < COMFYUI_MAX_TASKS_PER_NODE
        ]

        retry_after_ms = _SCHEDULER_RETRY_MS
        ready: list[ComfyUINodeState] = []
        for state in candidates:
            last_ms = self._last_admission_at.get(state.base_url)
            if last_ms is None:
                # 从未提交过：不受间隔约束（首次提交无需等待）。
                ready.append(state)
                continue
            remaining = last_ms + COMFYUI_MIN_SUBMISSION_INTERVAL_MS - now_ms
            if remaining > 0:
                retry_after_ms = min(retry_after_ms, int(remaining))
            else:
                ready.append(state)

        def _rank(state: ComfyUINodeState) -> tuple[int, int, float, int]:
            return (
                0 if state.load == 0 else 1,
                state.load,
                self._last_admission_at.get(state.base_url, 0.0),
                states.index(state),
            )

        ready.sort(key=_rank)
        return (ready[0] if ready else None), max(1, retry_after_ms)

    # ── 调度循环 ─────────────────────────────────────────────

    def _schedule_dispatch(self, delay_ms: int = 0) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 无运行中事件循环（如清理期），调度无法进行，直接返回。
            return
        if delay_ms <= 0:
            self._cancel_retry_task()
            if self._dispatch_queued:
                return
            self._dispatch_queued = True
            asyncio.create_task(self._dispatch_microtask())
            return
        if self._retry_task is not None:
            return
        self._retry_task = asyncio.create_task(self._dispatch_after_delay(delay_ms))

    def _cancel_retry_task(self) -> None:
        if self._retry_task is not None:
            self._retry_task.cancel()
            self._retry_task = None

    async def _dispatch_microtask(self) -> None:
        await asyncio.sleep(0)
        self._dispatch_queued = False
        await self.dispatch_waiters()

    async def _dispatch_after_delay(self, delay_ms: int) -> None:
        try:
            await self._clock.sleep(delay_ms / 1000.0)
        except asyncio.CancelledError:
            return
        self._retry_task = None
        self._dispatch_queued = False
        await self.dispatch_waiters()

    def _index_of(self, waiter: _Waiter) -> int:
        for index, existing in enumerate(self._waiters):
            if existing is waiter:
                return index
        return -1

    async def dispatch_waiters(self) -> None:
        """处理等待队列：按池健康检查、选择节点、发放租约。"""
        if self._dispatching or not self._waiters:
            return
        self._dispatching = True
        retry_after_ms = _SCHEDULER_RETRY_MS
        try:
            self._expire_stale_reservations()
            while self._waiters:
                queue_changed = False
                visited_pools: set[str] = set()

                for waiter in list(self._waiters):
                    if waiter not in self._waiters:
                        continue
                    if waiter.pool_key in visited_pools:
                        continue
                    visited_pools.add(waiter.pool_key)

                    cancelled = False
                    if waiter.is_cancelled is not None:
                        cancelled = await waiter.is_cancelled()
                    current_index = self._index_of(waiter)
                    if current_index < 0:
                        queue_changed = True
                        break
                    if cancelled:
                        self._waiters.pop(current_index)
                        waiter.future.set_exception(ComfyUISchedulingCancelledError())
                        queue_changed = True
                        break

                    try:
                        states = await inspect_comfyui_nodes(
                            waiter.base_urls,
                            waiter.api_key,
                        )
                    except ValueError as exc:
                        failed_index = self._index_of(waiter)
                        if failed_index < 0:
                            queue_changed = True
                            break
                        self._waiters.pop(failed_index)
                        waiter.future.set_exception(exc)
                        queue_changed = True
                        break

                    ready_index = self._index_of(waiter)
                    if ready_index < 0:
                        # 健康检查期间被取消，等待者已不在队列。
                        queue_changed = True
                        break

                    selected, selection_retry = self._select_node(states)
                    retry_after_ms = min(retry_after_ms, selection_retry)
                    if selected is None:
                        continue

                    self._waiters.pop(ready_index)
                    waiter.future.set_result(self._create_lease(selected))
                    queue_changed = True
                    break

                if not queue_changed:
                    break
        finally:
            self._dispatching = False
            if self._waiters:
                self._schedule_dispatch(retry_after_ms)


# 模块级默认实例：生产消费方直接用模块级函数。
_default_scheduler = ComfyUIPoolScheduler()


async def acquire_comfyui_node(
    base_urls: str,
    api_key: str = "",
    *,
    task_id: int | None = None,
    is_cancelled: Callable[[], Awaitable[bool]] | None = None,
) -> ComfyUINodeLease:
    """模块级入口：申请节点租约（见 ComfyUIPoolScheduler.acquire_comfyui_node）。"""
    return await _default_scheduler.acquire_comfyui_node(
        base_urls,
        api_key,
        task_id=task_id,
        is_cancelled=is_cancelled,
    )


def cancel_comfyui_scheduling(task_id: int) -> bool:
    """模块级入口：取消等待中的申请。"""
    return _default_scheduler.cancel_comfyui_scheduling(task_id)


def reset_comfyui_scheduler_for_tests() -> None:
    """模块级入口：清空默认实例状态（测试用）。"""
    _default_scheduler.reset()
