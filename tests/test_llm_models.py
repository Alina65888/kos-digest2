"""Проверяем реальные HTTP-тела SDK без сетевых вызовов и рабочего ключа."""
import json

import pytest
from openai import OpenAI

try:
    import httpx
except ModuleNotFoundError:
    import httpx2 as httpx  # Новые версии SDK используют HTTPX2.

from src import llm_client as llm


@pytest.fixture
def api(monkeypatch, tmp_path):
    settings = {"OPENAI_MODEL": "gpt-6-astra", "OPENAI_REASONING_EFFORT": "high"}
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
            "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({"title": "Новый заголовок"}),
            }}],
        })

    client = OpenAI(api_key="test-key", base_url="https://api.example.test/v1",
                    http_client=httpx.Client(transport=httpx.MockTransport(respond)),
                    max_retries=0)
    monkeypatch.setattr(llm, "_get_secret", lambda key, default="": settings.get(key, default))
    monkeypatch.setattr(llm, "_get_client", lambda: client)
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("LLM_CACHE_ENABLED", "1")
    yield settings, requests
    client.close()


@pytest.mark.parametrize("model,effort,with_temperature", [
    ("gpt-6-astra", "high", False),
    ("gpt-6-astra-2026-09-01", "medium", False),
    ("gpt-6-sol", "high", False),
    ("gpt-6-sol", "none", True),
    ("gpt-6-luna", "none", True),
    ("gpt-4.1", None, True),
    ("gpt-4o", None, True),
])
def test_request_is_compatible_and_keeps_json_contract(api, model, effort, with_temperature):
    settings, requests = api
    settings["OPENAI_MODEL"] = model
    # Старую модель можно вернуть, оставив уровень рассуждения в Secrets.
    settings["OPENAI_REASONING_EFFORT"] = effort or "high"
    result = llm.llm_json("Верни JSON.", "Новость", temperature=0.45, use_cache=False)
    assert result == {"title": "Новый заголовок"}
    assert len(requests) == 1
    body = requests[0]
    assert body["model"] == model
    assert body.get("reasoning_effort") == effort
    assert ("temperature" in body) is with_temperature
    if with_temperature:
        assert body["temperature"] == 0.45
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [
        {"role": "system", "content": "Верни JSON."},
        {"role": "user", "content": "Новость"},
    ]


@pytest.mark.parametrize("effort", ["none", "ultra", "maximum"])
def test_bad_astra_effort_stops_before_http_request(api, effort):
    settings, requests = api
    settings["OPENAI_REASONING_EFFORT"] = effort
    with pytest.raises(ValueError, match="OPENAI_REASONING_EFFORT"):
        llm.llm_json("Верни JSON.", "Новость")
    assert requests == []


def test_reasoning_default_and_cache_isolation(api):
    settings, requests = api
    del settings["OPENAI_REASONING_EFFORT"]
    llm.llm_json("Верни JSON.", "Новость")
    assert requests[0]["reasoning_effort"] == "high"
    llm.llm_json("Верни JSON.", "Новость", temperature=0.8)
    assert len(requests) == 1  # Неприменяемый temperature не меняет запрос.
    settings["OPENAI_REASONING_EFFORT"] = "medium"
    llm.llm_json("Верни JSON.", "Новость")
    assert len(requests) == 2
    assert requests[-1]["reasoning_effort"] == "medium"
    settings["OPENAI_BASE_URL"] = "https://other-api.example.test/v1"
    llm.llm_json("Верни JSON.", "Новость")
    assert len(requests) == 3
    settings["OPENAI_MODEL"] = "gpt-4.1"
    llm.llm_json("Верни JSON.", "Новость")
    assert len(requests) == 4
    assert "reasoning_effort" not in requests[-1]


def test_editor_displays_configured_model(api):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=20)
    app.session_state["digest_mode"] = "kos"
    app.run()
    assert not app.exception
    assert any("gpt-6-astra · рассуждение: high" in item.value for item in app.caption)
    api[0]["OPENAI_REASONING_EFFORT"] = "none"
    app.run()
    assert not app.exception
    assert any("OPENAI_REASONING_EFFORT" in item.value for item in app.error)
    assert next(b for b in app.button if b.label == "Сгенерировать дайджест").disabled
