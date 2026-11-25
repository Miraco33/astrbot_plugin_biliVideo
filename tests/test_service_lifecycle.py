"""Offline composition smoke test: build, report health and release services."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bilivideo import __version__
from bilivideo import services as composition
from bilivideo.auth.qrlogin import LoginStatus
from bilivideo.core.config import PluginConfig
from bilivideo.core.constants import ENDPOINT_NAV
from bilivideo.core.exceptions import NetworkError
from bilivideo.handlers import login, status


@pytest.mark.asyncio
async def test_recovery_services_construct_report_and_shutdown(tmp_path, monkeypatch):
    monkeypatch.setattr(
        composition,
        "RenderChain",
        lambda **_kwargs: SimpleNamespace(available_backends=(), backend_diagnostics={}),
    )
    monkeypatch.setattr(status.shutil, "which", lambda _name: "mock-system-tool")
    services = composition.BiliVideoServices(
        config=PluginConfig(),
        data_dir=str(tmp_path),
    )
    try:
        event = SimpleNamespace(plain_result=lambda text: text)
        request = AsyncMock()
        monkeypatch.setattr(services.http_client, "request_json", request)
        replies = [reply async for reply in status.handle_status(services, event)]
        request.assert_not_awaited()
        assert "未保存登录信息" in replies[0]
        assert len(replies) == 1
        assert "biliVideo（自用维护）" in replies[0]
        assert f"版本: {__version__}" in replies[0]
        assert "订阅: 0 个 / 推送目标: 0 个" in replies[0]
        assert "待处理 0 / 自动重试耗尽 0" in replies[0]
        assert "备用语音转写: 关闭" in replies[0]
    finally:
        await services.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error", "expected_status", "can_scan"),
    [
        ({"code": 0, "data": {"isLogin": True}}, None, "已登录（B站核验）", False),
        ({"code": 0, "data": {"isLogin": False}}, None, "登录已失效", True),
        ({"code": -101, "data": {}}, None, "登录已失效", True),
        ({"code": -352, "data": {"isLogin": False}}, None, "暂无法核验", False),
        ({"code": -412}, None, "暂无法核验", False),
        ({"code": False, "data": {"isLogin": True}}, None, "暂无法核验", False),
        ({"code": 0, "data": {"isLogin": "false"}}, None, "暂无法核验", False),
        ({"code": 0, "data": []}, None, "暂无法核验", False),
        (None, NetworkError("client 412"), "暂无法核验", False),
        (None, asyncio.TimeoutError(), "暂无法核验", False),
        (None, UnicodeDecodeError("utf-8", b"\xff", 0, 1, "mock"), "暂无法核验", False),
    ],
)
async def test_saved_bilibili_session_checked_in_status_and_login(
    tmp_path, monkeypatch, *, response, error, expected_status, can_scan,
):
    monkeypatch.setattr(
        composition,
        "RenderChain",
        lambda **_kwargs: SimpleNamespace(available_backends=(), backend_diagnostics={}),
    )
    monkeypatch.setattr(status.shutil, "which", lambda _name: "mock-system-tool")
    services = composition.BiliVideoServices(config=PluginConfig(), data_dir=str(tmp_path))
    assert services.update_cookies({"SESSDATA": "old-test-value"})
    cookie_file = tmp_path / "bili_cookies.json"
    saved = cookie_file.read_bytes()

    request = AsyncMock(return_value=response, side_effect=error)
    generate = AsyncMock(return_value=None)
    monkeypatch.setattr(services.http_client, "request_json", request)
    monkeypatch.setattr(services.qrlogin, "generate", generate)
    event = SimpleNamespace(plain_result=lambda text: text)
    try:
        replies = [reply async for reply in status.handle_status(services, event)]
        request.assert_awaited_once_with("GET", ENDPOINT_NAV, expect_code_zero=False)
        assert expected_status in replies[0]

        replies = [reply async for reply in login.handle_login(services, event)]
        if can_scan:
            generate.assert_awaited_once_with()
            assert "正在生成" in replies[0]
        else:
            generate.assert_not_awaited()
            assert ("暂无法核验" if expected_status == "暂无法核验" else "已登录") in replies[0]
        assert cookie_file.read_bytes() == saved
    finally:
        await services.shutdown()


@pytest.mark.asyncio
async def test_session_check_cancels_slow_request(tmp_path, monkeypatch):
    monkeypatch.setattr(composition, "BILIBILI_SESSION_CHECK_TIMEOUT", 0.05)
    services = composition.BiliVideoServices(config=PluginConfig(), data_dir=str(tmp_path))
    assert services.update_cookies({"SESSDATA": "test-value"})
    request_cancelled = asyncio.Event()

    async def stall(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            request_cancelled.set()

    monkeypatch.setattr(services.http_client, "request_json", stall)
    try:
        assert await services.check_bilibili_session() is composition.BilibiliSessionStatus.UNVERIFIED
        assert request_cancelled.is_set()
    finally:
        await services.shutdown()


@pytest.mark.asyncio
async def test_logout_invalidates_scan_in_progress(tmp_path, monkeypatch):
    import segno

    services = composition.BiliVideoServices(config=PluginConfig(), data_dir=str(tmp_path))
    monkeypatch.setattr(login, "Plain", lambda text: text)
    monkeypatch.setattr(login, "Image", SimpleNamespace(fromFileSystem=lambda path: path))
    monkeypatch.setattr(
        segno, "make", lambda _url: SimpleNamespace(save=lambda path, **_kw: Path(path).touch()),
    )
    monkeypatch.setattr(services.qrlogin, "generate", AsyncMock(return_value=SimpleNamespace(url="", key="")))
    scan_result = SimpleNamespace(status=LoginStatus.SUCCESS, cookies={"SESSDATA": "test-value"})
    monkeypatch.setattr(services.qrlogin, "run_until_complete", AsyncMock(return_value=scan_result))
    event = SimpleNamespace(plain_result=lambda text: text, chain_result=lambda chain: chain)
    scan = login.handle_login(services, event)
    try:
        assert "正在生成" in await anext(scan)
        _, qr_path = await anext(scan)
        assert Path(qr_path).exists()

        replies = [reply async for reply in login.handle_logout(services, event)]
        assert replies == ["ℹ️ 当前未登录B站"]
        assert "已作废" in await anext(scan)
        assert not Path(qr_path).exists()
        assert not services.is_logged_in()
    finally:
        await scan.aclose()
        await services.shutdown()
