"""
Пайплайн дайджеста кадровых назначений.

В отличие от src/pipeline.py (classify → plan → rewrite для новостей),
здесь раздел человека задаётся вручную в Excel (колонка section), а LLM
дописывает тексты по заметкам HR. Пожелания создаются последовательно,
с учетом других карточек выпуска и проверкой повторов.
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
from .appointment_wishes import (BRIEFS, plain, split_message, compose_message,
                                  wish_problem, reserve_wish)

log = logging.getLogger(__name__)

PROMPT_NAME = "appointments_bio"


def _needs_llm(person: Dict[str, Any]) -> bool:
    section = person["section"]
    if section in APPOINTMENT_BIO_SECTIONS or section == APPOINTMENT_CONGRATS_SECTION:
        return True
    if section == APPOINTMENT_DEPARTED_SECTION:
        return bool(person.get("notes") or person.get("is_retirement"))
    return False


def write_bio(person: Dict[str, Any], _seed: Optional[int] = None, editorial_context=None) -> Dict[str, Any]:
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
    if editorial_context is not None:
        payload["editorial_context"] = editorial_context
    if _seed is not None:
        payload["_seed"] = _seed
    return llm_json(
        system_prompt,
        json.dumps(payload, ensure_ascii=False),
        temperature=REWRITE_TEMPERATURE,
        label=f"appointments[{person.get('section')}]",
    )


def write_congratulation(person, used_wishes, warnings, fixed_transition=None, seed=None):
    """До двух вариантов от LLM; затем неповторяющийся резерв с предупреждением."""
    context = {"used_wishes": used_wishes, "brief": BRIEFS[len(used_wishes) % len(BRIEFS)]}
    if fixed_transition is not None:
        context["fixed_transition"] = fixed_transition
    transition = fixed_transition or ""
    reason = ""
    for attempt in range(2):
        try:
            result = write_bio(person, _seed=seed, editorial_context=context)
            if not isinstance(result, dict):
                raise ValueError("Ответ должен быть JSON-объектом")
            if fixed_transition is None:
                transition = plain(result.get("transition", ""))
            wish = plain(result.get("wish", ""))
            reason = wish_problem(wish, used_wishes)
            if not reason:
                return compose_message(transition, wish)
            context.update(previous_wish=wish, revision_reason=reason)
        except Exception as error:
            reason = str(error)
            break
    wish = reserve_wish(used_wishes)
    note = "использовано резервное пожелание, проверьте перед отправкой" if wish else "добавьте пожелание вручную: свободные резервные варианты закончились"
    warnings.append(f"{person.get('name')}: {note} ({reason})")
    return compose_message(transition, wish)


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

    tasks = [p for p in people if _needs_llm(p) and p["section"] != APPOINTMENT_CONGRATS_SECTION]
    congratulations = grouped.get(APPOINTMENT_CONGRATS_SECTION, [])
    total = len(tasks) + len(congratulations)
    done = 0

    def _process(person):
        try:
            result = write_bio(person)
            return person, result, None
        except Exception as e:
            return person, None, str(e)

    if tasks:
        if progress:
            progress(0, total, f"Пишем тексты 0/{total}")
        with ThreadPoolExecutor(max_workers=min(LLM_PARALLEL_WORKERS, len(tasks))) as ex:
            futures = [ex.submit(_process, p) for p in tasks]
            for f in as_completed(futures):
                person, result, err = f.result()
                done += 1
                if progress:
                    progress(done, total, f"Пишем тексты {done}/{total}")
                if err:
                    warnings.append(f"{person.get('name')}: не удалось сгенерировать текст ({err})")
                    result = {}
                person["education"] = result.get("education", "")
                person["career"] = result.get("career", "")
                person["message"] = result.get("message", "")

    used_wishes = []
    for person in congratulations:
        person["message"] = write_congratulation(person, used_wishes, warnings)
        wish = split_message(person["message"])[1]
        if wish:
            used_wishes.append(wish)
        done += 1
        if progress:
            progress(done, total, f"Пишем тексты {done}/{total}")

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

    if section_key == APPOINTMENT_CONGRATS_SECTION:
        transition, previous = split_message(person.get("message", ""))
        used = [split_message(p.get("message", ""))[1]
                for sec in draft["sections"] if sec["key"] == APPOINTMENT_CONGRATS_SECTION
                for p in sec.get("people", []) if p is not person and p.get("message")]
        if previous:
            used.append(previous)
        person["message"] = write_congratulation(
            person, used, draft.setdefault("warnings", []),
            fixed_transition=transition, seed=time.time_ns())
        return person

    result = write_bio(person, _seed=time.time_ns())
    person["education"] = result.get("education", person.get("education", ""))
    person["career"] = result.get("career", person.get("career", ""))
    person["message"] = result.get("message", person.get("message", ""))
    return person
