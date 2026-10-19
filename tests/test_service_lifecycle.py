"""Offline composition smoke test: build, report health and release services."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bilivideo import __version__
from bilivideo import services as composition
from bilivideo.core.config import PluginConfig
from bilivideo.handlers import status


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
        replies = [reply async for reply in status.handle_status(services, event)]
        assert len(replies) == 1
        assert "biliVideo（自用维护）" in replies[0]
        assert f"版本: {__version__}" in replies[0]
        assert "订阅: 0 个 / 推送目标: 0 个" in replies[0]
        assert "待处理 0 / 自动重试耗尽 0" in replies[0]
        assert "备用语音转写: 关闭" in replies[0]
    finally:
        await services.shutdown()
