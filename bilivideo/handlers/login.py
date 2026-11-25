"""`/B站登录` and `/B站登出` handlers."""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from ..auth.qrlogin import LoginStatus
from ..services import BilibiliSessionStatus, BiliVideoServices
from ._utils import require_access

# Lazy import (test environments lack AstrBot)
try:
    from astrbot.api.message_components import Image, Plain  # type: ignore[import]
except Exception:  # pragma: no cover
    Image = Plain = None  # type: ignore[assignment]


@require_access
async def handle_login(services: BiliVideoServices, event: object) -> AsyncIterator[object]:
    checked_cookies = services.http_client.cookies
    session_status = await services.check_bilibili_session()
    if session_status is BilibiliSessionStatus.VALID:
        yield event.plain_result("✅ B站已登录,如需切换账号请先 /B站登出")  # type: ignore[attr-defined]
        return
    if session_status is BilibiliSessionStatus.UNVERIFIED:
        yield event.plain_result("⚠️ B站暂无法核验登录，已保留登录信息，请稍后重试")  # type: ignore[attr-defined]
        return

    if checked_cookies is not services.http_client.cookies:
        yield event.plain_result("ℹ️ 登录信息已变化，请重新发起扫码")  # type: ignore[attr-defined]
        return

    yield event.plain_result("🔄 正在生成B站登录二维码...")  # type: ignore[attr-defined]

    qr = await services.qrlogin.generate()
    if qr is None:
        yield event.plain_result("❌ 生成二维码失败,请稍后重试")  # type: ignore[attr-defined]
        return

    qr_path = Path(services.data_dir) / f"login_qr_{uuid.uuid4().hex[:8]}.png"
    try:
        import segno

        segno.make(qr.url).save(str(qr_path), scale=10, border=4)
    except ImportError:
        yield event.plain_result("❌ 缺少 segno 依赖,请运行: pip install segno")  # type: ignore[attr-defined]
        return
    except Exception as exc:
        yield event.plain_result(f"❌ 生成二维码图片失败: {exc}")  # type: ignore[attr-defined]
        return

    chain = [
        Plain("📱 请使用B站App扫描下方二维码登录\n⏳ 二维码有效期 3 分钟\n"),
        Image.fromFileSystem(str(qr_path)),
    ]
    try:
        yield event.chain_result(chain)  # type: ignore[attr-defined]
        result = await services.qrlogin.run_until_complete(qr.key, total_timeout=180)

        if result.status == LoginStatus.SUCCESS and result.cookies:
            if checked_cookies is not services.http_client.cookies:
                message = "ℹ️ 登录信息已变化，本次二维码已作废"
            elif services.update_cookies(result.cookies):
                message = "✅ B站登录成功!"
            else:
                message = "❌ 扫码成功，但登录信息保存失败"
        elif result.status == LoginStatus.EXPIRED:
            message = "⏰ 二维码已过期,请重新发送 /B站登录"
        elif result.status == LoginStatus.TIMEOUT:
            message = "⏰ 登录超时,请重新发送 /B站登录"
        else:
            message = "❌ 登录失败,请重新发送 /B站登录"
    finally:
        with contextlib.suppress(OSError):
            os.remove(qr_path)
    yield event.plain_result(message)  # type: ignore[attr-defined]


@require_access
async def handle_logout(services: BiliVideoServices, event: object) -> AsyncIterator[object]:
    had_credentials = services.is_logged_in()
    if not services.update_cookies(None):
        yield event.plain_result("❌ 无法清除已保存的登录信息，请检查存储后重试")  # type: ignore[attr-defined]
    elif had_credentials:
        yield event.plain_result("✅ 已退出B站登录")  # type: ignore[attr-defined]
    else:
        yield event.plain_result("ℹ️ 当前未登录B站")  # type: ignore[attr-defined]
