"""
Пайплайн обработки постов через LLM.

Три шага:
  1. classify_posts(posts) — батчами по 10
  2. plan_digest(classified) — один большой вызов
  3. rewrite_blocks(plan, classified) — параллельно

Каждый шаг проверяемый, с предупреждениями вместо немых сбоев.
"""
import json
import logging
import re
from html import unescape, escape
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Callable

from .config import (
    CLASSIFY_BATCH_SIZE,
    CLASSIFY_TEMPERATURE,
    PLAN_TEMPERATURE,
    REWRITE_TEMPERATURE,
    LLM_PARALLEL_WORKERS,
    CANONICAL_RUBRIC_ORDER,
    RUBRIC_ICONS,
    MAIN_BLOCK_SIZE,
    RUBRIC_TARGET_MIN,
    RUBRIC_TARGET_MAX,
    CARD_TITLE_MIN_WORDS,
    CARD_TITLE_MAX_WORDS,
    CARD_TEXT_MAX_CHARS,
    CARD_TEXT_MAX_SENTENCES,
    MAIN_FIGURE_DESC_MAX_WORDS,
)
from .llm_client import llm_json, load_prompt

log = logging.getLogger(__name__)

NEWS_RUBRICS = tuple(CANONICAL_RUBRIC_ORDER[:7])

_TAG_RE = re.compile(r"<[^>]+>")
_NON_ANCHOR_TAG_RE = re.compile(r"<(?!/?a(?:\s|>))[^>]+>", re.IGNORECASE)
_NUMBER_RE = re.compile(
    r"(?<![\w])\d+(?:[\s\u00a0]\d{3})*(?:[.,]\d+)?(?:\s*%|\s*(?:млн|тыс\.?))?",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-zА-Яа-я0-9]+(?:-[A-Za-zА-Яа-я0-9]+)?")
_CTA_RE = re.compile(
    r"регистрац|зарегистрир|запис|подать|заявк|перейти по ссылке|подробнее|"
    r"смотр|пройти курс|до\s+\d{1,2}\s+[а-я]+",
    re.IGNORECASE,
)
_BANNED_COPY = (
    "это не просто",
    "меняет правила игры",
    "и это только начало",
    "новый уровень",
    "настоящий праздник",
    "незабываемые эмоции",
    "впереди еще много работы, но",
)
_SENSITIVE_CLAIMS = (
    "впервые",
    "первый",
    "лучш",
    "рекорд",
    "побед",
    "наград",
    "успеш",
    "уникаль",
    "экономическ",
    "эффект",
    "сниз",
    "увелич",
    "без происшеств",
)


def _plain_text(value: Any) -> str:
    """Видимый текст без HTML, лишних пробелов и буквы «ё»."""
    text = unescape(_TAG_RE.sub(" ", str(value or "")))
    text = text.replace("ё", "е").replace("Ё", "Е").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _source_text(post: Dict[str, Any]) -> str:
    return _plain_text(" ".join(
        str(post.get(key, "") or "")
        for key in ("date", "author", "title", "text")
    ))


def _normalise_number(token: str) -> str:
    return re.sub(r"\s+", "", token.lower().replace(",", "."))


def _numbers_are_grounded(value: Any, post: Dict[str, Any]) -> bool:
    generated = {_normalise_number(x) for x in _NUMBER_RE.findall(_plain_text(value))}
    if not generated:
        return True
    source = {_normalise_number(x) for x in _NUMBER_RE.findall(_source_text(post))}
    return generated.issubset(source)


def _claims_are_grounded(value: Any, post: Dict[str, Any]) -> bool:
    generated = _plain_text(value).lower()
    source = _source_text(post).lower()
    return all(stem not in generated or stem in source for stem in _SENSITIVE_CLAIMS)


def _modality_is_grounded(value: Any, post: Dict[str, Any]) -> bool:
    """Не дает превратить отрицание, план или испытание в готовый результат."""
    generated = _plain_text(value).lower()
    source = _source_text(post).lower()
    negation_pairs = (
        (r"не\s+прош", r"\bпрош(?:ел|ла|ли|ло)?\b"),
        (r"не\s+подтверж", r"\bподтвержден"),
        (r"не\s+достиг", r"\bдостиг"),
        (r"не\s+заверш", r"\bзаверш"),
    )
    for source_pattern, assertion_pattern in negation_pairs:
        if re.search(source_pattern, source):
            if re.search(assertion_pattern, generated) and not re.search(source_pattern, generated):
                return False

    future_markers = (
        "планируется", "предстоит", "ожидается", "готовит", "готовится",
        "пройдет", "состоится", "будет", "появится", "откроется",
    )
    completion_stems = ("завершил", "запустил", "ввел", "получил", "достиг")
    if any(marker in source for marker in future_markers):
        for stem in completion_stems:
            if stem in generated and stem not in source:
                return False
    return True


def _is_obvious_noise(post: Dict[str, Any]) -> bool:
    """Узкий детерминированный фильтр: лучше оставить сомнительное, чем потерять новость."""
    text = _source_text(post).lower()
    patterns = (
        r"\bнайден[аоы]?\b.*\b(кружк|ключ|зонт|перчат|телефон)",
        r"\bпотерян[аоы]?\b.*\b(кружк|ключ|зонт|перчат|телефон)",
        r"\bпереставил[иа]?\b.*\b(стол|шкаф|мебел)",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _strong_keyword_rubric(post: Dict[str, Any]) -> Optional[str]:
    """Возвращает рубрику только при наличии однозначного тематического маркера."""
    text = _source_text(post).lower()
    rules = (
        ("ПСС", r"\bпсс\b|\bа-?3\b|\b5с\b|кпсц|мини-т|бережлив|картирован|маршрут.{0,25}обход|устранен.{0,20}потер"),
        ("ДОСТИЖЕНИЯ", r"побед|наград|медал|призов|лауреат|занял[аи]? .{0,20}мест|рекорд"),
        ("БЕЗОПАСНОСТЬ", r"безопасност|охран[аы] труда|стоп-карт|противоавар|пожар|\bсиз\b|выброс|травм"),
        ("ЗАБОТА О ЛЮДЯХ", r"\bдмс\b|страхов|педиатр|психолог|столов|питани|льгот|ветеран|профсоюз|благотвор"),
        ("КАРЬЕРА", r"обучен|вебинар|настав|стажир|карьер|кадров.{0,15}резерв|егэ|экзамен|профориент|профильн.{0,10}класс"),
        ("ПРОИЗВОДСТВО", r"производ|установк|цех|оборудован|ремонт|сырь|полимер|насос|компресс|технолог"),
    )
    for rubric, pattern in rules:
        if re.search(pattern, text):
            return rubric
    return None


def _keyword_rubric(post: Dict[str, Any]) -> str:
    """Фолбэк при сбое LLM: однозначный маркер либо нейтральные «СОБЫТИЯ»."""
    return _strong_keyword_rubric(post) or "СОБЫТИЯ"


def _normalise_rubric(value: Any, post: Dict[str, Any]) -> str:
    manual = str(post.get("rubric", "") or "").strip().upper().strip("/ ")
    aliases = {
        "ЗАБОТА": "ЗАБОТА О ЛЮДЯХ",
        "КАРЬЕРА И ОБУЧЕНИЕ": "КАРЬЕРА",
        "ПРОИЗВОДСТВЕННАЯ СИСТЕМА СИБУРА": "ПСС",
    }
    manual = aliases.get(manual, manual)
    if manual in NEWS_RUBRICS:
        return manual

    # Однозначные маркеры защищают от типовой ошибки «все в СОБЫТИЯ».
    strong_candidate = _strong_keyword_rubric(post)
    if strong_candidate:
        return strong_candidate

    candidate = str(value or "").strip().upper().strip("/ ")
    candidate = aliases.get(candidate, candidate)
    return candidate if candidate in NEWS_RUBRICS else _keyword_rubric(post)


def _clamp_importance(value: Any) -> int:
    try:
        return max(1, min(10, int(float(value))))
    except (TypeError, ValueError):
        return 5


def _coerce_confidence(value: Any, manual: bool = False) -> float:
    if manual:
        return 1.0
    try:
        confidence = float(value)
        return max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        return 0.5


def _coerce_id(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _grounded_quote(post: Dict[str, Any]) -> Optional[str]:
    quote = _plain_text(post.get("quote_text"))
    if not quote:
        return None
    source = _source_text(post).lower()
    return quote if quote.lower() in source else None


def _evidence_is_grounded(value: Any, post: Dict[str, Any]) -> bool:
    """Проверяет, что модель приложила дословные фрагменты исходника."""
    if isinstance(value, str):
        fragments = [value]
    elif isinstance(value, list):
        fragments = value
    else:
        return False

    source = _source_text(post).lower()
    cleaned = [_plain_text(fragment) for fragment in fragments]
    cleaned = [fragment for fragment in cleaned if fragment]
    if not cleaned:
        return False
    return all(
        len(_WORD_RE.findall(fragment)) >= 3
        and fragment.lower() in source
        for fragment in cleaned
    )


def _fallback_title(post: Dict[str, Any]) -> str:
    source = _plain_text(post.get("title")) or _plain_text(post.get("text"))
    words = _WORD_RE.findall(source)
    if not words:
        return "НОВОСТЬ КОС"
    if len(words) == 1:
        extra = _WORD_RE.findall(_plain_text(post.get("text")))
        words = (words + [w for w in extra if w.lower() != words[0].lower()])[:2]
    if len(words) < CARD_TITLE_MIN_WORDS:
        words.append("КОС")
    return " ".join(words[:CARD_TITLE_MAX_WORDS]).upper()


def _safe_title(
    value: Any,
    post: Dict[str, Any],
    *,
    evidence: Any = None,
    require_evidence: bool = False,
) -> str:
    title = _plain_text(value).strip("–—-:;,.! ")
    lowered = title.lower()
    words = _WORD_RE.findall(title)
    invalid = (
        not (CARD_TITLE_MIN_WORDS <= len(words) <= CARD_TITLE_MAX_WORDS)
        or "?" in title
        or any(phrase in lowered for phrase in _BANNED_COPY)
        or not _numbers_are_grounded(title, post)
        or not _claims_are_grounded(title, post)
        or not _modality_is_grounded(title, post)
        or (require_evidence and not _evidence_is_grounded(evidence, post))
    )
    return _fallback_title(post) if invalid else title.upper()


def _sentence_count(text: str) -> int:
    parts = re.split(r"(?<=[.!?])\s+(?=[А-ЯA-Z])", text.strip())
    return len([part for part in parts if part.strip()])


def _source_excerpt(post: Dict[str, Any]) -> str:
    source = _plain_text(post.get("text"))
    title = _plain_text(post.get("title"))
    if title and source.lower().startswith(title.lower()):
        source = source[len(title):].lstrip(" .:–—-")
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", source) if s.strip()]
    selected = sentences[:2]
    cta = next((s for s in sentences if _CTA_RE.search(s)), None)
    if cta and cta not in selected:
        selected.append(cta)
    text = " ".join(selected[:CARD_TEXT_MAX_SENTENCES]) or source
    has_cta = bool(_CTA_RE.search(source))
    reserve = 16 if post.get("link") and has_cta else 0
    limit = CARD_TEXT_MAX_CHARS - reserve
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
    if post.get("link") and has_cta:
        text = text.rstrip() + " <a>Подробнее</a>."
    return _fix_links(text, post.get("link", ""))


def _safe_card_text(
    value: Any,
    post: Dict[str, Any],
    *,
    evidence: Any = None,
    require_evidence: bool = False,
) -> str:
    html_text = _NON_ANCHOR_TAG_RE.sub("", str(value or ""))
    html_text = html_text.replace("ё", "е").replace("Ё", "Е")
    html_text = re.sub(r"\s+", " ", html_text).strip()
    visible = _plain_text(html_text)
    invalid = (
        not visible
        or len(visible) > CARD_TEXT_MAX_CHARS
        or _sentence_count(visible) > CARD_TEXT_MAX_SENTENCES
        or "?" in visible
        or any(phrase in visible.lower() for phrase in _BANNED_COPY)
        or not _numbers_are_grounded(visible, post)
        or not _claims_are_grounded(visible, post)
        or not _modality_is_grounded(visible, post)
        or (require_evidence and not _evidence_is_grounded(evidence, post))
    )
    if invalid:
        return _source_excerpt(post)
    if not post.get("link"):
        html_text = re.sub(r"</?a\b[^>]*>", "", html_text, flags=re.IGNORECASE)
    elif _CTA_RE.search(_source_text(post)) and "<a" not in html_text.lower():
        addition = " <a>Подробнее</a>."
        if len(visible) + len(_plain_text(addition)) <= CARD_TEXT_MAX_CHARS:
            html_text = html_text.rstrip(". ") + "." + addition
    return _fix_links(html_text, post.get("link", ""))


def _source_figure_description(post: Dict[str, Any]) -> str:
    source = _plain_text(post.get("text") or post.get("title"))
    value = _plain_text(post.get("number_value"))
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", source)
        if sentence.strip()
    ]
    matching = next(
        (sentence for sentence in sentences if value and value.lower() in sentence.lower()),
        None,
    )
    description = matching or (sentences[0] if sentences else source)
    return " ".join(description.split()[:MAIN_FIGURE_DESC_MAX_WORDS]).rstrip(".,;:")


def _safe_figure_description(
    value: Any,
    post: Dict[str, Any],
    *,
    evidence: Any = None,
    require_evidence: bool = False,
) -> str:
    description = _plain_text(value or post.get("number_desc"))
    if (
        not description
        or not _numbers_are_grounded(description, post)
        or not _claims_are_grounded(description, post)
        or (require_evidence and not _evidence_is_grounded(evidence, post))
    ):
        description = _source_figure_description(post)
    words = description.split()
    return " ".join(words[:MAIN_FIGURE_DESC_MAX_WORDS]).rstrip(".,;:")


# ===========================================================================
# ШАГ 1. КЛАССИФИКАЦИЯ (БАТЧАМИ)
# ===========================================================================

def classify_posts(
    posts: List[Dict[str, Any]],
    progress: Optional[Callable] = None,
) -> List[Dict[str, Any]]:
    """
    Прогоняет все посты через LLM батчами по CLASSIFY_BATCH_SIZE.
    Возвращает посты с добавленными полями классификации.
    """
    system_prompt = load_prompt("classify")
    result: Dict[int, Dict[str, Any]] = {}  # post_id → classification

    # Разбиваем на батчи
    total = len(posts)
    batches = [posts[i:i + CLASSIFY_BATCH_SIZE]
               for i in range(0, total, CLASSIFY_BATCH_SIZE)]

    def process_batch(batch_idx: int, batch: List[Dict[str, Any]]):
        payload = {
            "posts": [
                {
                    "id": p["post_id"],
                    "date": p.get("date", ""),
                    "author": p.get("author", ""),
                    "title": p.get("title", ""),
                    "rubric_override": p.get("rubric", ""),
                    "text": p.get("text", "")[:2500],  # обрезаем длинные посты
                }
                for p in batch
            ]
        }
        try:
            response = llm_json(
                system_prompt,
                json.dumps(payload, ensure_ascii=False),
                temperature=CLASSIFY_TEMPERATURE,
                label=f"classify[{batch_idx}]",
            )
            items = response.get("items", [])
            # Сопоставляем по id
            return {
                _coerce_id(item.get("id")): item
                for item in items
                if _coerce_id(item.get("id")) is not None
            }
        except Exception as e:
            log.error(f"Батч {batch_idx} провалился: {e}")
            return {}

    # Параллельная обработка батчей
    with ThreadPoolExecutor(max_workers=min(LLM_PARALLEL_WORKERS, len(batches))) as ex:
        futures = {ex.submit(process_batch, i, b): i for i, b in enumerate(batches)}
        done = 0
        for f in as_completed(futures):
            batch_idx = futures[f]
            batch_result = f.result()
            result.update(batch_result)
            done += 1
            if progress:
                progress(done, len(batches), f"Классификация: батч {done}/{len(batches)}")

    # Прикрепляем классификацию к каждому посту
    # КРИТИЧНО: post сначала, классификация поверх — но post.image_file всегда побеждает.
    classified = []
    for post in posts:
        pid = post["post_id"]
        cls = result.get(pid)
        if cls is None:
            # Фолбэк: дефолтная классификация
            log.warning(f"Пост {pid}: классификация не получена, использую дефолт")
            cls = _default_classification(post)
        # ВНИМАНИЕ: cls имеет приоритет НИЖЕ, чем оригинальные поля post.
        # Это гарантирует, что image_file/link/text из Excel не будут перетёрты.
        merged = {**cls, **post}
        # Гарантируем, что нужные поля есть с дефолтами, если LLM их пропустила
        merged.setdefault("topic", merged.get("title") or "(тема не определена)")
        manual_rubric = bool(post.get("rubric"))
        strong_rubric = _strong_keyword_rubric(post)
        merged["rubric_candidate"] = _normalise_rubric(
            merged.get("rubric_candidate"), post
        )
        merged["rubric_basis"] = (
            "manual" if manual_rubric
            else "rule" if strong_rubric
            else "model"
        )
        merged["rubric_confidence"] = _coerce_confidence(
            merged.get("rubric_confidence"), manual=manual_rubric
        )
        if strong_rubric and not manual_rubric:
            merged["rubric_confidence"] = max(0.95, merged["rubric_confidence"])
        merged["importance"] = _clamp_importance(merged.get("importance"))
        include_value = merged.get("include_in_digest", True)
        if isinstance(include_value, str):
            include_value = include_value.strip().lower() not in {"false", "нет", "0", "no"}
        merged["include_in_digest"] = (
            (bool(include_value) or merged["importance"] > 3)
            and not _is_obvious_noise(post)
        )
        merged.setdefault("exclude_reason", None)
        if not merged["include_in_digest"] and not merged.get("exclude_reason"):
            merged["exclude_reason"] = "очевидное служебное или бытовое сообщение"
        merged.setdefault("has_number", False)
        merged.setdefault("has_quote", False)
        merged.setdefault("is_video", False)
        merged.setdefault("is_special", False)
        merged.setdefault("summary_short", post.get("text", "")[:300])

        # Цифра и цитата допустимы только как дословные элементы исходника.
        if not merged.get("number_value") or not _numbers_are_grounded(merged.get("number_value"), post):
            merged["has_number"] = False
            merged["number_value"] = None
            merged["number_desc"] = None
        elif merged.get("has_number"):
            merged["number_desc"] = _source_figure_description(merged)
        if not _grounded_quote(merged):
            merged["has_quote"] = False
            merged["quote_text"] = None
            merged["quote_author_name"] = None
            merged["quote_author_role"] = None
        classified.append(merged)

    return classified


def _default_classification(post: Dict[str, Any]) -> Dict[str, Any]:
    """Безопасный фолбэк, если LLM упала или вернула не тот id"""
    return {
        "topic": post.get("title") or post.get("text", "")[:80],
        "rubric_candidate": _keyword_rubric(post),
        "rubric_confidence": 0.35,
        "importance": 5,
        "include_in_digest": not _is_obvious_noise(post),
        "exclude_reason": "очевидное служебное или бытовое сообщение" if _is_obvious_noise(post) else None,
        "people": [],
        "has_number": False,
        "number_value": None,
        "number_desc": None,
        "has_quote": False,
        "quote_text": None,
        "quote_author_name": None,
        "quote_author_role": None,
        "is_video": False,
        "is_special": False,
        "summary_short": post.get("text", "")[:300],
    }


# ===========================================================================
# ШАГ 2. ПЛАН ДАЙДЖЕСТА
# ===========================================================================

def plan_digest(
    classified: List[Dict[str, Any]],
    progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Составляет план: что куда положить."""
    if progress:
        progress(0, 1, "Составляю план дайджеста…")

    system_prompt = load_prompt("plan")

    # Сжатый payload — без полных текстов
    compact = [
        {
            "post_id": p["post_id"],
            "date": p.get("date", ""),
            "title": p.get("title", ""),
            "topic": p.get("topic", ""),
            "summary_short": (p.get("summary_short") or "")[:300],
            "text_excerpt": (p.get("text") or "")[:900],
            "rubric_candidate": p.get("rubric_candidate"),
            "importance": p.get("importance", 5),
            "has_number": p.get("has_number", False),
            "number_value": p.get("number_value"),
            "number_desc": p.get("number_desc"),
            "has_quote": p.get("has_quote", False),
            "quote_author_name": p.get("quote_author_name"),
            "quote_author_role": p.get("quote_author_role"),
            "is_video": p.get("is_video", False),
            "is_special": p.get("is_special", False),
            "has_image": bool(p.get("image_file")),
        }
        for p in classified
    ]

    plan = llm_json(
        system_prompt,
        json.dumps({"posts": compact}, ensure_ascii=False),
        temperature=PLAN_TEMPERATURE,
        label="plan",
    )

    if progress:
        progress(1, 1, "План готов")
    return plan


# ===========================================================================
# ШАГ 3. ПЕРЕПИСЬ — ПАРАЛЛЕЛЬНО
# ===========================================================================

def rewrite_card(post: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Один пост → одна переписанная карточка."""
    system_prompt = load_prompt("rewrite")
    payload = {
        "post": {
            "title": post.get("title", ""),
            "text": (post.get("text") or "")[:4000],
            "date": post.get("date", ""),
            "author": post.get("author", ""),
            "link": post.get("link", ""),
            "rubric": post.get("rubric_candidate", ""),
            "summary_short": post.get("summary_short", ""),
            "quote_text": post.get("quote_text"),
            "quote_author_name": post.get("quote_author_name"),
            "quote_author_role": post.get("quote_author_role"),
            "number_value": post.get("number_value"),
            "number_desc": post.get("number_desc"),
        },
        "context": context,
    }
    result = llm_json(
        system_prompt,
        json.dumps(payload, ensure_ascii=False),
        temperature=REWRITE_TEMPERATURE,
        label="rewrite",
    )
    if "text" in result and isinstance(result["text"], str):
        result["text"] = _fix_links(result["text"], post.get("link", ""))
    return result


def _fix_links(text: str, fallback_link: str) -> str:
    """
    Чинит теги <a> в тексте карточки:
      - Если <a> без href — подставляет ссылку из Excel.
      - Если href пустой или '#' — подставляет ссылку из Excel.
      - Гарантирует inline-стиль для Outlook.
    """
    import re
    if not fallback_link:
        fallback_link = "#"
    fallback_link = escape(str(fallback_link), quote=True)

    def replace_a(match):
        attrs = match.group(1) or ""
        inner = match.group(2) or ""

        href_match = re.search(r'href\s*=\s*["\']([^"\']*)["\']', attrs)
        if href_match:
            attrs = re.sub(
                r'href\s*=\s*["\'][^"\']*["\']',
                f'href="{fallback_link}"',
                attrs,
            )
        else:
            attrs = f' href="{fallback_link}"' + attrs

        if "style" not in attrs.lower():
            attrs += ' style="color:#008C95;text-decoration:underline;"'

        return f"<a{attrs}>{inner}</a>"

    return re.sub(r"<a\b([^>]*)>(.*?)</a>", replace_a, text, flags=re.IGNORECASE | re.DOTALL)


# ===========================================================================
# ОРКЕСТРАЦИЯ — ВСЕ ТРИ ШАГА
# ===========================================================================

def build_digest_draft(
    posts: List[Dict[str, Any]],
    progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """
    Полный пайплайн с детерминированной маршрутизацией.

    LLM выбирает верхние блоки и редактирует тексты, но не может переносить
    новости между рубриками, обрезать источники или добавлять непроверенные
    факты. При сбое каждого LLM-шага остается безопасный фолбэк.
    """
    warnings: List[str] = []

    for i, post in enumerate(posts, start=1):
        post["post_id"] = i

    classified = classify_posts(posts, progress=progress)
    eligible = [p for p in classified if p.get("include_in_digest", True)]
    excluded = [p for p in classified if not p.get("include_in_digest", True)]

    if not eligible:
        raise ValueError(
            "После фильтрации не осталось содержательных новостей. "
            "Проверьте исходный Excel и колонку text."
        )

    try:
        plan = plan_digest(eligible, progress=progress)
    except Exception as exc:
        log.exception("Не удалось составить план, использую детерминированный фолбэк")
        warnings.append(
            f"План верхних блоков не получен: {exc}. "
            "Главные новости выбраны по важности."
        )
        plan = {
            "subject_topics": [],
            "main_block": [],
            "main_figure_post_id": None,
            "main_video_post_id": None,
            "main_quote_post_id": None,
        }

    by_id = {p["post_id"]: p for p in eligible}
    ranked = sorted(
        eligible,
        key=lambda p: (-p.get("importance", 5), p.get("post_id", 0)),
    )
    used_ids = set()

    # === РОВНО ТРИ ГЛАВНЫЕ НОВОСТИ ===
    main_ids: List[int] = []
    for item in plan.get("main_block", []) or []:
        raw_id = item.get("post_id") if isinstance(item, dict) else item
        pid = _coerce_id(raw_id)
        if pid in by_id and pid not in main_ids:
            main_ids.append(pid)
        if len(main_ids) == MAIN_BLOCK_SIZE:
            break

    for post in ranked:
        pid = post["post_id"]
        if len(main_ids) >= MAIN_BLOCK_SIZE:
            break
        if pid not in main_ids:
            main_ids.append(pid)

    main_block = []
    for pid in main_ids:
        post = by_id[pid]
        try:
            rewritten = rewrite_card(post, {"is_main_block": True})
        except Exception as exc:
            warnings.append(f"Главная новость #{pid}: {exc}")
            rewritten = {}
        main_block.append({
            "post_id": pid,
            "title": _safe_title(
                rewritten.get("title"),
                post,
                evidence=rewritten.get("evidence"),
                require_evidence=True,
            ),
            "image_file": post.get("image_file", ""),
            "link": post.get("link", ""),
        })
        used_ids.add(pid)

    if len(main_block) < MAIN_BLOCK_SIZE:
        warnings.append(
            f"В блоке «ГЛАВНОЕ» {len(main_block)} новостей: "
            f"для нормы {MAIN_BLOCK_SIZE} не хватает содержательных источников."
        )

    # === ГЛАВНАЯ ЦИФРА: ТОЛЬКО ДОСЛОВНОЕ ЧИСЛО ИЗ ИСХОДНИКА ===
    main_figure = None
    fig_id = _coerce_id(plan.get("main_figure_post_id"))
    if (
        fig_id not in by_id
        or fig_id in used_ids
        or not by_id[fig_id].get("has_number")
        or not by_id[fig_id].get("number_value")
    ):
        fig_id = next(
            (
                p["post_id"]
                for p in ranked
                if p["post_id"] not in used_ids
                and p.get("has_number")
                and p.get("number_value")
            ),
            None,
        )

    if fig_id:
        post = by_id[fig_id]
        raw_figure: Dict[str, Any] = {}
        try:
            raw_figure = rewrite_card(post, {"is_figure": True})
        except Exception as exc:
            warnings.append(f"Главная цифра #{fig_id}: {exc}")

        value = _plain_text(raw_figure.get("value") or post.get("number_value"))
        if not value or not _numbers_are_grounded(value, post):
            value = _plain_text(post.get("number_value"))
        if value:
            main_figure = {
                "value": value,
                "description": _safe_figure_description(
                    raw_figure.get("description") or post.get("number_desc"),
                    post,
                    evidence=raw_figure.get("evidence"),
                    require_evidence=True,
                ),
                "post_id": fig_id,
                "link": post.get("link", ""),
            }
            used_ids.add(fig_id)

    # === ГЛАВНОЕ ВИДЕО ===
    main_video = None
    vid_id = _coerce_id(plan.get("main_video_post_id"))
    if vid_id not in by_id or vid_id in used_ids or not by_id[vid_id].get("is_video"):
        vid_id = next(
            (
                p["post_id"]
                for p in ranked
                if p["post_id"] not in used_ids and p.get("is_video")
            ),
            None,
        )

    if vid_id:
        post = by_id[vid_id]
        try:
            rewritten = rewrite_card(post, {"is_video": True})
        except Exception as exc:
            warnings.append(f"Главное видео #{vid_id}: {exc}")
            rewritten = {}
        main_video = {
            "title": "📹 ГЛАВНОЕ ВИДЕО",
            "text": _safe_card_text(
                rewritten.get("text"),
                post,
                evidence=rewritten.get("evidence"),
                require_evidence=True,
            ),
            "image_file": post.get("image_file", ""),
            "link": post.get("link", ""),
            "post_id": vid_id,
        }
        used_ids.add(vid_id)

    # === ЦИТАТА: НЕ ПЕРЕФРАЗИРУЕМ ПРЯМУЮ РЕЧЬ ===
    main_quote = None
    main_quote_rubric = None
    quote_id = _coerce_id(plan.get("main_quote_post_id"))
    if quote_id not in by_id or quote_id in used_ids or not _grounded_quote(by_id[quote_id]):
        quote_id = next(
            (
                p["post_id"]
                for p in ranked
                if p["post_id"] not in used_ids and _grounded_quote(p)
            ),
            None,
        )

    if quote_id:
        post = by_id[quote_id]
        source = _source_text(post).lower()
        author_name = _plain_text(post.get("quote_author_name"))
        author_role = _plain_text(post.get("quote_author_role"))
        if author_name and author_name.lower() not in source:
            author_name = ""
        if author_role and author_role.lower() not in source:
            author_role = ""
        main_quote = {
            "text": _grounded_quote(post) or "",
            "author_name": author_name,
            "author_role": author_role,
            "photo_file": post.get("image_file", ""),
            "post_id": quote_id,
        }
        main_quote_rubric = post.get("rubric_candidate")
        used_ids.add(quote_id)

    # === РУБРИКИ: ТОЛЬКО rubric_candidate, БЕЗ ПРИНУДИТЕЛЬНЫХ ПЕРЕНОСОВ ===
    rubrics_skeleton = [
        {
            "name": rubric_name,
            "icon": RUBRIC_ICONS.get(rubric_name, "rubric_events.png"),
            "cards": [],
            "quote_before": None,
        }
        for rubric_name in NEWS_RUBRICS
    ]
    rubric_index = {r["name"]: idx for idx, r in enumerate(rubrics_skeleton)}
    rewrite_tasks = []

    for post in ranked:
        pid = post["post_id"]
        if pid in used_ids:
            continue
        rubric_name = _normalise_rubric(post.get("rubric_candidate"), post)
        r_idx = rubric_index[rubric_name]
        card_idx = len(rubrics_skeleton[r_idx]["cards"])
        rubrics_skeleton[r_idx]["cards"].append(None)
        rewrite_tasks.append({
            "rubric_idx": r_idx,
            "card_idx": card_idx,
            "pid": pid,
            "position": card_idx + 1,
            "rubric_name": rubric_name,
        })
        used_ids.add(pid)

    def _rewrite_one(task):
        post = by_id[task["pid"]]
        error = None
        try:
            rewritten = rewrite_card(post, {
                "rubric": task["rubric_name"],
                "position_in_rubric": task["position"],
            })
        except Exception as exc:
            error = str(exc)
            rewritten = {}

        return task, {
            "title": _safe_title(
                rewritten.get("title"),
                post,
                evidence=rewritten.get("evidence"),
                require_evidence=True,
            ),
            "text": _safe_card_text(
                rewritten.get("text"),
                post,
                evidence=rewritten.get("evidence"),
                require_evidence=True,
            ),
        }, error

    if rewrite_tasks:
        if progress:
            progress(0, len(rewrite_tasks), f"Редактура карточек 0/{len(rewrite_tasks)}")
        with ThreadPoolExecutor(max_workers=LLM_PARALLEL_WORKERS) as executor:
            futures = [executor.submit(_rewrite_one, task) for task in rewrite_tasks]
            done = 0
            for future in as_completed(futures):
                task, rewritten, error = future.result()
                done += 1
                if progress:
                    progress(
                        done,
                        len(rewrite_tasks),
                        f"Редактура карточек {done}/{len(rewrite_tasks)}",
                    )
                if error:
                    warnings.append(
                        f"Карточка #{task['pid']}: модель не ответила ({error}); "
                        "использован текст исходника."
                    )
                post = by_id[task["pid"]]
                position = task["position"]
                has_image = bool(post.get("image_file")) and position % 2 == 1
                rubrics_skeleton[task["rubric_idx"]]["cards"][task["card_idx"]] = {
                    "post_id": task["pid"],
                    "title": rewritten["title"],
                    "text": rewritten["text"],
                    "image_file": post.get("image_file", "") if has_image else "",
                    "has_image": has_image,
                    "position": position,
                    "link": post.get("link", ""),
                }

    # Цитата закрепляется за фактической рубрикой источника.
    if main_quote and main_quote_rubric in rubric_index:
        target = rubrics_skeleton[rubric_index[main_quote_rubric]]
        target["quote_before"] = 1 if target["cards"] else None

    video_after_rubric_idx = 0
    if main_video:
        video_after_rubric_idx = rubric_index.get("ПРОИЗВОДСТВО", 0) + 1

    # Не скрываем естественный дисбаланс и не исправляем его чужими новостями.
    for rubric in rubrics_skeleton:
        count = len(rubric["cards"])
        if count < RUBRIC_TARGET_MIN:
            warnings.append(
                f"«{rubric['name']}»: {count} новостей после отбора. "
                "Чужие темы в рубрику не переносились."
            )
        elif count > RUBRIC_TARGET_MAX:
            warnings.append(
                f"«{rubric['name']}»: {count} новостей. Все сохранены, "
                "чтобы не потерять исходные материалы."
            )

    if excluded:
        warnings.append(
            f"Отфильтровано служебных, бытовых или дублирующих сообщений: {len(excluded)}."
        )

    # === ФИНАЛЬНАЯ ПРОВЕРКА ===
    eligible_ids = {p["post_id"] for p in eligible}
    final_used = {item["post_id"] for item in main_block}
    for special in (main_figure, main_video, main_quote):
        if special and special.get("post_id"):
            final_used.add(special["post_id"])
    for rubric in rubrics_skeleton:
        for card in rubric.get("cards", []) or []:
            if card and card.get("post_id"):
                final_used.add(card["post_id"])

    really_missing = eligible_ids - final_used
    if really_missing:
        warnings.append(
            f"Не размещено содержательных постов: {sorted(really_missing)}."
        )

    location_by_id = {}
    location_by_id.update({pid: "/ ГЛАВНОЕ /" for pid in main_ids})
    if main_figure:
        location_by_id[main_figure["post_id"]] = "ГЛАВНАЯ ЦИФРА"
    if main_video:
        location_by_id[main_video["post_id"]] = "ГЛАВНОЕ ВИДЕО"
    if main_quote:
        location_by_id[main_quote["post_id"]] = f"ЦИТАТА · {main_quote_rubric}"
    for rubric in rubrics_skeleton:
        for card in rubric["cards"]:
            if card:
                location_by_id[card["post_id"]] = rubric["name"]

    routing = []
    for post in classified:
        is_excluded = not post.get("include_in_digest", True)
        manual = bool(post.get("rubric"))
        basis = post.get("rubric_basis")
        routing.append({
            "post_id": post["post_id"],
            "title": post.get("title") or _plain_text(post.get("text"))[:80],
            "rubric": "ИСКЛЮЧЕНО" if is_excluded else location_by_id.get(post["post_id"], "НЕ РАЗМЕЩЕНО"),
            "confidence": round(
                100 * (1.0 if manual else post.get("rubric_confidence", 0.5))
            ),
            "note": (
                post.get("exclude_reason") or "отфильтровано"
                if is_excluded
                else "задано в Excel"
                if manual
                else "однозначный тематический признак"
                if basis == "rule"
                else ""
            ),
        })

    # Тема письма тоже строится из исходных заголовков, а не из свободного пересказа.
    topics = [
        _plain_text(by_id[pid].get("title") or by_id[pid].get("text"))[:60]
        for pid in main_ids
    ]

    return {
        "subject_topics": topics,
        "main_block": main_block,
        "main_figure": main_figure,
        "main_video": main_video,
        "main_quote": main_quote,
        "main_quote_rubric": main_quote_rubric,
        "rubrics": rubrics_skeleton,
        "video_after_rubric_idx": video_after_rubric_idx,
        "warnings": warnings,
        "_classified": classified,
        "_plan": plan,
        "_routing": routing,
        "_excluded": [
            {
                "post_id": p["post_id"],
                "title": p.get("title", ""),
                "reason": p.get("exclude_reason", ""),
            }
            for p in excluded
        ],
        "_stats": {
            "input_posts": len(classified),
            "included_posts": len(eligible),
            "excluded_posts": len(excluded),
            "placed_posts": len(final_used),
            "per_rubric": {
                rubric["name"]: len(rubric["cards"])
                for rubric in rubrics_skeleton
            },
        },
    }

def _canonical_index(rubric_name: str) -> int:
    """Индекс рубрики в каноническом порядке. Неизвестные — в конец."""
    try:
        return CANONICAL_RUBRIC_ORDER.index(rubric_name)
    except ValueError:
        return 999


# ===========================================================================
# ПЕРЕГЕНЕРАЦИЯ ОДНОЙ КАРТОЧКИ (для UI «обновить заголовок»)
# ===========================================================================

def regenerate_single_card(
    draft: Dict[str, Any],
    rubric_idx: int,
    card_idx: int,
) -> Dict[str, Any]:
    """
    Перегенерирует одну карточку рубрики, не трогая остальные.
    Использует более высокую температуру для разнообразия.
    """
    classified = draft.get("_classified", [])
    by_id = {p["post_id"]: p for p in classified}

    rubric = draft["rubrics"][rubric_idx]
    card = rubric["cards"][card_idx]
    pid = card["post_id"]
    post = by_id.get(pid)
    if not post:
        raise ValueError(f"Пост #{pid} не найден")

    # Сбрасываем кэш ТОЛЬКО для этого вызова (через context-вариацию)
    import time
    context = {
        "rubric": rubric["name"],
        "position_in_rubric": card["position"],
        "_seed": int(time.time()),  # ломаем кэш
    }
    rewritten = rewrite_card(post, context)
    card["title"] = _safe_title(
        rewritten.get("title"),
        post,
        evidence=rewritten.get("evidence"),
        require_evidence=True,
    )
    card["text"] = _safe_card_text(
        rewritten.get("text"),
        post,
        evidence=rewritten.get("evidence"),
        require_evidence=True,
    )
    return card
