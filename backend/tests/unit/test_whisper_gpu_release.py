from pathlib import Path
from types import SimpleNamespace

import pytest

from src.media import transcription


class FakeModel:
    def __init__(self, device_type, fail=False):
        self.device = SimpleNamespace(type=device_type)
        self.fail = fail

    def transcribe(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("decode failed")
        return {"text": "hello", "segments": [], "language": "en"}


@pytest.fixture
def whisper_with(monkeypatch):
    def install(model):
        monkeypatch.setattr(transcription, "_WHISPER_AVAILABLE", True)
        monkeypatch.setattr(transcription, "_whisper", SimpleNamespace(load_model=lambda name: model))
        monkeypatch.setattr(transcription, "_prepare_audio_for_transcription", lambda path: path)
        monkeypatch.setattr(transcription, "_WHISPER_MODEL_CACHE", {})
        return transcription._WHISPER_MODEL_CACHE

    return install


def test_gpu_model_is_released_after_transcription(whisper_with):
    cache = whisper_with(FakeModel("cuda"))

    result = transcription.transcribe_with_whisper(Path("talk.mp4"), "large-v3")

    assert result["text"] == "hello"
    assert "large-v3" not in cache


def test_gpu_model_is_released_when_transcription_fails(whisper_with):
    cache = whisper_with(FakeModel("cuda", fail=True))

    with pytest.raises(RuntimeError):
        transcription.transcribe_with_whisper(Path("talk.mp4"), "large-v3")

    assert "large-v3" not in cache


def test_cpu_model_stays_cached(whisper_with):
    model = FakeModel("cpu")
    cache = whisper_with(model)

    transcription.transcribe_with_whisper(Path("talk.mp4"), "small")

    assert cache["small"] is model
