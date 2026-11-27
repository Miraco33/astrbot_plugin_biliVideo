"""Fetch public dynamic images without sharing the Bilibili API session."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from http import HTTPStatus

import aiohttp

from ..core.exceptions import BiliVideoError
from .endpoints import DYNAMIC_IMAGE_LIMIT, normalize_dynamic_image_url

_IMAGE_MAX_BYTES = 8 * 1024 * 1024
_BATCH_MAX_BYTES = 24 * 1024 * 1024
_IMAGE_TIMEOUT_SECONDS = 8
_BATCH_TIMEOUT_SECONDS = 25
_CHUNK_BYTES = 64 * 1024


class DynamicImageDownloadError(BiliVideoError):
    """A download failure with diagnostics safe to log."""

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        super().__init__(f"动态图片下载失败 ({reason})")
        self.reason = reason
        self.status = status


async def _download_batch(image_urls: Sequence[str]) -> list[bytes]:
    images: list[bytes] = []
    batch_remaining = _BATCH_MAX_BYTES
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.DummyCookieJar(),
        timeout=aiohttp.ClientTimeout(total=_IMAGE_TIMEOUT_SECONDS),
        trust_env=False,
        auto_decompress=False,
        headers={"Accept-Encoding": "identity"},
    ) as session:
        for url in image_urls:
            async with session.get(url, allow_redirects=False, ssl=True) as response:
                if response.status != HTTPStatus.OK:
                    raise DynamicImageDownloadError("http_status", status=response.status)
                if not response.content_type.startswith("image/"):
                    raise DynamicImageDownloadError("content_type")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise DynamicImageDownloadError("content_encoding")

                byte_limit = min(_IMAGE_MAX_BYTES, batch_remaining)
                if response.content_length is not None and response.content_length > byte_limit:
                    raise DynamicImageDownloadError("size_limit")

                data = bytearray()
                async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
                    if len(data) + len(chunk) > byte_limit:
                        raise DynamicImageDownloadError("size_limit")
                    data.extend(chunk)

                if not data:
                    raise DynamicImageDownloadError("empty")
                batch_remaining -= len(data)
                images.append(bytes(data))
    return images


async def download_dynamic_images(image_urls: Sequence[str]) -> list[bytes]:
    """Download a complete batch of public images in post order."""
    if not image_urls:
        return []
    if isinstance(image_urls, str) or len(image_urls) > DYNAMIC_IMAGE_LIMIT:
        raise DynamicImageDownloadError("url_limit")
    urls = [normalize_dynamic_image_url(url) for url in image_urls]
    if not all(urls):
        raise DynamicImageDownloadError("invalid_url")

    try:
        return await asyncio.wait_for(_download_batch(urls), timeout=_BATCH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise DynamicImageDownloadError("timeout") from None
    except (aiohttp.ClientError, OSError) as exc:
        raise DynamicImageDownloadError(type(exc).__name__) from None
