"""ComfyUI 池端到端验证（mock ComfyUI 主机，不依赖真实实例）。

覆盖 issue #8 验收主链路：池发现/容量聚合、调度器选主机、租约互斥、满载不无限提交，
以及 ComfyUI 后端出站链路的故障用户可见错误。ComfyUI 的 HTTP 侧全部用 respx 拦截
（GET /queue、GET /system_stats、POST /prompt、GET /history、GET /view、
POST /upload/image），不触达真实外部服务（e2e 档由 CI 单独覆盖真实实例）。

三个验收维度落点：

1. **2 台主机各 2 容量时任务分布正确**：调度器租约「空闲优先 + 节点级互斥」——
   两主机同时空闲时两笔并发申请落到不同主机；``pool_capacity`` = 各主机容量之和。
2. **单台满载时任务不向 ComfyUI 无限提交**：宿主 /queue 恒报 running=1、pending=1
   （负载 2 = 容量满），调度器对满载节点不发租约、不产生任何 POST /prompt。
3. **故障的用户可见错误**：主机不可达 → 调度器跳过该节点；工作流校验失败在构造期抛
   ``ComfyUIWorkflowError``；history 终态无视频输出抛带文案的 RuntimeError；
   history execution_error 透传异常消息；轮询超时抛 TimeoutError。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import httpx
import pytest
import respx

from lib.custom_provider.comfyui_pool import COMFYUI_POOL_PROVIDER_ID, ComfyUIPoolHost, pool_capacity
from lib.custom_provider.comfyui_scheduler import (
    ComfyUIPoolScheduler,
    reset_comfyui_scheduler_for_tests,
)
from tests.fakes import bounded_poll_clock
from tests.http_capture import capture_http


@pytest.fixture(autouse=True)
def _reset_default_scheduler():
    """每个用例独立重置模块级调度器状态（跨用例租约/等待队列不得泄漏）。"""
    reset_comfyui_scheduler_for_tests()
    yield
    reset_comfyui_scheduler_for_tests()


class FakeClock:
    """可注入的假时钟：monotonic 与 sleep 同步推进（与单元测试同款，见 test_comfyui_scheduler.py）。"""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _scheduler(clock: FakeClock | None = None) -> ComfyUIPoolScheduler:
    """构造带假时钟的调度器（测试不依赖真实 5s 重试 / 8s 提交间隔）。"""
    return ComfyUIPoolScheduler(clock=clock if clock is not None else FakeClock())


# ── ComfyUI 主机 HTTP 路由替身 ─────────────────────────────────────────────


class _MockComfyUIHost:
    """一台 mock ComfyUI 主机：/queue 负载 + /prompt 提交记录 + 完成态 /history。

    出站行为集中在本类，测试通过替换 ``submit_behavior`` / ``history`` 方法注入
    故障形态（如 503、无输出、execution_error、一直进行中）。
    """

    def __init__(self, base_url: str, *, running: int = 0, pending: int = 0) -> None:
        self.base_url = base_url
        self.running = running
        self.pending = pending
        self.submits: list[dict] = []
        self.uploads = 0

    # ── 负载/状态 ──

    def queue_payload(self) -> dict:
        return {
            "queue_running": [[i, f"{self.base_url}-r-{i}"] for i in range(self.running)],
            "queue_pending": [[i, f"{self.base_url}-p-{i}"] for i in range(self.pending)],
        }

    def stats_payload(self) -> dict:
        return {
            "system": {
                "comfyui_version": "0.3.50",
                "device_name": self.base_url,
                "python_version": "3.12.4",
                "torch_version": "2.3.0",
            },
            "devices": [{"name": self.base_url, "type": "cuda", "vram_total": 25139646464}],
        }

    # ── 出站行为 ──

    def _handle_submit(self, request: httpx.Request) -> httpx.Response:
        self.submits.append(json.loads(request.content))
        return httpx.Response(200, json={"prompt_id": f"p-{len(self.submits)}"})

    def _handle_upload(self, request: httpx.Request) -> httpx.Response:
        del request
        self.uploads += 1
        return httpx.Response(200, json={"name": "image-1.png", "subfolder": "arcreel/task-local", "type": "input"})

    def _handle_history(self, request: httpx.Request) -> httpx.Response:
        prompt_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                prompt_id: {
                    "status": {"status_str": "success", "completed": True},
                    "outputs": {
                        "92": {
                            "videos": [
                                {
                                    "filename": f"{prompt_id}.mp4",
                                    "subfolder": "video/MiniMax_H3",
                                    "type": "output",
                                }
                            ]
                        }
                    },
                }
            },
        )

    def register(self, router: respx.MockRouter) -> None:
        router.get(f"{self.base_url}/system_stats").mock(return_value=httpx.Response(200, json=self.stats_payload()))
        router.get(f"{self.base_url}/queue").mock(
            side_effect=lambda _request: httpx.Response(200, json=self.queue_payload())
        )
        router.post(f"{self.base_url}/prompt").mock(side_effect=self._handle_submit)
        router.post(f"{self.base_url}/upload/image").mock(side_effect=self._handle_upload)
        router.get(url__regex=rf"^{self.base_url}/history/[^/]+$").mock(side_effect=self._handle_history)
        router.get(url__regex=rf"^{self.base_url}/view\?").mock(return_value=httpx.Response(200, content=b"mp4-bytes"))


def _register_host(router: respx.MockRouter, base_url: str, *, running: int = 0, pending: int = 0) -> _MockComfyUIHost:
    host = _MockComfyUIHost(base_url, running=running, pending=pending)
    host.register(router)
    return host


def _pool_host(base_url: str, *, workers: int = 2, model: str = "minimax-h3-ref2va") -> ComfyUIPoolHost:
    return ComfyUIPoolHost(
        provider_id=f"custom-{base_url}",
        base_url=base_url,
        api_key="",
        video_max_workers=workers,
        default_model_id=model,
    )


# ── 维度 1：两主机各 2 容量，任务分布正确 ──────────────────────────────────


@pytest.mark.asyncio
async def test_two_idle_hosts_spread_concurrent_leases_across_both(respx_mock: respx.MockRouter):
    """两主机空闲时，两笔并发租约申请落到不同主机（节点级互斥 + 空闲优先）。"""
    _register_host(respx_mock, "http://gpu-1:8188")
    _register_host(respx_mock, "http://gpu-2:8188")
    scheduler = _scheduler()

    async def _scenario() -> tuple[str, str]:
        first = await scheduler.acquire_comfyui_node("http://gpu-1:8188\nhttp://gpu-2:8188")
        second = await scheduler.acquire_comfyui_node("http://gpu-1:8188\nhttp://gpu-2:8188")
        return first.base_url, second.base_url

    first, second = await _scenario()
    assert {first, second} == {"http://gpu-1:8188", "http://gpu-2:8188"}
    assert first != second

    # 池容量 = 各主机 video_max_workers 之和（claim 层按池总量限流）
    hosts = [_pool_host("http://gpu-1:8188", workers=2), _pool_host("http://gpu-2:8188", workers=2)]
    assert pool_capacity(hosts) == 4


@pytest.mark.asyncio
async def test_distribution_favors_idle_over_loaded_host(respx_mock: respx.MockRouter):
    """一主机已有负载（running=1）、另一主机空闲时，租约选空闲主机。"""
    _register_host(respx_mock, "http://gpu-1:8188", running=1, pending=0)
    _register_host(respx_mock, "http://gpu-2:8188")
    scheduler = _scheduler()

    lease = await scheduler.acquire_comfyui_node("http://gpu-1:8188\nhttp://gpu-2:8188")
    assert lease.base_url == "http://gpu-2:8188"
    lease.release()


# ── 维度 2：单台满载时任务不向 ComfyUI 无限提交 ────────────────────────────


@pytest.mark.asyncio
async def test_full_host_never_grants_lease_and_never_submits(respx_mock: respx.MockRouter):
    """主机 /queue 恒报 running=1 + pending=1（负载 2 = 容量满）时，租约申请保持等待，
    整个等待窗口内不产生任何 POST /prompt——「满载不向 ComfyUI 无限提交」。"""
    host = _register_host(respx_mock, "http://gpu-1:8188", running=1, pending=1)
    scheduler = _scheduler()

    async def _scenario() -> None:
        acquire_task = asyncio.create_task(scheduler.acquire_comfyui_node("http://gpu-1:8188", task_id=1))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not acquire_task.done(), "满载主机不应发放租约"
        # 主机负载下降后调度重试发放租约
        host.running = 0
        host.pending = 0
        for _ in range(10):
            await asyncio.sleep(0)
        lease = await asyncio.wait_for(acquire_task, timeout=2)
        lease.release()

    await _scenario()
    assert host.submits == [], "满载等待期间不得向 ComfyUI 提交任务"


@pytest.mark.asyncio
async def test_release_frees_next_slot_for_second_task(respx_mock: respx.MockRouter):
    """单主机容量 2：第一笔租约释放后，第二笔申请才能获得租约（容量不超发）。"""
    _register_host(respx_mock, "http://gpu-1:8188")
    scheduler = _scheduler()

    first = await scheduler.acquire_comfyui_node("http://gpu-1:8188")
    assert first.base_url == "http://gpu-1:8188"
    # 主机仍空载但第一笔租约未释放：同节点互斥，第二笔只能等待
    second_task = asyncio.create_task(scheduler.acquire_comfyui_node("http://gpu-1:8188", task_id=2))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not second_task.done(), "同节点租约未释放时不得再发租约"
    first.release()
    second = await asyncio.wait_for(second_task, timeout=2)
    assert second.base_url == "http://gpu-1:8188"
    second.release()


# ── 维度 3：故障的用户可见错误 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unreachable_host_skipped_and_healthy_host_used(respx_mock: respx.MockRouter):
    """主机不可达（连接拒绝）时调度器跳过该节点，租约落到健康主机。"""
    respx_mock.get("http://gpu-1:8188/queue").mock(side_effect=httpx.ConnectError("down"))
    _register_host(respx_mock, "http://gpu-2:8188")
    scheduler = _scheduler()

    lease = await scheduler.acquire_comfyui_node("http://gpu-1:8188\nhttp://gpu-2:8188")
    assert lease.base_url == "http://gpu-2:8188"
    lease.release()


@pytest.mark.asyncio
async def test_workflow_validation_failure_is_user_visible(tmp_path: Path):
    """覆盖工作流缺 SaveVideo 节点：构造期抛 ComfyUIWorkflowError（用户可见错误）。"""
    from lib.video_backends.comfyui import ComfyUIVideoBackend, ComfyUIWorkflowError

    bad_workflow = {
        "1": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {"prompt": "x"}},
    }
    with pytest.raises(ComfyUIWorkflowError, match="SaveVideo"):
        ComfyUIVideoBackend(
            base_url="http://gpu-1:8188",
            model="custom-model",
            configured_workflows=[{"model": "custom-model", "workflow": bad_workflow}],
        )


@pytest.mark.asyncio
async def test_history_without_video_output_raises_user_visible_error(tmp_path: Path):
    """history 终态 success 但无视频输出 → 用户可见错误文案（不静默成功）。"""
    from lib.video_backends.base import VideoGenerationRequest
    from lib.video_backends.comfyui import ComfyUIVideoBackend

    output = tmp_path / "o.mp4"
    with capture_http() as http:
        http.post("http://gpu-1:8188/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-empty"}))
        http.get("http://gpu-1:8188/history/p-empty").mock(
            return_value=httpx.Response(
                200,
                json={
                    "p-empty": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {},
                    }
                },
            )
        )
        backend = ComfyUIVideoBackend(base_url="http://gpu-1:8188")
        with pytest.raises(RuntimeError, match="没有找到视频输出"):
            await backend.generate(
                VideoGenerationRequest(prompt="p", output_path=output, aspect_ratio="9:16", duration_seconds=5)
            )


@pytest.mark.asyncio
async def test_history_execution_error_message_is_user_visible(tmp_path: Path):
    """history 报 execution_error → 异常消息透传为用户可见错误。"""
    from lib.video_backends.base import VideoGenerationRequest
    from lib.video_backends.comfyui import ComfyUIVideoBackend

    output = tmp_path / "o.mp4"
    with capture_http() as http:
        http.post("http://gpu-1:8188/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-fail"}))
        http.get("http://gpu-1:8188/history/p-fail").mock(
            return_value=httpx.Response(
                200,
                json={
                    "p-fail": {
                        "status": {
                            "status_str": "error",
                            "completed": True,
                            "messages": [["execution_error", {"exception_message": "CUDA out of memory"}]],
                        },
                        "outputs": {},
                    }
                },
            )
        )
        backend = ComfyUIVideoBackend(base_url="http://gpu-1:8188")
        with pytest.raises(RuntimeError, match="CUDA out of memory"):
            await backend.generate(
                VideoGenerationRequest(prompt="p", output_path=output, aspect_ratio="9:16", duration_seconds=5)
            )


@pytest.mark.asyncio
async def test_polling_timeout_raises_user_visible_error(tmp_path: Path):
    """轮询持续不达终态 → TimeoutError，且不产生下载请求。"""
    from lib.video_backends.base import VideoGenerationRequest
    from lib.video_backends.comfyui import ComfyUIVideoBackend

    output = tmp_path / "o.mp4"
    with capture_http() as http, bounded_poll_clock():
        http.post("http://gpu-1:8188/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-slow"}))
        http.get("http://gpu-1:8188/history/p-slow").mock(return_value=httpx.Response(200, json={}))

        backend = ComfyUIVideoBackend(base_url="http://gpu-1:8188")
        with pytest.raises(TimeoutError, match="ComfyUI"):
            await backend.generate(
                VideoGenerationRequest(prompt="p", output_path=output, aspect_ratio="9:16", duration_seconds=5)
            )


# ── 主链路补强：从池选主机到 backend 提交（pin 语义） ───────────────────────


@pytest.mark.asyncio
async def test_pin_pool_host_payload_locks_host_for_resolution():
    """选中主机 pin 进执行 payload（video_provider_<cap> = <provider>/<model>）。"""
    from lib.custom_provider.comfyui_pool import pin_pool_host_payload

    host = _pool_host("http://gpu-1:8188", model="minimax-h3-8step")
    original = {"prompt": "x"}
    pinned = pin_pool_host_payload(original, host, "i2v")
    assert pinned["video_provider_i2v"] == "custom-http://gpu-1:8188/minimax-h3-8step"
    assert pinned["prompt"] == "x"
    # 原 payload 不被改写
    assert "video_provider_i2v" not in original


@pytest.mark.asyncio
async def test_backend_uses_host_base_url_for_full_roundtrip(tmp_path: Path):
    """主链路：backend 上传 → 提交 → 轮询 → 下载，全部命中同一主机 base_url。"""
    from lib.video_backends.base import VideoGenerationRequest
    from lib.video_backends.comfyui import ComfyUIVideoBackend

    output = tmp_path / "o.mp4"
    with capture_http() as http:
        host = _register_host(http, "http://gpu-1:8188")
        backend = ComfyUIVideoBackend(base_url="http://gpu-1:8188", model="minimax-h3-ref2va")
        result = await backend.generate(
            VideoGenerationRequest(prompt="一只猫", output_path=output, aspect_ratio="9:16", duration_seconds=5)
        )

    assert result.video_path == output
    assert output.read_bytes() == b"mp4-bytes"
    assert result.task_id == "p-1"
    assert host.uploads == 0  # 无参考素材：不上传
    assert len(host.submits) == 1
    workflow = host.submits[0]["prompt"]
    assert workflow["136"]["class_type"] == "MiniMaxH3ReferenceToVideo"
    assert workflow["92"]["class_type"] == "SaveVideo"


# ── 全链路：DB 配置 ComfyUI provider → 池发现 → 选主机 → 提交 → 轮询 → 下载 ──


@pytest.mark.asyncio
async def test_full_flow_from_db_provider_to_downloaded_video(db_factory, monkeypatch, tmp_path: Path):
    """验收主链路：DB 建 ComfyUI provider → discover 成池成员 → 选主机（调度器租约）
    → pin 进执行 payload → load_custom_backend 构造真实 ComfyUIVideoBackend →
    上传/提交/轮询/下载全命中被选主机。mock 主机代替真实 ComfyUI。"""
    from lib.custom_provider.comfyui_pool import discover_comfyui_pool
    from lib.custom_provider.comfyui_scheduler import reset_comfyui_scheduler_for_tests
    from lib.db.models.custom_provider import CustomProvider, CustomProviderModel

    async with db_factory() as session:
        for i, base_url in enumerate(("http://gpu-1:8188", "http://gpu-2:8188"), start=1):
            provider = CustomProvider(
                display_name=f"ComfyUI-{i}",
                discovery_format="openai",
                base_url=base_url,
                api_key="",
                video_max_workers=2,
            )
            session.add(provider)
            await session.flush()
            session.add(
                CustomProviderModel(
                    provider_id=provider.id,
                    model_id="minimax-h3-ref2va",
                    display_name="MiniMax H3",
                    endpoint="comfyui-video",
                    is_default=True,
                    is_enabled=True,
                )
            )
        await session.commit()

    monkeypatch.setattr("lib.db.safe_session_factory", db_factory)
    hosts = await discover_comfyui_pool()
    assert len(hosts) == 2
    assert {h.base_url for h in hosts} == {"http://gpu-1:8188", "http://gpu-2:8188"}
    assert pool_capacity(hosts) == 4

    with capture_http() as router:
        h1 = _register_host(router, "http://gpu-1:8188")
        _register_host(router, "http://gpu-2:8188")
        scheduler = _scheduler()

        async def _select(*, is_cancelled=None, exclude_base_urls=frozenset()):
            from lib.custom_provider.comfyui_pool import (
                ComfyUIPoolUnavailableError,
                find_host_by_base_url,
            )

            remaining = [h for h in hosts if h.base_url not in exclude_base_urls]
            if not remaining:
                raise ComfyUIPoolUnavailableError("未发现可用的 ComfyUI 池成员")
            lease = await scheduler.acquire_comfyui_node(
                "\n".join(h.base_url for h in remaining),
                "",
                is_cancelled=is_cancelled,
            )
            host = find_host_by_base_url(remaining, lease.base_url)
            if host is None:
                lease.release()
                raise ComfyUIPoolUnavailableError("租约主机不在池成员中")
            return host, lease

        host, lease = await _select()
        assert host.base_url in {"http://gpu-1:8188", "http://gpu-2:8188"}

        from lib.custom_provider.comfyui_pool import pin_pool_host_payload
        from lib.video_backends.base import VideoGenerationRequest

        payload = pin_pool_host_payload({"prompt": "一只猫"}, host, "i2v")
        assert payload["video_provider_i2v"].startswith("custom-")

        async with db_factory() as session:
            from lib.custom_provider.backends import CustomVideoBackend
            from lib.custom_provider.loader import load_custom_backend

            backend = cast(
                CustomVideoBackend,
                await load_custom_backend(
                    session=session,
                    provider_id=host.provider_id,
                    model_id=host.default_model_id,
                    media_type="video",
                ),
            )
            assert backend.model == "minimax-h3-ref2va"

        output = tmp_path / "out.mp4"
        result = await backend.generate(
            VideoGenerationRequest(prompt="一只猫", output_path=output, aspect_ratio="9:16", duration_seconds=5)
        )
        assert result.video_path == output
        assert output.read_bytes() == b"mp4-bytes"
        lease.release()

    submits = h1.submits
    assert len(submits) == 1, "完整链路应恰好提交一次到被选主机"
    workflow = submits[0]["prompt"]
    assert workflow["136"]["class_type"] == "MiniMaxH3ReferenceToVideo"
    assert workflow["92"]["class_type"] == "SaveVideo"
    reset_comfyui_scheduler_for_tests()


# ── execute_video_task 池分支：选主机 + pin + DispatchProviderChanged 豁免 ──


@pytest.mark.asyncio
async def test_execute_video_task_pool_branch_pins_host_and_exempts_dispatch_check(monkeypatch, tmp_path: Path):
    """池任务（claimed_provider_id=comfyui-pool）经 execute_video_task：
    select_pool_host 选主机 → pin 进执行 payload → DispatchProviderChanged 豁免
    （池任务解析到具体 custom-N，与虚拟池 id 必然不等，不得回队）。"""
    from server.services import generation_tasks
    from tests.integration.server.services.generation_tasks_support import (
        _async_return,
        _fake_resolve_ctx,
        _FakeGenerator,
        _FakePM,
        _prepare_files,
        _seed_current_storyboard,
    )

    project_path = _prepare_files(tmp_path)
    fake_pm = _FakePM(project_path)
    _seed_current_storyboard(fake_pm)
    item = fake_pm.script["segments"][0]
    item["novel_text"] = "旁白正文"
    item["video_prompt"] = {"action": "跑", "camera_motion": "Static", "dialogue": []}
    item["duration_seconds"] = 8

    class _FormalGenerator(_FakeGenerator):
        async def generate_video_async(self, **kwargs):
            from lib.version_manager import PaidVersionCommit

            kwargs["commit_formal_output"].outcome = PaidVersionCommit(version=2, selected=True)
            return project_path / "videos" / "scene_E1S01.mp4", 2, "ref", "uri"

    fake_generator = _FormalGenerator()

    selected_host = _pool_host("http://gpu-1:8188", model="minimax-h3-8step")

    class _FakeLease:
        def release(self) -> None:
            pass

    async def _fake_select(*, is_cancelled=None, exclude_base_urls=frozenset()):
        del is_cancelled, exclude_base_urls
        return selected_host, _FakeLease()

    seen_payloads: list[dict] = []

    async def _fake_resolve(project_name, payload, *, project, user_id="default", **kwargs):
        seen_payloads.append(dict(payload))
        return await _fake_resolve_ctx(fake_generator)(
            project_name, payload, project=project, user_id=user_id, **kwargs
        )

    monkeypatch.setattr(generation_tasks, "get_project_manager", lambda: fake_pm)
    monkeypatch.setattr("lib.custom_provider.comfyui_pool.select_pool_host", _fake_select)
    monkeypatch.setattr(generation_tasks, "resolve_generation_context", _fake_resolve)
    monkeypatch.setattr(generation_tasks, "extract_video_thumbnail", _async_return(False))
    monkeypatch.setattr(generation_tasks, "emit_generation_success_batch", lambda **kw: None)

    result = await generation_tasks.execute_generation_task(
        {
            "task_id": "task-pool-1",
            "task_type": "video",
            "project_name": "demo",
            "resource_id": "E1S01",
            "payload": {"script_file": "episode_1.json", "prompt": {"action": "跑"}},
        },
        claimed_provider_id=COMFYUI_POOL_PROVIDER_ID,
    )
    assert result["resource_type"] == "videos"
    # pin 进执行 payload：解析层收到的 payload 带 video_provider_i2v = 选中主机
    assert seen_payloads, "resolve_generation_context 应收到 pin 后的 payload"
    assert seen_payloads[0]["video_provider_i2v"] == (f"{selected_host.provider_id}/{selected_host.default_model_id}")


# ── 全链路：DB 配置 ComfyUI provider → 池发现 → 选主机 → 提交 → 轮询 → 下载 ──
