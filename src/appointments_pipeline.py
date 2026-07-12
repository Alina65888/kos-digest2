"""
Пайплайн дайджеста кадровых назначений.

В отличие от src/pipeline.py (classify → plan → rewrite для новостей),
здесь раздел человека задаётся вручную в Excel (колонка section), а LLM
только дописывает био/поздравление по коротким заметкам HR — один вызов
на человека, параллельно, через тот же llm_client.
"""
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Callable

from .config import (
    APPOINTMENT_SECTIONS,
    APPOINTMENT_BIO_SECTIONS,
    APPOINTMENT_CONGRATS_SECTION,
    APPOINTMENT_DEPARTED_SECTION,
    APPOINTMENT_CLOSING_NOTE,
    LLM_PARALLEL_WORKERS,
    REWRITE_TEMPERATURE,
)
from .llm_client import llm_json, load_prompt

log = logging.getLogger(__name__)

PROMPT_NAME = "appointments_bio"


def _needs_llm(person: Dict[str, Any]) -> bool:
    section = person["section"]
    if section in APPOINTMENT_BIO_SECTIONS or section == APPOINTMENT_CONGRATS_SECTION:
        return True
    if section == APPOINTMENT_DEPARTED_SECTION:
        return bool(person.get("notes") or person.get("is_retirement"))
    return False


def write_bio(person: Dict[str, Any], _seed: Optional[int] = None) -> Dict[str, Any]:
    """Один вызов LLM на человека. _seed ломает кэш при перегенерации."""
    system_prompt = load_prompt(PROMPT_NAME)
    payload = {
        "person": {
            "name": person.get("name", ""),
            "new_position": person.get("new_position", ""),
            "previous_position": person.get("previous_position", ""),
            "notes": person.get("notes", ""),
            "section": person.get("section", ""),
        }
    }
    if _seed is not None:
        payload["_seed"] = _seed
    return llm_json(
        system_prompt,
        json.dumps(payload, ensure_ascii=False),
        temperature=REWRITE_TEMPERATURE,
        label=f"appointments[{person.get('section')}]",
    )


def build_appointments_draft(
    people: List[Dict[str, Any]],
    period_label: str = "",
    recipient_label: str = "",
    progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """
    Полный пайплайн дайджеста назначений. Возвращает draft, готовый к рендерингу.
    """
    warnings: List[str] = []

    # Группируем по разделам в каноническом порядке, сохраняя order/порядок строк
    grouped: Dict[str, List[Dict[str, Any]]] = {s["key"]: [] for s in APPOINTMENT_SECTIONS}
    for p in people:
        grouped.setdefault(p["section"], []).append(p)
    for key in grouped:
        grouped[key].sort(key=lambda p: p.get("order", 0))

    tasks = [p for p in people if _needs_llm(p)]

    def _process(person):
        try:
            result = write_bio(person)
            return person, result, None
        except Exception as e:
            return person, None, str(e)

    if tasks:
        if progress:
            progress(0, len(tasks), f"Пишем био 0/{len(tasks)}")
        with ThreadPoolExecutor(max_workers=min(LLM_PARALLEL_WORKERS, len(tasks))) as ex:
            futures = [ex.submit(_process, p) for p in tasks]
            done = 0
            for f in as_completed(futures):
                person, result, err = f.result()
                done += 1
                if progress:
                    progress(done, len(tasks), f"Пишем био {done}/{len(tasks)}")
                if err:
                    warnings.append(f"{person.get('name')}: не удалось сгенерировать текст ({err})")
                    result = {}
                person["education"] = result.get("education", "")
                person["career"] = result.get("career", "")
                person["message"] = result.get("message", "")

    # Люди без LLM-вызова — пустые текстовые поля по умолчанию
    for p in people:
        p.setdefault("education", "")
        p.setdefault("career", "")
        p.setdefault("message", "")

    sections = []
    for s in APPOINTMENT_SECTIONS:
        key = s["key"]
        section_people = grouped.get(key, [])
        section_obj = {
            "key": key,
            "title": s["title"],
            "people": section_people,
        }
        if key == APPOINTMENT_DEPARTED_SECTION and section_people:
            section_obj["closing_note"] = APPOINTMENT_CLOSING_NOTE
        sections.append(section_obj)

    return {
        "period_label": period_label,
        "recipient_label": recipient_label,
        "sections": sections,
        "org_chart_link": "",
        "contact_email": "",
        "warnings": warnings,
        "_stats": {
            "total_people": len(people),
            "per_section": {s["key"]: len(grouped.get(s["key"], [])) for s in APPOINTMENT_SECTIONS},
        },
    }


def regenerate_single_person(
    draft: Dict[str, Any],
    section_key: str,
    person_idx: int,
) -> Dict[str, Any]:
    """Перегенерирует текст одного человека, не трогая остальных."""
    section = next((s for s in draft["sections"] if s["key"] == section_key), None)
    if not section:
        raise ValueError(f"Раздел '{section_key}' не найден")
    person = section["people"][person_idx]

    result = write_bio(person, _seed=int(time.time()))
    person["education"] = result.get("education", person.get("education", ""))
    person["career"] = result.get("career", person.get("career", ""))
    person["message"] = result.get("message", person.get("message", ""))
    return person
