"""Health-check (`/总结状态`) and cache-clear (`/总结清缓存`) handlers."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator

from .. import __version__
from ..api.endpoints import clear_video_info_cache, video_info_cache_size
from ..api.wbi import clear_wbi_cache
from ..core.logging import get_logger
from ..llm.provider import DisabledLLMProvider
from ..services import BilibiliSessionStatus, BiliVideoServices
from ..subscription.scheduler import CheckScheduler

logger = get_logger("BiliVideo/Status")


def _scheduler_state(enabled: bool, scheduler: CheckScheduler | None) -> str:
    if not enabled:
        return "关闭"
    if scheduler is not None and scheduler.is_running():
        return "运行中"
    return "⚠️ 已启用但未运行"


async def handle_status(services: BiliVideoServices, event: object) -> AsyncIterator[object]:
    cfg = services.config
    session_status = await services.check_bilibili_session()
    cookie_state = {
        BilibiliSessionStatus.MISSING: "❌ 未保存登录信息",
        BilibiliSessionStatus.VALID: "✅ 已登录（B站核验）",
        BilibiliSessionStatus.EXPIRED: "❌ 登录已失效，可发送 /B站登录 重新扫码",
        BilibiliSessionStatus.UNVERIFIED: "⚠️ 已保存登录信息，B站请求受限或异常，暂无法核验",
    }[session_status]
    video_scheduler = _scheduler_state(cfg.enable_auto_push, services.scheduler)
    dynamic_scheduler = _scheduler_state(cfg.enable_auto_push, services.dynamic_scheduler)
    targets = await services.subscription_manager.get_push_targets()
    pending_videos = exhausted_videos = pending_dynamics = exhausted_dynamics = subscription_count = 0
    for origin, subscriptions in (await services.subscription_manager.all_subscriptions()).items():
        subscription_count += len(subscriptions)
        for subscription in subscriptions:
            for job in await services.subscription_manager.get_delivery_jobs(origin, subscription.mid):
                pending_videos += job.status in {"pending", "retry"}
                exhausted_videos += job.status == "exhausted"
            for job in await services.subscription_manager.get_dynamic_jobs(origin, subscription.mid):
                pending_dynamics += job.status in {"pending", "retry"}
                exhausted_dynamics += job.status == "exhausted"
    backends = ", ".join(services.renderer.available_backends) or "无 (将回退纯文本)"
    diagnostics = getattr(services.renderer, "backend_diagnostics", {})
    render_diag_lines = "\n".join(
        f"  - {name}: {reason}" for name, reason in diagnostics.items()
    )
    if render_diag_lines:
        render_diag_lines = f"\n🔎 渲染诊断:\n{render_diag_lines}"
    wkhtml = "✅" if shutil.which("wkhtmltoimage") or shutil.which("wkhtmltoimage.exe") else "❌"
    if shutil.which("ffmpeg") or shutil.which("ffmpeg.exe"):
        ffmpeg = "✅ 系统"
    else:
        try:
            import imageio_ffmpeg

            ffmpeg = "✅ 内建" if imageio_ffmpeg.get_ffmpeg_exe() else "❌"
        except Exception:
            ffmpeg = "❌"
    if isinstance(services.llm, DisabledLLMProvider):
        llm_state = "未配置"
    elif cfg.is_openai_compatible:
        llm_state = cfg.llm_model
    else:
        pinned = getattr(services.llm, "provider_id", "") or ""
        llm_state = f"astrbot:{pinned}" if pinned else "astrbot 当前模型"

    if cfg.enable_multi_platform:
        yt_cookies = "有" if services.youtube_cookies.has() else "无"
        multi_state = f"on (实验,仅 /总结;YT cookies:{yt_cookies})"
    else:
        multi_state = "off"

    body = (
        "🩺 biliVideo（自用维护）状态\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"📌 版本: {__version__}\n"
        f"🔐 登录: {cookie_state}\n"
        f"🤖 LLM: {cfg.llm_provider} / {llm_state}\n"
        f"🎨 渲染后端: {backends}\n"
        f"🛠 系统工具: ffmpeg {ffmpeg}  wkhtmltoimage {wkhtml}\n"
        f"🔁 自动识别: {'开' if services.enable_miniapp_detect else '关'}\n"
        f"🌐 多平台: {multi_state}\n"
        f"📡 定时检查: 视频 {video_scheduler}（每 {cfg.check_interval_minutes} 分钟）"
        f" / 动态 {dynamic_scheduler}（每 {cfg.dynamic_check_interval_minutes} 分钟）\n"
        f"📋 订阅: {subscription_count} 个 / 推送目标: {len(targets)} 个\n"
        f"📬 视频投递: 待处理 {pending_videos} / 自动重试耗尽 {exhausted_videos}\n"
        f"📬 动态投递: 待处理 {pending_dynamics} / 自动重试耗尽 {exhausted_dynamics}\n"
        f"🎧 下载 / 转写预算: {cfg.download_timeout_seconds} / {cfg.asr_timeout_seconds} 秒\n"
        f"🛟 备用语音转写: {'已配置' if cfg.fallback_asr_provider_id else '关闭'}\n"
        f"🗄  视频信息缓存: {video_info_cache_size()} 条\n"
        f"⏳ 用户冷却窗口: {cfg.user_cooldown_seconds} 秒\n"
        f"🖼 图片输出: {'on' if cfg.output_image else 'off'}  / "
        f" 自动分图: {'on' if cfg.enable_auto_split else 'off'}\n"
        f"💬 合并转发: {'on' if cfg.enable_forward_message else 'off'}\n"
        f"{render_diag_lines}\n"
    )
    yield event.plain_result(body)  # type: ignore[attr-defined]


async def handle_clear_cache(services: BiliVideoServices, event: object) -> AsyncIterator[object]:
    before = video_info_cache_size()
    await clear_video_info_cache()
    await clear_wbi_cache()
    await services.orchestrator.clear_cache()
    yield event.plain_result(  # type: ignore[attr-defined]
        f"🧹 已清除缓存(视频信息 {before} 条 + WBI 密钥 + 总结结果)"
    )
