"""Empty model responses must be retried instead of written out as a note."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.srt_daemon.ai_client import AiClient, AiClientError, RetryableAiError
from src.srt_daemon.config import Config


@pytest.fixture
def client_config(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-empty")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("## System Prompt\nYou are a test.\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    data = {
        "ai": {
            "model": "deepseek-chat",
            "system_prompt_file": str(prompt),
            "max_retries": 2,
            "retry_backoff_base": 0.0,
        },
        "logging": {"file": str(logs / "t.log")},
        "watch": {"directory": str(tmp_path / "watch")},
        "output": {"directory": str(tmp_path / "out")},
    }
    (tmp_path / "watch").mkdir()
    (tmp_path / "out").mkdir()
    return Config(data, tmp_path)


def _response(content: str, finish_reason: str = "stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(completion_tokens=0),
    )


class TestEmptyResponse:
    def test_empty_content_is_an_error(self, client_config: Config) -> None:
        client = AiClient(client_config)
        client._client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_: _response("   "))
            )
        )

        with pytest.raises(RetryableAiError):
            client._call_api("system", "user")

    def test_retry_then_success(self, client_config: Config) -> None:
        client = AiClient(client_config)
        calls = {"n": 0}

        def fake_call_api(system, user, overrides=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RetryableAiError("empty response")
            return "# Notes\n\nbody"

        with patch.object(AiClient, "_call_api", side_effect=fake_call_api):
            result = client._call_api_with_retry("system", "user")

        assert result.startswith("# Notes")
        assert calls["n"] == 2

    def test_all_empty_raises_after_retries(self, client_config: Config) -> None:
        client = AiClient(client_config)

        with patch.object(
            AiClient, "_call_api", side_effect=RetryableAiError("empty response")
        ) as call:
            with pytest.raises(AiClientError):
                client._call_api_with_retry("system", "user")

        assert call.call_count == client_config.ai_max_retries + 1

    def test_empty_classification_falls_back(self, client_config: Config) -> None:
        from src.srt_daemon.profiles import ProfileRegistry

        client = AiClient(client_config)
        registry = ProfileRegistry(client_config)

        with patch.object(AiClient, "_call_api", return_value="   "):
            # The classifier swallows the error and returns nothing, so the
            # caller keeps the default profile.
            assert client.classify_profile(registry.all(), "some transcript") is None
