"""
Обёртка над OpenAI-совместимым LLM с тремя улучшениями:
  - Retry при ошибках сети/парсинга (до 3 попыток с экспоненциальным бэкоффом).
  - Локальный файловый кэш (sha1 от промптов → JSON-ответ).
    Повторный запуск на тех же данных стоит 0 копеек и занимает 0 секунд.
  - Унифицированный парсинг JSON-ответа (снимает обёртки ```json...```).

Все функции пайплайна должны звонить через эту обёртку, не напрямую в OpenAI.
"""
import os
import re
import json
import time
import hashlib
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from openai import OpenAI
from dotenv import load_dotenv

from .config import CACHE_DIR, LLM_MAX_RETRIES

load_dotenv()
log = logging.getLogger(__name__)


# === Клиент OpenAI ===

def _get_secret(key: str, default: str = "") -> str:
    """Читает секрет из st.secrets (Streamlit Cloud) или из переменных окружения (.env)."""
    try:
        import streamlit as st
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)


def _get_client() -> OpenAI:
    """Создаёт клиента, читая ключ и base_url из st.secrets или .env"""
    api_key = _get_secret("OPENAI_API_KEY")
    if not api_key or api_key.startswith("sk-замените"):
        raise RuntimeError(
            "API-ключ не задан. Укажите OPENAI_API_KEY в Secrets (Streamlit Cloud) "
            "или в .env (локально)."
        )
    return OpenAI(
        api_key=api_key,
        base_url=_get_secret("OPENAI_BASE_URL") or "https://api.openai.com/v1",
    )


def _get_model() -> str:
    return _get_secret("OPENAI_MODEL") or "gpt-4o"


# === Кэш ===

def _cache_key(system_prompt: str, user_payload: str, temperature: float) -> str:
    """sha1 от всех ингредиентов запроса — детерминированный ключ кэша"""
    raw = f"{_get_model()}|{temperature}|{system_prompt}|{user_payload}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    if not os.getenv("LLM_CACHE_ENABLED", "1") == "1":
        return None
    path = Path(CACHE_DIR) / f"{key}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _cache_set(key: str, value: Dict[str, Any]) -> None:
    if not os.getenv("LLM_CACHE_ENABLED", "1") == "1":
        return
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    path = Path(CACHE_DIR) / f"{key}.json"
    try:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning(f"Не удалось записать кэш {key}: {e}")


def clear_cache() -> int:
    """Очистка кэша. Возвращает количество удалённых файлов."""
    cache = Path(CACHE_DIR)
    if not cache.exists():
        return 0
    count = 0
    for f in cache.glob("*.json"):
        f.unlink()
        count += 1
    return count


# === Парсинг JSON-ответа ===

def _clean_json(raw: str) -> str:
    """Снимает обёртки ```json...``` и обрезает мусор по краям"""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw


# === Главная функция ===

def llm_json(
    system_prompt: str,
    user_payload: str,
    temperature: float = 0.4,
    use_cache: bool = True,
    label: str = "",
) -> Dict[str, Any]:
    """
    Вызывает LLM, возвращает распарсенный JSON.
    Делает retry при сбоях. Кэширует успешные ответы.

    label — короткая метка для логов («classify», «plan», «rewrite»).
    """
    cache_key = _cache_key(system_prompt, user_payload, temperature)

    # Попытка взять из кэша
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            log.info(f"[{label}] cache HIT")
            return cached

    # Реальный вызов
    client = _get_client()
    model = _get_model()
    last_error = None

    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_payload},
                ],
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content
            cleaned = _clean_json(raw)
            result = json.loads(cleaned)

            # Успех — записываем в кэш и возвращаем
            if use_cache:
                _cache_set(cache_key, result)
            log.info(f"[{label}] success on attempt {attempt}")
            return result

        except json.JSONDecodeError as e:
            last_error = e
            log.warning(f"[{label}] попытка {attempt}: невалидный JSON: {e}")
        except Exception as e:
            last_error = e
            log.warning(f"[{label}] попытка {attempt}: {type(e).__name__}: {e}")

        # Экспоненциальный бэкофф (начинаем с 6с для 429 rate-limit)
        if attempt < LLM_MAX_RETRIES:
            sleep_s = 6 * attempt
            time.sleep(sleep_s)

    raise RuntimeError(
        f"LLM не ответил после {LLM_MAX_RETRIES} попыток.\n"
        f"Последняя ошибка: {last_error}\n\n"
        "Проверьте:\n"
        "- Правильность API-ключа (OPENAI_API_KEY)\n"
        "- Доступность API-сервера (OPENAI_BASE_URL)\n"
        "- Баланс на аккаунте посредника"
    )


def load_prompt(name: str) -> str:
    """Читает системный промпт из prompts/{name}.txt"""
    from .config import PROMPTS_DIR
    return (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
