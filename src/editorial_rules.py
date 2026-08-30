"""Детерминированные редакторские правила поверх ответов LLM.

Модель предлагает рубрику и формулировки, но не может отменить базовые
ограничения: рубрика должна существовать, цифры и цитаты должны быть в
исходнике, а анонс не должен дублировать уже вышедший отчет.
"""
from __future__ import annotations

import html
import re
from typing import Any, Dict, Iterable, List, Tuple

from .config import (
    CANONICAL_RUBRIC_ORDER,
    CARD_TEXT_MIN_CHARS,
    CARD_TEXT_TARGET_CHARS,
    CARD_TEXT_MAX_CHARS,
    VIDEO_TEXT_MIN_CHARS,
    RICH_SOURCE_MIN_CHARS,
    HEADLINE_MIN_WORDS,
    HEADLINE_MAX_WORDS,
    MAIN_HEADLINE_MAX_WORDS,
    HEADLINE_CANDIDATE_COUNT,
)


RUBRIC_ALIASES = {
    "ПРОИЗВОДСТВО": "ПРОИЗВОДСТВО",
    "ПСС": "ПСС",
    "ПРОИЗВОДСТВЕННАЯ СИСТЕМА": "ПСС",
    "БЕЗОПАСНОСТЬ": "БЕЗОПАСНОСТЬ",
    "ЗАБОТА": "ЗАБОТА О ЛЮДЯХ",
    "ЗАБОТА О ЛЮДЯХ": "ЗАБОТА О ЛЮДЯХ",
    "КАРЬЕРА": "КАРЬЕРА",
    "СОБЫТИЯ": "СОБЫТИЯ",
    "ДОСТИЖЕНИЯ": "ДОСТИЖЕНИЯ",
    "ВЫ ПРОСИЛИ МЫ СДЕЛАЛИ": "ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ",
    "ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ": "ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ",
}

RUBRIC_KEYWORDS: Dict[str, List[Tuple[str, int]]] = {
    "ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ": [
        ("вы просили", 8), ("сотрудники просили", 8), ("по просьб", 7),
        ("по обращениям", 6), ("по обратной связи", 6),
        ("обновили после", 5), ("вопрос решен", 5),
    ],
    "ДОСТИЖЕНИЯ": [
        ("первое место", 7), ("второе место", 7), ("третье место", 7),
        ("занял первое", 7), ("заняла первое", 7), ("заняли первое", 7),
        ("победител", 6), ("призер", 6), ("призёр", 6),
        ("серебро", 6), ("золото", 6), ("бронза", 6),
        ("наград", 5), ("лауреат", 5), ("рекорд", 5),
    ],
    "ПСС": [
        ("мини-т", 8), ("мини-трансформац", 8),
        ("производственная система", 7), ("бережлив", 6),
        ("кпсц", 7), ("а-3", 7), ("а3", 6), ("5с", 7),
        ("фабрика процессов", 7), ("картирован", 5),
        ("устранение потерь", 5), ("узкие места", 4),
    ],
    "БЕЗОПАСНОСТЬ": [
        ("охрана труда", 7), ("промышленная безопасность", 7),
        ("культура безопасности", 7), ("стоп-карт", 7),
        ("требую остановки", 7), ("средства защиты", 6),
        ("сиз", 6), ("эколог", 4), ("выброс", 4),
        ("санитарно-защит", 5), ("замеры воздуха", 6),
    ],
    "ЗАБОТА О ЛЮДЯХ": [
        ("дмс", 8), ("согаз", 6), ("медицин", 4), ("здоров", 4),
        ("профсоюз", 6), ("ветеран", 6), ("благотвор", 6),
        ("волонтер", 6), ("волонтёр", 6), ("социального центра", 5),
        ("дети сотрудников", 5), ("питани", 4), ("столов", 4),
    ],
    "КАРЬЕРА": [
        ("егэ", 8), ("целевое обучение", 7), ("наставнич", 6),
        ("днк лидерства", 7), ("стажиров", 6), ("карьер", 5),
        ("обучен", 4), ("образовательн", 4), ("курс", 3),
        ("школ", 3), ("мгу", 3), ("книту", 3),
    ],
    "ПРОИЗВОДСТВО": [
        ("производств", 3), ("оборудован", 4), ("компрессор", 6),
        ("агрегат", 5), ("установк", 3), ("пэнп", 6),
        ("полиэтилен", 4), ("поликарбонат", 4), ("пиролиз", 5),
        ("этиленопровод", 6), ("ремонт", 4), ("технических газов", 6),
    ],
    "СОБЫТИЯ": [
        ("форум", 6), ("фестивал", 6), ("концерт", 6),
        ("спартакиад", 6), ("марафон", 5), ("турнир", 5),
        ("встреч", 4), ("мероприят", 4), ("анонс", 5),
        ("приглашаем", 4), ("сыграли", 4), ("игра", 3),
    ],
}

FORBIDDEN_CLICHES = (
    "это не просто", "это про ", "меняет правила игры", "не для галочки",
    "и это только начало", "главное – не останавливаться",
    "главное — не останавливаться", "впереди еще много работы",
    "впереди ещё много работы", "настоящий праздник",
    "незабываемые эмоции", "вместе мы можем больше",
)

# Заголовки из этого списка могут подойти почти к любой корпоративной новости.
# Они не становятся сильнее от КАПСА и не сообщают читателю, что произошло.
WEAK_HEADLINE_PHRASES = (
    "важный шаг", "новый уровень", "в центре внимания", "движение вперед",
    "движение вперёд", "шаг в будущее", "путь к успеху", "время перемен",
    "сила команды", "вместе к успеху", "работа продолжается",
    "итоги подведены", "курс на развитие", "новые горизонты",
    "больше чем", "больше, чем", "мы снова доказали", "все только начинается",
    "всё только начинается", "будущее начинается", "территория возможностей",
    "в фокусе", "главное о важном", "есть чем гордиться",
)

WEAK_HEADLINE_WORDS = {
    "важный", "важная", "важные", "уникальный", "уникальная", "яркий",
    "яркая", "успешный", "успешная", "эффективный", "эффективная",
    "масштабный", "масштабная", "значимый", "значимая", "новый", "новая",
}

ABSTRACT_HEADLINE_OPENERS = {
    "внедрение", "проведение", "реализация", "организация", "обновление",
    "развитие", "создание", "обучение", "подготовка", "обсуждение",
}

GENERIC_HEADLINE_NOUNS = {
    "команда", "команды", "сотрудники", "специалисты", "участники",
    "мероприятие", "проект", "работа", "работы", "производство",
}

HEADLINE_ACTION_ENDINGS = (
    "ли", "ла", "ло", "ет", "ют", "ит", "ат", "ят", "ен", "ена", "ены",
)

# Слова результата особенно опасны: одна смена времени превращает
# «планируют запустить» в ложное «запустили». Для таких формулировок одного
# лексического пересечения недостаточно – в источнике тоже нужен завершенный
# статус из той же группы.
HEADLINE_STATUS_GROUPS = (
    (
        {"запущен", "запущена", "запущено", "запущены", "запустили", "заработал", "заработала"},
        {"запущен", "запущена", "запущено", "запущены", "запустили", "заработал", "заработала"},
    ),
    (
        {"завершен", "завершена", "завершено", "завершены", "завершили", "закончили"},
        {"завершен", "завершена", "завершено", "завершены", "завершили", "закончили"},
    ),
    (
        {"внедрен", "внедрена", "внедрено", "внедрены", "внедрили"},
        {"внедрен", "внедрена", "внедрено", "внедрены", "внедрили"},
    ),
    (
        {"открыт", "открыта", "открыто", "открыты", "открыли"},
        {"открыт", "открыта", "открыто", "открыты", "открыли"},
    ),
    (
        {"исправлен", "исправлена", "исправлено", "исправлены", "исправили", "устранили"},
        {"исправлен", "исправлена", "исправлено", "исправлены", "исправили", "устранили"},
    ),
    (
        {"снизили", "снижен", "снижена", "снижено", "снижены"},
        {"снизили", "снижен", "снижена", "снижено", "снижены"},
    ),
    (
        {"повысили", "повышен", "повышена", "повышено", "повышены"},
        {"повысили", "повышен", "повышена", "повышено", "повышены"},
    ),
    (
        {"пройден", "пройдена", "пройдено", "пройдены", "прошел", "прошла", "прошло", "прошли"},
        {"пройден", "пройдена", "пройдено", "пройдены", "прошел", "прошла", "прошло", "прошли"},
    ),
    (
        {"заменили", "заменен", "заменена", "заменено", "заменены"},
        {"заменили", "заменен", "заменена", "заменено", "заменены"},
    ),
    (
        {"установили", "установлен", "установлена", "установлено", "установлены"},
        {"установили", "установлен", "установлена", "установлено", "установлены"},
    ),
    (
        {"получили", "получен", "получена", "получено", "получены"},
        {"получили", "получен", "получена", "получено", "получены"},
    ),
    (
        {"отремонтировали", "отремонтирован", "отремонтирована", "отремонтировано", "отремонтированы"},
        {"отремонтировали", "отремонтирован", "отремонтирована", "отремонтировано", "отремонтированы"},
    ),
    (
        {"провели", "проведен", "проведена", "проведено", "проведены"},
        {"провели", "проведен", "проведена", "проведено", "проведены"},
    ),
    (
        {"создали", "создан", "создана", "создано", "созданы"},
        {"создали", "создан", "создана", "создано", "созданы"},
    ),
    (
        {"разработали", "разработан", "разработана", "разработано", "разработаны"},
        {"разработали", "разработан", "разработана", "разработано", "разработаны"},
    ),
    (
        {"обновили", "обновлен", "обновлена", "обновлено", "обновлены"},
        {"обновили", "обновлен", "обновлена", "обновлено", "обновлены"},
    ),
    (
        {"восстановили", "восстановлен", "восстановлена", "восстановлено", "восстановлены"},
        {"восстановили", "восстановлен", "восстановлена", "восстановлено", "восстановлены"},
    ),
)

HEADLINE_EXACT_SUPPORT_TERMS = {
    "серебро", "золото", "бронза", "рекорд", "прорыв",
    "сенсация", "победа", "победили", "успешно",
}

HEADLINE_EXACT_SUPPORT_PHRASES = (
    "без потерь", "без ошибок", "без сбоев", "раньше срока",
    "досрочно", "впервые", "лучший результат",
)

NUMBER_RE = re.compile(r"(?<![\w])\d+(?:[\s\u00a0]\d{3})*(?:[.,]\d+)?", re.UNICODE)
NUMBER_WORDS = {
    "один", "одна", "одно", "два", "две", "три", "четыре", "пять",
    "шесть", "семь", "восемь", "девять", "десять", "одиннадцать",
    "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
    "двадцать", "тридцать", "сорок", "пятьдесят", "сто", "тысяча",
}
CONTENT_STOPWORDS = {
    "который", "которая", "которые", "этого", "этой", "своей", "своих",
    "после", "перед", "через", "также", "только", "сейчас", "чтобы",
    "будет", "были", "было", "стали", "среди", "нашей", "нашего",
}


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower().replace("ё", "е")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return _norm(value) in {"true", "да", "1", "yes"}


def source_blob(post: Dict[str, Any]) -> str:
    return " ".join(str(post.get(k) or "") for k in ("title", "author", "text"))


def canonicalize_rubric(value: Any) -> str | None:
    raw = str(value or "").strip().upper().replace("Ё", "Е")
    raw = re.sub(r"\s*[-–—]\s*", " — ", raw)
    raw = re.sub(r"\s+", " ", raw)
    if raw in RUBRIC_ALIASES:
        return RUBRIC_ALIASES[raw]
    raw_without_dash = raw.replace(" — ", " ")
    return RUBRIC_ALIASES.get(raw_without_dash)


def rubric_scores(post: Dict[str, Any]) -> Dict[str, int]:
    text = _norm(source_blob(post))
    scores = {name: 0 for name in CANONICAL_RUBRIC_ORDER}
    for rubric, weighted_phrases in RUBRIC_KEYWORDS.items():
        for phrase, weight in weighted_phrases:
            if phrase in text:
                scores[rubric] += weight
    return scores


def infer_rubric(post: Dict[str, Any]) -> Tuple[str, int, Dict[str, int]]:
    scores = rubric_scores(post)
    best = max(CANONICAL_RUBRIC_ORDER, key=lambda r: (scores.get(r, 0), -CANONICAL_RUBRIC_ORDER.index(r)))
    if scores.get(best, 0) == 0:
        best = "СОБЫТИЯ"
    return best, scores.get(best, 0), scores


def number_tokens(value: Any) -> set[str]:
    return {
        re.sub(r"[\s\u00a0]", "", m).replace(",", ".")
        for m in NUMBER_RE.findall(str(value or ""))
    }


def number_word_tokens(value: Any) -> set[str]:
    return set(re.findall(r"[а-я]+", _norm(value))) & NUMBER_WORDS


def has_only_source_numbers(generated: Any, post: Dict[str, Any]) -> bool:
    source = source_blob(post)
    return (
        number_tokens(generated).issubset(number_tokens(source))
        and number_word_tokens(generated).issubset(number_word_tokens(source))
    )


def _looks_trivial_number(value: str, description: str) -> bool:
    value_norm = _norm(value)
    description_norm = _norm(description)
    if ":" in value_norm and len(number_tokens(value_norm)) >= 2:
        return True
    tokens = number_tokens(value_norm)
    if len(tokens) != 1:
        return False
    try:
        numeric = float(next(iter(tokens)))
    except ValueError:
        return False
    if numeric.is_integer() and 1900 <= numeric <= 2100:
        return True
    months = (
        "январ", "феврал", "март", "апрел", "мая", "июн", "июл",
        "август", "сентябр", "октябр", "ноябр", "декабр",
    )
    if numeric.is_integer() and 1 <= numeric <= 31 and any(month in description_norm for month in months):
        return True
    if any(marker in description_norm for marker in ("номер корпуса", "счет матча", "счета матча", "время начала")):
        return True
    return False


def has_reasonable_source_overlap(value: Any, post: Dict[str, Any], minimum: float = 0.35) -> bool:
    """Отсекает тексты, где почти вся фактура появилась только после генерации."""
    candidate_tokens = [
        token for token in re.findall(r"[a-zа-я0-9-]+", _norm(_plain_text(value)))
        if len(token) >= 4 and token not in CONTENT_STOPWORDS
    ]
    if not candidate_tokens:
        return True
    source_tokens = [
        token for token in re.findall(r"[a-zа-я0-9-]+", _norm(source_blob(post)))
        if len(token) >= 4
    ]
    source_stems = {token[:5] for token in source_tokens}
    supported = sum(1 for token in candidate_tokens if token[:5] in source_stems)
    return supported / len(candidate_tokens) >= minimum


def _supported_fragment(value: Any, post: Dict[str, Any]) -> bool:
    candidate = _norm(value).strip("«»\"'.,:;!?–—- ")
    return bool(candidate) and candidate in _norm(source_blob(post))


def extract_direct_quote(post: Dict[str, Any]) -> str:
    text = str(post.get("text") or "")
    matches = re.findall(r"[«\"]([^»\"]{20,700})[»\"]", text, flags=re.DOTALL)
    if not matches:
        return ""
    return re.sub(r"\s+", " ", max(matches, key=len)).strip()


def _sentence_count(value: Any) -> int:
    text = re.sub(r"\s+", " ", _plain_text(value)).strip()
    if not text:
        return 0
    sentences = [part for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    return len(sentences)


def _source_supports_full_lead(post: Dict[str, Any]) -> bool:
    text = re.sub(r"\s+", " ", str(post.get("text") or "")).strip()
    return len(text) >= RICH_SOURCE_MIN_CHARS or _sentence_count(text) >= 3


def extractive_summary(
    post: Dict[str, Any],
    max_chars: int = CARD_TEXT_MAX_CHARS,
    min_chars: int = CARD_TEXT_TARGET_CHARS,
    max_sentences: int = 4,
) -> str:
    text = re.sub(r"\s+", " ", str(post.get("text") or "")).strip()
    if not text:
        return str(post.get("title") or "").strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chosen: List[str] = []
    for sentence in sentences:
        if not sentence:
            continue
        proposal = " ".join(chosen + [sentence]).strip()
        if chosen and len(proposal) > max_chars:
            break
        chosen.append(sentence)
        if (
            (len(proposal) >= min(min_chars, max_chars) and len(chosen) >= 2)
            or len(chosen) >= max_sentences
        ):
            break
    result = " ".join(chosen).strip()
    if len(result) > max_chars:
        result = result[: max_chars - 1].rsplit(" ", 1)[0].rstrip(".,;:–—-") + "…"
    return result


def normalize_classification(post: Dict[str, Any], raw: Dict[str, Any] | None) -> Dict[str, Any]:
    raw = dict(raw or {})
    flags: List[str] = []
    inferred, confidence, scores = infer_rubric(post)
    model_rubric = canonicalize_rubric(raw.get("rubric_candidate"))
    if model_rubric is None:
        rubric = inferred
        flags.append("Рубрика восстановлена по содержанию")
    elif inferred != model_rubric and confidence >= 6 and confidence >= scores.get(model_rubric, 0) + 3:
        rubric = inferred
        flags.append(f"Рубрика исправлена: {model_rubric} → {inferred}")
    else:
        rubric = model_rubric

    try:
        importance = int(round(float(raw.get("importance", 5))))
    except (TypeError, ValueError):
        importance = 5
    importance = max(1, min(10, importance))

    topic = str(post.get("title") or raw.get("topic") or "").strip()
    if not topic or not has_only_source_numbers(topic, post):
        topic = str(post.get("title") or extractive_summary(post, 100)).strip()

    # Планирование работает по extractive summary: так промежуточный пересказ
    # модели не может привнести новый результат, который затем разойдется по карточкам.
    summary = extractive_summary(post)

    number_value = str(raw.get("number_value") or "").strip()
    number_desc = str(raw.get("number_desc") or "").strip()
    has_number = _as_bool(raw.get("has_number")) and bool(number_value) and bool(number_desc)
    if has_number and (
        not has_only_source_numbers(number_value, post)
        or not has_only_source_numbers(number_desc, post)
        or _looks_trivial_number(number_value, number_desc)
    ):
        has_number = False
        number_value = ""
        number_desc = ""
        flags.append("Неподтвержденная цифра удалена")

    quote_text = str(raw.get("quote_text") or "").strip().strip("«»\"")
    has_quote = _as_bool(raw.get("has_quote")) and len(quote_text) >= 20
    if has_quote and not _supported_fragment(quote_text, post):
        quote_text = extract_direct_quote(post)
        has_quote = bool(quote_text)
        flags.append("Цитата восстановлена из исходного текста")

    allowed_people_blob = _norm(source_blob(post))
    raw_people = raw.get("people") if isinstance(raw.get("people"), list) else []
    people = [
        str(person).strip() for person in raw_people
        if str(person).strip() and _norm(person) in allowed_people_blob
    ]
    quote_author_name = str(raw.get("quote_author_name") or "").strip()
    quote_author_role = str(raw.get("quote_author_role") or "").strip()
    if quote_author_name and _norm(quote_author_name) not in allowed_people_blob:
        quote_author_name = ""
    if quote_author_role and _norm(quote_author_role) not in allowed_people_blob:
        quote_author_role = ""
    if has_quote and not quote_author_name and post.get("author"):
        author_parts = str(post.get("author")).split(",", 1)
        quote_author_name = author_parts[0].strip()
        quote_author_role = author_parts[1].strip() if len(author_parts) > 1 else quote_author_role

    return {
        "topic": topic[:140],
        "rubric_candidate": rubric,
        "importance": importance,
        "people": people,
        "has_number": has_number,
        "number_value": number_value or None,
        "number_desc": number_desc or None,
        "has_quote": has_quote,
        "quote_text": quote_text or None,
        "quote_author_name": quote_author_name or None,
        "quote_author_role": quote_author_role or None,
        "is_video": _as_bool(raw.get("is_video")),
        "is_special": _as_bool(raw.get("is_special")),
        "summary_short": summary[:600],
        "quality_flags": flags,
    }


def _plain_text(value: Any) -> str:
    return re.sub(r"<[^>]+>", "", str(value or ""))


def _fallback_title(post: Dict[str, Any], uppercase: bool) -> str:
    title = re.sub(r"\s+", " ", str(post.get("title") or "")).strip(" .")
    if not title or _headline_is_weak(title):
        title = extractive_summary(post, 90).split(".", 1)[0].strip()
    words = title.split()
    max_words = HEADLINE_MAX_WORDS if uppercase else MAIN_HEADLINE_MAX_WORDS
    if len(words) > max_words:
        title = " ".join(words[:max_words]).rstrip(".,;:–—-")
    title = _clean_headline(title)
    return title.upper() if uppercase else title[:1].upper() + title[1:]


def _clean_headline(value: Any) -> str:
    """Приводит вариант заголовка к фирменной типографике без переписывания."""
    title = re.sub(r"\s+", " ", _plain_text(value)).strip()
    title = title.replace("ё", "е").replace("Ё", "Е").replace("—", "–")
    title = re.sub(r"\s+[-–]\s+", " – ", title)
    title = title.strip(" \t\r\n.,:;–—-")
    return title


def _headline_words(value: Any) -> List[str]:
    return re.findall(r"[A-Za-zА-Яа-я0-9Ёё-]+", str(value or ""))


def _headline_is_weak(title: str) -> bool:
    normalized = _norm(title).strip(" .,!?:;–—-")
    if any(phrase in normalized for phrase in WEAK_HEADLINE_PHRASES):
        return True
    words = [word.lower().replace("ё", "е") for word in _headline_words(title)]
    meaningful = [word for word in words if len(word) >= 4]
    return bool(meaningful) and all(word in WEAK_HEADLINE_WORDS for word in meaningful)


def _headline_claims_are_supported(title: str, post: Dict[str, Any]) -> bool:
    candidate_norm = _norm(title)
    source_norm = _norm(source_blob(post))
    candidate_words = set(re.findall(r"[a-zа-я0-9-]+", candidate_norm))
    source_words = set(re.findall(r"[a-zа-я0-9-]+", source_norm))

    for candidate_group, source_group in HEADLINE_STATUS_GROUPS:
        if candidate_words & candidate_group and not source_words & source_group:
            return False
    for term in candidate_words & HEADLINE_EXACT_SUPPORT_TERMS:
        if term not in source_words:
            return False
    for phrase in HEADLINE_EXACT_SUPPORT_PHRASES:
        if phrase in candidate_norm and phrase not in source_norm:
            return False
    return True


def _headline_is_valid(title: str, post: Dict[str, Any], is_main: bool) -> bool:
    words = _headline_words(title)
    max_words = MAIN_HEADLINE_MAX_WORDS if is_main else HEADLINE_MAX_WORDS
    source_title = str(post.get("title") or "")
    return bool(
        title
        and HEADLINE_MIN_WORDS <= len(words) <= max_words
        and len(title) <= 90
        and has_only_source_numbers(title, post)
        and has_reasonable_source_overlap(title, post, minimum=0.3)
        and _headline_claims_are_supported(title, post)
        and not _headline_is_weak(title)
        and not ("?" in title and "?" not in source_title)
        and "!!" not in title
        and "?!" not in title
        and "!?" not in title
    )


def _headline_score(title: str, post: Dict[str, Any], candidate_index: int) -> float:
    """Ранжирует только уже проверенные варианты, не решая за модель факты."""
    words = [word.lower().replace("ё", "е") for word in _headline_words(title)]
    content_words = [
        word for word in words
        if len(word) >= 4 and word not in CONTENT_STOPWORDS
    ]
    source_tokens = re.findall(r"[a-zа-я0-9-]+", _norm(source_blob(post)))
    source_stems = {token[:5] for token in source_tokens if len(token) >= 4}
    supported_specific = sum(
        1 for word in content_words
        if (
            word[:5] in source_stems
            and word not in WEAK_HEADLINE_WORDS
            and word not in GENERIC_HEADLINE_NOUNS
            and word not in ABSTRACT_HEADLINE_OPENERS
        )
    )

    score = min(supported_specific, 3) * 0.8
    if 3 <= len(words) <= 6:
        score += 3.0
    elif len(words) == 2:
        score += 2.0
    elif len(words) == 7:
        score += 1.0
    if len(set(words)) == len(words):
        score += 0.5
    if any(word.endswith(HEADLINE_ACTION_ENDINGS) for word in words if len(word) >= 5):
        score += 0.75
    if number_tokens(title):
        score += 0.5
    score -= sum(1.25 for word in words if word in WEAK_HEADLINE_WORDS)
    if words and words[0] in ABSTRACT_HEADLINE_OPENERS:
        score -= 1.5
    if words and words[0] in GENERIC_HEADLINE_NOUNS:
        score -= 0.75
    score -= title.count(":") * 0.5
    score -= candidate_index * 0.15
    return score


def construct_headline(
    post: Dict[str, Any],
    context: Dict[str, Any],
    raw: Dict[str, Any],
) -> Tuple[str, bool]:
    """Выбирает сильнейший фактический заголовок из вариантов модели.

    Возвращает пару ``(title, used_fallback)``. Модель отвечает за редакторскую
    идею, а код – за длину, числа, связь с исходником и антиштампы.
    """
    is_main = bool(context.get("is_main_block"))
    uppercase = not is_main
    candidate_values: List[Any] = [raw.get("title")]
    raw_candidates = raw.get("title_candidates")
    if isinstance(raw_candidates, list):
        for item in raw_candidates[:HEADLINE_CANDIDATE_COUNT]:
            if isinstance(item, dict):
                candidate_values.append(item.get("title"))
            else:
                candidate_values.append(item)

    candidates: List[str] = []
    seen = set()
    for value in candidate_values:
        title = _clean_headline(value)
        key = _norm(title)
        if title and key not in seen:
            candidates.append(title)
            seen.add(key)

    valid = [
        (title, index)
        for index, title in enumerate(candidates)
        if _headline_is_valid(title, post, is_main)
    ]
    if valid:
        title, _ = max(
            valid,
            key=lambda item: _headline_score(item[0], post, item[1]),
        )
        if uppercase:
            title = title.upper()
        else:
            title = title[:1].upper() + title[1:]
        return title, False

    return _fallback_title(post, uppercase=uppercase), True


def sanitize_card_text(value: Any, fallback_link: str) -> str:
    """Оставляет только безопасные ссылки и всегда подставляет URL из Excel."""
    raw = str(value or "")
    pattern = re.compile(r"<a\b[^>]*>(.*?)</a>", flags=re.IGNORECASE | re.DOTALL)
    parts: List[str] = []
    cursor = 0
    safe_href = html.escape(str(fallback_link or "#"), quote=True)
    for match in pattern.finditer(raw):
        outside = re.sub(r"<[^>]+>", "", raw[cursor:match.start()])
        parts.append(html.escape(outside, quote=False))
        inner = re.sub(r"<[^>]+>", "", match.group(1))
        inner = html.escape(inner, quote=False)
        if fallback_link:
            parts.append(
                f'<a href="{safe_href}" style="color:#008C95;text-decoration:underline;">{inner}</a>'
            )
        else:
            parts.append(inner)
        cursor = match.end()
    tail = re.sub(r"<[^>]+>", "", raw[cursor:])
    parts.append(html.escape(tail, quote=False))
    return "".join(parts).strip()


def validate_rewrite_output(
    post: Dict[str, Any],
    context: Dict[str, Any],
    raw: Dict[str, Any] | None,
) -> Tuple[Dict[str, Any], List[str]]:
    raw = dict(raw or {})
    flags: List[str] = []

    if context.get("is_figure"):
        value = str(raw.get("value") or "").strip()
        description = str(raw.get("description") or "").strip()
        if not value or not has_only_source_numbers(value, post):
            value = str(post.get("number_value") or "").strip()
            flags.append("Главная цифра возвращена к значению из исходника")
        if not description or not has_only_source_numbers(description, post):
            description = str(post.get("number_desc") or post.get("summary_short") or "").strip()
            flags.append("Описание цифры возвращено к исходнику")
        return {"value": value, "description": description}, flags

    if context.get("is_quote"):
        quote = str(raw.get("quote_text") or "").strip().strip("«»\"")
        if not _supported_fragment(quote, post):
            quote = str(post.get("quote_text") or extract_direct_quote(post)).strip()
            flags.append("Цитата возвращена к дословному исходнику")
        name = str(raw.get("author_name") or post.get("quote_author_name") or "").strip()
        role = str(raw.get("author_role") or post.get("quote_author_role") or "").strip()
        if name and _norm(name) not in _norm(source_blob(post)):
            name = str(post.get("quote_author_name") or "")
        if role and _norm(role) not in _norm(source_blob(post)):
            role = str(post.get("quote_author_role") or "")
        return {"quote_text": quote, "author_name": name, "author_role": role}, flags

    is_main = bool(context.get("is_main_block"))
    title, used_title_fallback = construct_headline(post, context, raw)
    if used_title_fallback:
        flags.append("Заголовок заменен на фактический")

    # Главный блок содержит только заголовок. Отсутствие поля text в ответе
    # модели здесь штатно и не должно давать ложный флаг качества.
    if is_main or context.get("title_only"):
        return {"title": title}, flags

    text = str(raw.get("text") or "").strip()
    plain = _plain_text(text)
    has_cliche = any(phrase in _norm(plain) for phrase in FORBIDDEN_CLICHES)
    is_video = bool(context.get("is_video"))
    min_chars = VIDEO_TEXT_MIN_CHARS if is_video else CARD_TEXT_MIN_CHARS
    too_short = _source_supports_full_lead(post) and (
        len(plain) < min_chars
        or (not is_video and _sentence_count(plain) < 2)
    )
    if (
        not text
        or len(plain) > CARD_TEXT_MAX_CHARS
        or not has_only_source_numbers(plain, post)
        or not has_reasonable_source_overlap(plain, post)
        or has_cliche
        or too_short
    ):
        text = extractive_summary(
            post,
            max_chars=CARD_TEXT_MAX_CHARS,
            min_chars=CARD_TEXT_TARGET_CHARS,
            max_sentences=4,
        )
        if has_cliche:
            flags.append("ИИ-штамп удален")
        elif too_short:
            flags.append("Короткая подводка дополнена фактами из источника")
        else:
            flags.append("Подводка заменена на фактический фрагмент")
    text = sanitize_card_text(text, str(post.get("link") or ""))
    return {"title": title, "text": text}, flags


def _title_terms(post: Dict[str, Any]) -> set[str]:
    stop = {
        "анонс", "итоги", "приглашаем", "приглашение", "результаты",
        "прошел", "прошла", "прошли", "состоялся", "состоялась",
    }
    return {
        token for token in re.findall(r"[а-я0-9]+", _norm(post.get("title")))
        if len(token) >= 4 and token not in stop
    }


def find_announcement_report_duplicates(posts: Iterable[Dict[str, Any]]) -> Dict[int, str]:
    """Исключает анонс, если в том же наборе уже есть отчет о событии."""
    items = list(posts)
    announcement_markers = ("приглашаем", "пройдет", "состоится", "регистрац", "анонс")
    report_markers = ("прошел", "прошла", "состоялся", "состоялась", "подвели итоги", "завершился")
    skipped: Dict[int, str] = {}
    for announcement in items:
        a_blob = _norm(source_blob(announcement))
        if not any(marker in a_blob for marker in announcement_markers):
            continue
        a_terms = _title_terms(announcement)
        if not a_terms:
            continue
        for report in items:
            if report is announcement:
                continue
            r_blob = _norm(source_blob(report))
            if not any(marker in r_blob for marker in report_markers):
                continue
            r_terms = _title_terms(report)
            if a_terms & r_terms:
                skipped[int(announcement["post_id"])] = (
                    f"Анонс исключен: в выпуске есть отчет по той же теме (пост #{report['post_id']})"
                )
                break
    return skipped
