"""Opt-in fallback using an already configured AstrBot speech provider.

The provider ID must be explicitly selected. No default provider, credentials
or new endpoint is inferred; get_text may transmit audio and incur provider cost.
"""

from __future__ import annotations

from ..core.exceptions import TranscriptionError
from ..core.types import TranscriptResult, TranscriptSegment


class AstrBotSTTFallback:
    def __init__(self, context: object, provider_id: str) -> None:
        self._context = context
        self._provider_id = provider_id

    async def transcribe(self, file_path: str, *, duration: float) -> TranscriptResult:
        try:
            providers = self._context.get_all_stt_providers()
            provider = self._context.get_provider_by_id(self._provider_id)
            # Do not accidentally send audio to a chat/TTS provider sharing
            # a similar interface or choose another provider on ID mismatch.
            if provider is None or not any(provider is item for item in providers):
                raise TranscriptionError("fallback ASR provider missing or not a speech-to-text provider")
            text = await provider.get_text(file_path)
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(
                f"AstrBot fallback ASR failed category={type(exc).__name__}"
            ) from None
        if not isinstance(text, str) or not text.strip():
            raise TranscriptionError("AstrBot fallback ASR returned empty text")
        # This API returns plain text, not timestamps. Never invent detailed
        # timestamps; a single whole-audio segment keeps the contract honest.
        return TranscriptResult(
            language="und",
            full_text=text.strip(),
            segments=(TranscriptSegment(start=0, end=max(0, duration), text=text.strip()),),
            raw={"source": "astrbot_stt", "timestamps": False},
        )
