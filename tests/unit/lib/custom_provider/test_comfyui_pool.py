"""ComfyUI 池发现与聚合单元测试。"""

from __future__ import annotations

import pytest

from lib.custom_provider.comfyui_pool import (
    COMFYUI_POOL_PROVIDER_ID,
    ComfyUIPoolHost,
    find_host_by_base_url,
    pin_pool_host_payload,
    pool_base_urls,
    pool_capacity,
)


def _host(
    pid: str, base_url: str, *, workers: int = 2, api_key: str = "", model: str = "minimax-h3-ref2va"
) -> ComfyUIPoolHost:
    return ComfyUIPoolHost(
        provider_id=pid,
        base_url=base_url,
        api_key=api_key,
        video_max_workers=workers,
        default_model_id=model,
    )


class TestPoolCapacity:
    def test_sum_of_member_workers(self) -> None:
        hosts = [_host("custom-1", "http://gpu-1:8188", workers=2), _host("custom-2", "http://gpu-2:8188", workers=3)]
        assert pool_capacity(hosts) == 5

    def test_empty_pool_capacity_zero(self) -> None:
        assert pool_capacity([]) == 0


class TestPoolBaseUrls:
    def test_newline_joined(self) -> None:
        hosts = [_host("custom-1", "http://gpu-1:8188"), _host("custom-2", "http://gpu-2:8188")]
        assert pool_base_urls(hosts) == "http://gpu-1:8188\nhttp://gpu-2:8188"

    def test_empty_pool(self) -> None:
        assert pool_base_urls([]) == ""


class TestFindHostByBaseUrl:
    def test_exact_match_normalizes_trailing_slash(self) -> None:
        hosts = [_host("custom-1", "http://gpu-1:8188")]
        assert find_host_by_base_url(hosts, "http://gpu-1:8188/") is hosts[0]

    def test_no_match_returns_none(self) -> None:
        hosts = [_host("custom-1", "http://gpu-1:8188")]
        assert find_host_by_base_url(hosts, "http://gpu-9:8188") is None

    def test_pool_provider_id_is_distinct_from_custom_n(self) -> None:
        assert COMFYUI_POOL_PROVIDER_ID == "comfyui-pool"
        assert not COMFYUI_POOL_PROVIDER_ID.startswith("custom-")


@pytest.mark.parametrize(
    ("workers", "expected"),
    [
        (1, 1),
        (2, 2),
        (4, 4),
    ],
)
def test_single_host_capacity(workers: int, expected: int) -> None:
    hosts = [_host("custom-1", "http://gpu-1:8188", workers=workers)]
    assert pool_capacity(hosts) == expected


class TestPinPoolHostPayload:
    def test_pins_provider_model_into_capability_key(self) -> None:
        host = _host("custom-1", "http://gpu-1:8188", model="minimax-h3-8step")
        pinned = pin_pool_host_payload({"prompt": "x"}, host, "i2v")
        assert pinned["video_provider_i2v"] == "custom-1/minimax-h3-8step"
        assert pinned["prompt"] == "x"

    def test_does_not_mutate_original_payload(self) -> None:
        original = {"prompt": "x"}
        host = _host("custom-1", "http://gpu-1:8188")
        pin_pool_host_payload(original, host, "i2v")
        assert "video_provider_i2v" not in original

    def test_none_payload_becomes_dict(self) -> None:
        host = _host("custom-1", "http://gpu-1:8188")
        pinned = pin_pool_host_payload(None, host, "r2v")
        assert pinned["video_provider_r2v"] == "custom-1/minimax-h3-ref2va"
