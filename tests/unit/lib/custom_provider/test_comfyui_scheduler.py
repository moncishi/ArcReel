"""ComfyUIPoolScheduler 单元测试。

镜像参考实现（comfyui-scheduler.test.ts）的关键行为：空闲优先、满载跳过、
租约分散、取消、提交间隔、租约过期。HTTP 经 respx 拦截，时间经假时钟注入。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from lib.custom_provider.comfyui_scheduler import (
    COMFYUI_HEALTH_TIMEOUT_MS,
    COMFYUI_MAX_RESERVATION_MS,
    COMFYUI_MIN_SUBMISSION_INTERVAL_MS,
    ComfyUINodeLease,
    ComfyUIPoolScheduler,
    ComfyUISchedulingCancelledError,
    acquire_comfyui_node,
    cancel_comfyui_scheduling,
    parse_comfyui_base_urls,
    reset_comfyui_scheduler_for_tests,
)
from lib.retry import AsyncClock


class FakeClock:
    """可注入的假时钟：monotonic 与 sleep 同步推进。"""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _queue_payload(running: int, pending: int) -> dict:
    return {
        "queue_running": [[i, f"running-{i}"] for i in range(running)],
        "queue_pending": [[i, f"pending-{i}"] for i in range(pending)],
    }


def _route(respx_mock: respx.MockRouter, base_url: str, running: int, pending: int):
    respx_mock.get(f"{base_url}/queue").mock(return_value=httpx.Response(200, json=_queue_payload(running, pending)))


def _new_scheduler(clock: AsyncClock | None = None) -> ComfyUIPoolScheduler:
    return ComfyUIPoolScheduler(clock=clock if clock is not None else FakeClock())


class TestParseComfyUIBaseUrls:
    def test_accepts_newline_comma_semicolon_and_dedupes(self) -> None:
        result = parse_comfyui_base_urls("http://gpu-1:8188/\nhttp://gpu-2:8188; http://gpu-1:8188,")
        assert result == ["http://gpu-1:8188", "http://gpu-2:8188"]

    def test_rejects_invalid_url(self) -> None:
        with pytest.raises(ValueError, match="无效的 ComfyUI 节点地址"):
            parse_comfyui_base_urls("not a url")

    def test_rejects_non_http_protocol(self) -> None:
        with pytest.raises(ValueError, match="只支持 HTTP/HTTPS"):
            parse_comfyui_base_urls("ftp://gpu-1:8188")

    def test_empty_input_yields_empty_list(self) -> None:
        assert parse_comfyui_base_urls("") == []


class TestSelection:
    def test_prefers_idle_reachable_node(self, respx_mock: respx.MockRouter) -> None:
        _route(respx_mock, "http://gpu-1:8188", 1, 0)
        _route(respx_mock, "http://gpu-2:8188", 0, 0)
        _route(respx_mock, "http://gpu-3:8188", 0, 2)

        scheduler = _new_scheduler()
        lease = asyncio.run(scheduler.acquire_comfyui_node("http://gpu-1:8188\nhttp://gpu-2:8188\nhttp://gpu-3:8188"))
        assert lease.base_url == "http://gpu-2:8188"
        lease.release()

    def test_skips_nodes_already_at_capacity(self, respx_mock: respx.MockRouter) -> None:
        _route(respx_mock, "http://gpu-1:8188", 1, 1)
        _route(respx_mock, "http://gpu-2:8188", 1, 0)
        _route(respx_mock, "http://gpu-3:8188", 0, 2)

        scheduler = _new_scheduler()
        lease = asyncio.run(scheduler.acquire_comfyui_node("http://gpu-1:8188\nhttp://gpu-2:8188\nhttp://gpu-3:8188"))
        assert lease.base_url == "http://gpu-2:8188"
        lease.release()

    def test_reservations_spread_concurrent_submissions_across_idle_nodes(self, respx_mock: respx.MockRouter) -> None:
        _route(respx_mock, "http://gpu-1:8188", 0, 0)
        _route(respx_mock, "http://gpu-2:8188", 0, 0)
        _route(respx_mock, "http://gpu-3:8188", 0, 0)

        scheduler = _new_scheduler()

        async def acquire_two() -> tuple[ComfyUINodeLease, ComfyUINodeLease]:
            first = await scheduler.acquire_comfyui_node("http://gpu-1:8188\nhttp://gpu-2:8188\nhttp://gpu-3:8188")
            second = await scheduler.acquire_comfyui_node("http://gpu-1:8188\nhttp://gpu-2:8188\nhttp://gpu-3:8188")
            return first, second

        first, second = asyncio.run(acquire_two())
        assert first.base_url == "http://gpu-1:8188"
        assert second.base_url == "http://gpu-2:8188"
        first.release()
        second.release()

    def test_unreachable_node_skipped(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get("http://gpu-1:8188/queue").mock(side_effect=httpx.ConnectError("down"))
        _route(respx_mock, "http://gpu-2:8188", 0, 0)

        scheduler = _new_scheduler()
        lease = asyncio.run(scheduler.acquire_comfyui_node("http://gpu-1:8188\nhttp://gpu-2:8188"))
        assert lease.base_url == "http://gpu-2:8188"
        lease.release()


class TestWaitingAndCancellation:
    def test_waits_when_all_nodes_full_then_acquires_on_release(self, respx_mock: respx.MockRouter) -> None:
        _route(respx_mock, "http://gpu-1:8188", 1, 1)
        scheduler = _new_scheduler()

        async def scenario() -> ComfyUINodeLease:
            acquire_task = asyncio.create_task(scheduler.acquire_comfyui_node("http://gpu-1:8188", task_id=7))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            # 节点仍满载，等待者保持排队；释放后重新调度应拿到租约。
            assert not acquire_task.done()
            scheduler._release_reservation("http://gpu-1:8188")
            _route(respx_mock, "http://gpu-1:8188", 0, 0)
            return await asyncio.wait_for(acquire_task, timeout=2)

        lease = asyncio.run(scenario())
        assert lease.base_url == "http://gpu-1:8188"
        lease.release()

    def test_cancel_waiting_task(self, respx_mock: respx.MockRouter) -> None:
        _route(respx_mock, "http://gpu-1:8188", 1, 1)
        scheduler = _new_scheduler()

        async def scenario() -> None:
            acquire_task = asyncio.create_task(scheduler.acquire_comfyui_node("http://gpu-1:8188", task_id=3))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not acquire_task.done()
            assert scheduler.cancel_comfyui_scheduling(3) is True
            with pytest.raises(ComfyUISchedulingCancelledError):
                await acquire_task

        asyncio.run(scenario())

    def test_cancel_via_is_cancelled_callback(self, respx_mock: respx.MockRouter) -> None:
        _route(respx_mock, "http://gpu-1:8188", 1, 1)
        scheduler = _new_scheduler()

        async def scenario() -> None:
            acquire_task = asyncio.create_task(
                scheduler.acquire_comfyui_node(
                    "http://gpu-1:8188",
                    is_cancelled=lambda: asyncio.sleep(0, result=True),
                )
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            with pytest.raises(ComfyUISchedulingCancelledError):
                await acquire_task

        asyncio.run(scenario())

    def test_cancel_during_health_check_does_not_consume_next_waiter(self, respx_mock: respx.MockRouter) -> None:
        gate = asyncio.Event()

        async def gated_health_check(request: httpx.Request) -> httpx.Response:
            await gate.wait()
            return httpx.Response(200, json=_queue_payload(0, 0))

        respx_mock.get("http://gpu-1:8188/queue").mock(side_effect=gated_health_check)
        scheduler = _new_scheduler()

        async def scenario() -> None:
            cancelled = asyncio.create_task(scheduler.acquire_comfyui_node("http://gpu-1:8188", task_id=4))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert scheduler.cancel_comfyui_scheduling(4) is True
            with pytest.raises(ComfyUISchedulingCancelledError):
                await cancelled
            # 健康检查在途取消后，后续等待者仍能拿到同一节点租约。
            gate.set()
            next_waiter = asyncio.create_task(scheduler.acquire_comfyui_node("http://gpu-1:8188", task_id=5))
            lease = await asyncio.wait_for(next_waiter, timeout=2)
            assert lease.base_url == "http://gpu-1:8188"
            lease.release()

        asyncio.run(scenario())


class TestIntervalAndExpiry:
    def test_first_admission_not_gated_by_interval(self, respx_mock: respx.MockRouter) -> None:
        _route(respx_mock, "http://gpu-1:8188", 0, 0)
        clock = FakeClock()
        scheduler = _new_scheduler(clock)

        async def scenario() -> None:
            # 首次提交无需等待间隔（无上次提交时间）。
            first = await scheduler.acquire_comfyui_node("http://gpu-1:8188")
            assert first.base_url == "http://gpu-1:8188"
            assert clock.now < COMFYUI_MIN_SUBMISSION_INTERVAL_MS / 1000.0
            first.release()

        asyncio.run(scenario())

    def test_second_admission_blocked_within_interval(self, respx_mock: respx.MockRouter) -> None:
        _route(respx_mock, "http://gpu-1:8188", 0, 0)
        clock = FakeClock()
        scheduler = _new_scheduler(clock)

        async def scenario() -> None:
            first = await scheduler.acquire_comfyui_node("http://gpu-1:8188")
            first.release()
            # 间隔窗口内提交被拦截：等待直到重试调度推进时钟越过间隔。
            second_task = asyncio.create_task(scheduler.acquire_comfyui_node("http://gpu-1:8188"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not second_task.done()
            lease = await asyncio.wait_for(second_task, timeout=2)
            assert lease.base_url == "http://gpu-1:8188"
            lease.release()

        asyncio.run(scenario())

    def test_stale_reservation_expires(self, respx_mock: respx.MockRouter) -> None:
        _route(respx_mock, "http://gpu-1:8188", 0, 0)
        clock = FakeClock()
        scheduler = _new_scheduler(clock)

        async def scenario() -> None:
            first = await scheduler.acquire_comfyui_node("http://gpu-1:8188")
            assert first.base_url == "http://gpu-1:8188"
            # 不释放，直接过期。
            clock.advance(COMFYUI_MAX_RESERVATION_MS / 1000.0 + 1)
            second = await scheduler.acquire_comfyui_node("http://gpu-1:8188")
            assert second.base_url == "http://gpu-1:8188"
            second.release()

        asyncio.run(scenario())


class TestLeaseRelease:
    def test_release_is_idempotent_and_frees_slot(self, respx_mock: respx.MockRouter) -> None:
        _route(respx_mock, "http://gpu-1:8188", 0, 0)
        scheduler = _new_scheduler()

        async def scenario() -> None:
            first = await scheduler.acquire_comfyui_node("http://gpu-1:8188")
            first.release()
            first.release()
            second = await scheduler.acquire_comfyui_node("http://gpu-1:8188")
            assert second.base_url == "http://gpu-1:8188"
            second.release()

        asyncio.run(scenario())


def test_health_timeout_constant_used_in_default() -> None:
    assert COMFYUI_HEALTH_TIMEOUT_MS == 15_000


class TestModuleLevelEntryPoints:
    def test_acquire_cancel_and_reset(self, respx_mock: respx.MockRouter) -> None:
        _route(respx_mock, "http://gpu-1:8188", 1, 1)

        async def scenario() -> None:
            acquire_task = asyncio.create_task(acquire_comfyui_node("http://gpu-1:8188", task_id=9))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert cancel_comfyui_scheduling(9) is True
            with pytest.raises(ComfyUISchedulingCancelledError):
                await acquire_task
            # 不存在任务返回 False，reset 后清空状态且取消不命中。
            assert cancel_comfyui_scheduling(9) is False
            reset_comfyui_scheduler_for_tests()
            assert cancel_comfyui_scheduling(9) is False

        asyncio.run(scenario())
