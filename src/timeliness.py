"""Консервативная проверка анонсов на дату выпуска, отдельно от даты поста."""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from .excel_loader import parse_post_date

MONTHS = {name: i for i, name in enumerate((
    "января", "февраля", "марта", "апреля", "мая", "июня", "июля",
    "августа", "сентября", "октября", "ноября", "декабря",
), 1)}
DATE_RE = re.compile(
    r"(?<!\d)(?:(?P<iso>20\d{2}-\d{2}-\d{2})|"
    r"(?P<day>\d{1,2})(?:\s+(?P<month>" + "|".join(MONTHS) + r")|"
    r"[./](?P<month_num>\d{1,2}))(?:[. /]+(?P<year>20\d{2}))?)(?!\d)",
    re.IGNORECASE,
)
ANNOUNCEMENT_RE = re.compile(r"приглаша|регистрац|запис(?:аться|ывай)|прием заявок|пройдет|состоится|опрос", re.I)
RECAP_RE = re.compile(r"\b(?:прошел|прошла|прошли|состоялся|состоялась|состоялись|сыграли)\b", re.I)
DEADLINE_RE = re.compile(r"(?:регистрац\w*|заявк\w*|запис\w*|опрос\w*).{0,65}\bдо\s*$", re.I)
EVENT_RE = re.compile(r"приглаша|пройдет|состоится|ждем.{0,40}(?:на|в)", re.I)


def _dates(text: str, reference: date):
    for match in DATE_RE.finditer(text):
        if match.group("iso"):
            parsed = parse_post_date(match.group("iso"))
        else:
            month = MONTHS.get((match.group("month") or "").lower()) or int(match.group("month_num"))
            year = int(match.group("year")) if match.group("year") else reference.year
            if not match.group("year"):
                if month - reference.month < -6:
                    year += 1
                elif month - reference.month > 6:
                    year -= 1
            try:
                parsed = date(year, month, int(match.group("day")))
            except ValueError:
                continue
        if parsed:
            yield parsed, match


def assess_timeliness(post: dict[str, Any], digest_date: date | str | None) -> dict[str, Any]:
    """Не превращает прошедший анонс в отчет. Не удаляет итоги события.

    Для неоднозначных/относительных дат сохраняет материал с предупреждением.
    Исключение возможно только по дате, прочитанной непосредственно из текста.
    """
    publication = parse_post_date(digest_date)
    if not publication:
        return {"status": "not_checked", "reason": "Дата выпуска не указана"}
    text = str(post.get("text") or "").replace("ё", "е")
    if not ANNOUNCEMENT_RE.search(text):
        return {"status": "current", "reason": ""}
    reference = parse_post_date(post.get("date")) or publication
    dated = list(_dates(text, reference))
    # Смешанная публикация (итоги + следующий анонс) требует смысловой проверки.
    # Будущая дата в том же посте не должна потеряться из-за прошедшей встречи.
    if RECAP_RE.search(text):
        return {"status": "review", "reason": "В посте есть итоги и приглашение; проверьте, к какой встрече относится призыв"}
    deadlines, events = [], []
    for value, match in dated:
        before = text[max(0, match.start() - 100):match.start()].split("\n")[-1]
        after = text[match.end():match.end() + 90].split("\n")[0]
        if DEADLINE_RE.search(before):
            deadlines.append(value)
        elif EVENT_RE.search(before + after) and not re.search(r"\bс\s*$", before):
            events.append(value)
    if events and max(events) < publication:
        return {"status": "expired", "reason": f"Срок анонса прошел: {max(events):%d.%m.%Y}; выпуск {publication:%d.%m.%Y}"}
    if deadlines and max(deadlines) < publication:
        return {"status": "expired", "reason": f"Прием заявок или опрос завершен {max(deadlines):%d.%m.%Y}"}
    if not events and not deadlines:
        return {"status": "review", "reason": "Не удалось однозначно определить срок приглашения; проверьте актуальность"}
    return {"status": "current", "reason": ""}


def source_calendar_dates(post: dict[str, Any]) -> set[date]:
    reference = parse_post_date(post.get("date"))
    if not reference:
        return set()
    return {value for value, _ in _dates(str(post.get("text") or ""), reference)}
