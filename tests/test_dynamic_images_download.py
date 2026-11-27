"""Public dynamic image downloads use a bounded, anonymous client."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import aiohttp
import pytest

from bilivideo.api import dynamic_images
from bilivideo.api.dynamic_images import DynamicImageDownloadError, download_dynamic_images

_FIRST = "https://i0.hdslb.com/bfs/new_dyn/mock-first.png"
_SECOND = "https://i1.hdslb.com/bfs/album/mock-second.jpg"


def _response(
    *chunks: bytes,
    status: int = 200,
    content_type: str = "image/png",
    content_length: int | None = None,
    content_encoding: str = "identity",
) -> MagicMock:
    async def iter_chunked(_size: int):
        for chunk in chunks:
            yield chunk

    response = MagicMock(
        status=status,
        content_type=content_type,
        content_length=content_length,
        headers={"Content-Encoding": content_encoding},
    )
    response.content.iter_chunked.side_effect = iter_chunked
    context = MagicMock()
    context.__aenter__.return_value = response
    return context


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = MagicMock()
    monkeypatch.setattr(dynamic_images.aiohttp, "ClientSession", factory)
    return factory


async def test_downloads_in_order_without_credentials_or_redirects(client: MagicMock) -> None:
    session = client.return_value.__aenter__.return_value
    session.get.side_effect = [_response(b"X", b"Y", content_length=2), _response(b"Z")]

    assert await download_dynamic_images(("//i0.hdslb.com/bfs/new_dyn/mock-first.png", _SECOND)) == [
        b"XY",
        b"Z",
    ]

    assert isinstance(client.call_args.kwargs["cookie_jar"], aiohttp.DummyCookieJar)
    assert client.call_args.kwargs["trust_env"] is False
    assert client.call_args.kwargs["auto_decompress"] is False
    assert client.call_args.kwargs["timeout"].total == 8
    assert client.call_args.kwargs["headers"] == {"Accept-Encoding": "identity"}
    assert session.get.call_args_list == [
        ((_FIRST,), {"allow_redirects": False, "ssl": True}),
        ((_SECOND,), {"allow_redirects": False, "ssl": True}),
    ]


async def test_validates_all_urls_before_opening_session(client: MagicMock) -> None:
    for urls, reason in (
        ((_FIRST, "https://example.com/mock.png"), "invalid_url"),
        ((_FIRST, "https://i0.hdslb.com/bfs/new_dyn/mock.png?sample=value"), "invalid_url"),
        ((_FIRST,) * 10, "url_limit"),
    ):
        with pytest.raises(DynamicImageDownloadError, match=reason):
            await download_dynamic_images(urls)

    assert await download_dynamic_images(()) == []
    client.assert_not_called()


@pytest.mark.parametrize(
    ("options", "reason", "status"),
    [
        ({"status": 302}, "http_status", 302),
        ({"status": 404}, "http_status", 404),
        ({"content_type": "text/html"}, "content_type", None),
        ({"content_encoding": "gzip"}, "content_encoding", None),
        ({}, "empty", None),
    ],
)
async def test_declines_unsuitable_responses(
    client: MagicMock, options: dict, reason: str, status: int | None
) -> None:
    session = client.return_value.__aenter__.return_value
    session.get.return_value = _response(**options)

    with pytest.raises(DynamicImageDownloadError) as error:
        await download_dynamic_images((_FIRST,))

    assert error.value.reason == reason
    assert error.value.status == status
    session.get.assert_called_once()


@pytest.mark.parametrize("content_length", [None, 3])
async def test_caps_individual_image_with_or_without_content_length(
    client: MagicMock, monkeypatch: pytest.MonkeyPatch, content_length: int | None
) -> None:
    monkeypatch.setattr(dynamic_images, "_IMAGE_MAX_BYTES", 2)
    session = client.return_value.__aenter__.return_value
    response = _response(b"XY", b"Z", content_length=content_length)
    session.get.return_value = response

    with pytest.raises(DynamicImageDownloadError, match="size_limit"):
        await download_dynamic_images((_FIRST,))

    if content_length is not None:
        response.__aenter__.return_value.content.iter_chunked.assert_not_called()
    response.__aexit__.assert_awaited_once()


async def test_caps_total_batch_across_images(client: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dynamic_images, "_BATCH_MAX_BYTES", 3)
    session = client.return_value.__aenter__.return_value
    session.get.side_effect = [_response(b"XY"), _response(b"ZZ")]

    with pytest.raises(DynamicImageDownloadError, match="size_limit"):
        await download_dynamic_images((_FIRST, _SECOND))


async def test_batch_deadline_closes_session(client: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dynamic_images, "_BATCH_TIMEOUT_SECONDS", 0.01)
    session = client.return_value.__aenter__.return_value
    session.get.return_value = _response()

    async def stalled_response():
        await asyncio.Event().wait()

    session.get.return_value.__aenter__.side_effect = stalled_response

    with pytest.raises(DynamicImageDownloadError, match="timeout"):
        await download_dynamic_images((_FIRST,))

    client.return_value.__aexit__.assert_awaited_once()


async def test_network_error_discards_original_diagnostic(client: MagicMock) -> None:
    session = client.return_value.__aenter__.return_value
    session.get.side_effect = aiohttp.ClientConnectionError("fake-private-details")

    with pytest.raises(DynamicImageDownloadError, match="ClientConnectionError") as error:
        await download_dynamic_images((_FIRST,))

    assert "fake-private-details" not in str(error.value)
    assert error.value.__suppress_context__ is True
