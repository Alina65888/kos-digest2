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
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Callable

from .config import (
    CLASSIFY_BATCH_SIZE,
    CLASSIFY_TEXT_LIMIT,
    CLASSIFY_TEMPERATURE,
    PLAN_TEMPERATURE,
    REWRITE_TEXT_LIMIT,
    REWRITE_TEMPERATURE,
    LLM_PARALLEL_WORKERS,
    CANONICAL_RUBRIC_ORDER,
    RUBRIC_ICONS,
    MAIN_BLOCK_SIZE,
    MAIN_BLOCK_RESERVED_RUBRICS,
    MIN_MAIN_IMPORTANCE,
    MIN_CARD_IMPORTANCE,
    MAX_CARDS_PER_RUBRIC,
)
from .llm_client import llm_json, load_prompt
from .editorial_rules import (
    canonicalize_rubric,
    extractive_summary,
    find_announcement_report_duplicates,
    normalize_classification,
    validate_rewrite_output,
    headline_options,
    source_links,
)
from .excel_loader import parse_post_date
from .timeliness import assess_timeliness

log = logging.getLogger(__name__)


# ===========================================================================
# ШАГ 1. КЛАССИФИКАЦИЯ (БАТЧАМИ)
# ===========================================================================

def classify_posts(
    posts: List[Dict[str, Any]],
    progress: Optional[Callable] = None,
    digest_date: date | str | None = None,
) -> List[Dict[str, Any]]:
    """
    Прогоняет все посты через LLM батчами по CLASSIFY_BATCH_SIZE.
    Возвращает посты с добавленными полями классификации.
    """
    if not posts:
        return []
    system_prompt = load_prompt("classify")
    result: Dict[int, Dict[str, Any]] = {}  # post_id → classification

    # Разбиваем на батчи
    total = len(posts)
    batches = [posts[i:i + CLASSIFY_BATCH_SIZE]
               for i in range(0, total, CLASSIFY_BATCH_SIZE)]

    def process_batch(batch_idx: int, batch: List[Dict[str, Any]]):
        payload = {
            "digest_date": str(digest_date or ""),
            "posts": [
                {
                    "id": p["post_id"],
                    "date": p.get("date", ""),
                    "author": p.get("author", ""),
                    "title": p.get("title", ""),
                    "text": p.get("text", "")[:CLASSIFY_TEXT_LIMIT],
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
            if not isinstance(items, list):
                raise ValueError("Классификация должна содержать список items")
            normalized_items = {}
            batch_ids = {p["post_id"] for p in batch}
            for item in items:
                try:
                    item_id = int(item.get("id"))
                except (TypeError, ValueError, AttributeError):
                    continue
                if item_id in batch_ids:
                    normalized_items[item_id] = item
            return normalized_items
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
        raw_cls = result.get(pid)
        if raw_cls is None:
            # Фолбэк: дефолтная классификация
            log.warning(f"Пост {pid}: классификация не получена, использую дефолт")
        cls = normalize_classification(post, raw_cls)
        # ВНИМАНИЕ: cls имеет приоритет НИЖЕ, чем оригинальные поля post.
        # Это гарантирует, что image_file/link/text из Excel не будут перетёрты.
        merged = {**cls, **post}
        # Гарантируем, что нужные поля есть с дефолтами, если LLM их пропустила
        merged.setdefault("topic", merged.get("title") or "(тема не определена)")
        merged.setdefault("rubric_candidate", "СОБЫТИЯ")
        merged.setdefault("importance", 5)
        merged.setdefault("has_number", False)
        merged.setdefault("has_quote", False)
        merged.setdefault("is_video", False)
        merged.setdefault("is_special", False)
        merged.setdefault("summary_short", post.get("text", "")[:300])
        classified.append(merged)

    return classified


def _default_classification(post: Dict[str, Any]) -> Dict[str, Any]:
    """Безопасный фолбэк: правила по словам вместо свалки в «СОБЫТИЯ»."""
    return normalize_classification(post, None)


# ===========================================================================
# ШАГ 2. ПЛАН ДАЙДЖЕСТА
# ===========================================================================

def plan_digest(
    classified: List[Dict[str, Any]],
    progress: Optional[Callable] = None,
    digest_date: date | str | None = None,
    pinned_main_ids: Optional[List[int]] = None,
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
            "author": p.get("author", ""),
            "topic": p.get("topic", ""),
            "summary_short": (p.get("summary_short") or "")[:600],
            "rubric_candidate": p.get("rubric_candidate"),
            "rubric_reason": p.get("rubric_reason", ""),
            "rubric_evidence": p.get("rubric_evidence", ""),
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
            "source_excerpt": (p.get("text") or "")[:700],
        }
        for p in classified
    ]

    plan = llm_json(
        system_prompt,
        json.dumps({"posts": compact, "digest_date": str(digest_date or ""),
                    "pinned_main_ids": pinned_main_ids or []}, ensure_ascii=False),
        temperature=PLAN_TEMPERATURE,
        label="plan",
    )

    # Некоторые совместимые API возвращают id строками. Приводим их здесь,
    # чтобы валидные решения не терялись дальше в пайплайне.
    for item in plan.get("main_block", []) or []:
        try:
            item["post_id"] = int(item.get("post_id"))
        except (TypeError, ValueError):
            item["post_id"] = None
    for key in ("main_figure_post_id", "main_video_post_id", "main_quote_post_id"):
        try:
            plan[key] = int(plan.get(key)) if plan.get(key) is not None else None
        except (TypeError, ValueError):
            plan[key] = None
    for rubric in plan.get("rubrics", []) or []:
        normalized_ids = []
        for value in rubric.get("post_ids", []) or []:
            try:
                normalized_ids.append(int(value))
            except (TypeError, ValueError):
                continue
        rubric["post_ids"] = normalized_ids
    for item in plan.get("skipped", []) or []:
        try:
            item["post_id"] = int(item.get("post_id"))
        except (TypeError, ValueError):
            item["post_id"] = None

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
            "date": post.get("date", ""),
            "text": (post.get("text") or "")[:REWRITE_TEXT_LIMIT],
            "author": post.get("author", ""),
            "summary_short": post.get("summary_short", ""),
            "quote_text": post.get("quote_text"),
            "quote_author_name": post.get("quote_author_name"),
            "quote_author_role": post.get("quote_author_role"),
            "number_value": post.get("number_value"),
            "number_desc": post.get("number_desc"),
            "source_links": source_links(post),
        },
        "context": context,
    }
    raw_result = llm_json(
        system_prompt,
        json.dumps(payload, ensure_ascii=False),
        temperature=REWRITE_TEMPERATURE,
        label="rewrite",
    )
    result, quality_flags = validate_rewrite_output(post, context, raw_result)
    if not context.get("is_quote") and not context.get("is_figure"):
        result["headline_options"] = headline_options(post, context, raw_result)
    result["_quality_flags"] = quality_flags
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

    def replace_a(match):
        attrs = match.group(1) or ""
        inner = match.group(2) or ""

        href_match = re.search(r'href\s*=\s*["\']([^"\']*)["\']', attrs)
        if href_match:
            current_href = href_match.group(1).strip()
            if not current_href or current_href == "#":
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
    *,
    digest_date: date | str | None = None,
    pinned_main_ids: Optional[List[int]] = None,
    excluded_post_ids: Optional[List[int]] = None,
    max_cards_per_rubric: int = MAX_CARDS_PER_RUBRIC,
    preserve_main_titles: bool = False,
) -> Dict[str, Any]:
    """Полный пайплайн с детерминированной проверкой решений модели."""
    warnings: List[str] = []

    posts = [dict(post) for post in posts]
    assigned = set()
    for i, post in enumerate(posts, start=1):
        pid = post.get("post_id", i)
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid in assigned:
            pid = i
            while pid in assigned:
                pid += 1
        post["post_id"] = pid
        assigned.add(pid)
    if not posts:
        raise ValueError("Нет постов для дайджеста")
    publication = parse_post_date(digest_date)
    pinned = list(dict.fromkeys(pinned_main_ids or []))
    if len(pinned) > MAIN_BLOCK_SIZE or any(pid not in assigned for pid in pinned):
        raise ValueError("Для «Главного» можно выбрать до четырех постов из загруженного списка")
    editor_excluded = set(excluded_post_ids or [])
    if editor_excluded.intersection(pinned):
        raise ValueError("Один пост нельзя одновременно закрепить и исключить")
    max_cards = max(1, min(10, int(max_cards_per_rubric)))

    # Актуальность проверяется до модели: исключенный анонс не возвращается
    # через план, главную цифру, резервный отбор или ошибку API.
    time_checks = {p["post_id"]: assess_timeliness(p, publication) for p in posts}
    pre_excluded = {
        pid: check["reason"] for pid, check in time_checks.items()
        if check["status"] == "expired"
    }
    pre_excluded.update({pid: "Исключено редактором" for pid in editor_excluded if pid in assigned})
    for pid in pinned:
        if pid in pre_excluded:
            warnings.append(f"Закрепленный пост #{pid} исключен: {pre_excluded[pid]}")
    pinned = [pid for pid in pinned if pid not in pre_excluded]

    active = [p for p in posts if p["post_id"] not in pre_excluded]
    classified = classify_posts(active, progress=progress, digest_date=publication) if active else []
    for post in classified:
        post["timeliness"] = time_checks[post["post_id"]]
        if post["timeliness"]["status"] == "review":
            warnings.append(f"Пост #{post['post_id']}: {post['timeliness']['reason']}")
    plan = plan_digest(classified, progress=progress, digest_date=publication, pinned_main_ids=pinned) if classified else {}
    classified.extend({**normalize_classification(p, None), **p, "timeliness": time_checks[p["post_id"]]}
                      for p in posts if p["post_id"] in pre_excluded)
    by_id = {p["post_id"]: p for p in classified}
    all_post_ids = set(by_id)

    # Анонс не возвращается в выпуск, если в наборе уже есть отчет о событии.
    excluded_reasons: Dict[int, str] = find_announcement_report_duplicates(classified)
    excluded_reasons.update(pre_excluded)

    # Решение модели «пропустить» учитываем, только если этот пост не был
    # одновременно выбран ею в конкретный блок.
    referenced_by_plan = set()
    for item in plan.get("main_block", []) or []:
        if item.get("post_id") in by_id:
            referenced_by_plan.add(item["post_id"])
    for key in ("main_figure_post_id", "main_video_post_id", "main_quote_post_id"):
        if plan.get(key) in by_id:
            referenced_by_plan.add(plan[key])
    for rubric in plan.get("rubrics", []) or []:
        referenced_by_plan.update(pid for pid in (rubric.get("post_ids") or []) if pid in by_id)
    for item in plan.get("skipped", []) or []:
        pid = item.get("post_id")
        if pid in by_id and pid not in referenced_by_plan and pid not in pinned:
            excluded_reasons.setdefault(pid, str(item.get("reason") or "Не вошел в редакторский отбор"))

    used_ids: set[int] = set(pinned)

    def choose_feature(requested_id, predicate) -> Optional[int]:
        if (
            requested_id in by_id
            and requested_id not in used_ids
            and requested_id not in excluded_reasons
            and predicate(by_id[requested_id])
        ):
            return requested_id
        candidates = [
            post for post in classified
            if post["post_id"] not in used_ids
            and post["post_id"] not in excluded_reasons
            and predicate(post)
        ]
        candidates.sort(key=lambda p: (p.get("importance", 0), p.get("date", "")), reverse=True)
        return candidates[0]["post_id"] if candidates else None

    # Сначала резервируем специальные блоки, чтобы один пост не дублировался.
    fig_id = choose_feature(plan.get("main_figure_post_id"), lambda p: bool(p.get("has_number")))
    if fig_id:
        used_ids.add(fig_id)
    vid_id = choose_feature(plan.get("main_video_post_id"), lambda p: bool(p.get("is_video")))
    if vid_id:
        used_ids.add(vid_id)
    q_id = choose_feature(plan.get("main_quote_post_id"), lambda p: bool(p.get("has_quote")))
    if q_id:
        used_ids.add(q_id)

    # === ГЛАВНОЕ: максимум четыре действительно сильных и разных поста ===
    plan_main_titles = {
        item.get("post_id"): str(item.get("title") or "").strip()
        for item in (plan.get("main_block", []) or [])
        if item.get("post_id") in by_id
    }
    main_order = pinned + [pid for pid in plan_main_titles if pid not in pinned]
    ranked = sorted(
        classified,
        key=lambda p: (p.get("importance", 0), bool(p.get("is_special")), p.get("date", "")),
        reverse=True,
    )
    main_order.extend(p["post_id"] for p in ranked if p["post_id"] not in main_order)

    main_block = []
    for pid in main_order:
        if len(main_block) >= MAIN_BLOCK_SIZE:
            break
        post = by_id.get(pid)
        if (
            not post
            or (pid in used_ids and pid not in pinned)
            or pid in excluded_reasons
            or (pid not in pinned and post.get("importance", 0) < MIN_MAIN_IMPORTANCE)
            or (pid not in pinned and canonicalize_rubric(post.get("rubric_candidate"))
                in MAIN_BLOCK_RESERVED_RUBRICS)
        ):
            continue
        main_context = {"is_main_block": True, "digest_date": str(publication or "")}
        try:
            # Заголовок из плана нужен для отбора, но не должен попадать в
            # готовый дайджест без редакторской обработки: именно здесь чаще
            # всего появлялись длинные протокольные формулировки.
            if preserve_main_titles and post.get("title"):
                checked = {"title": post["title"], "_quality_flags": []}
            else:
                checked = rewrite_card(post, main_context)
            title_flags = checked.get("_quality_flags", [])
        except Exception as exc:
            warnings.append(
                f"Главное, пост #{pid}: использован фактический заголовок из-за ошибки LLM ({exc})"
            )
            checked, title_flags = validate_rewrite_output(
                post,
                main_context,
                {
                    "title": plan_main_titles.get(pid) or post.get("title") or post.get("topic"),
                    "text": post.get("summary_short") or extractive_summary(post),
                },
            )
        main_block.append({
            "post_id": pid,
            "title": checked["title"],
            "image_file": post.get("image_file", ""),
            "link": post.get("link", ""),
            "quality_flags": title_flags,
            "headline_options": checked.get("headline_options", []),
        })
        used_ids.add(pid)

    if len(main_block) < MAIN_BLOCK_SIZE:
        warnings.append(
            f"В блоке «Главное» {len(main_block)} новостей: остальные посты не прошли порог важности."
        )

    # === ГЛАВНАЯ ЦИФРА – только подтвержденное значение из исходника ===
    main_figure = None
    if fig_id:
        post = by_id[fig_id]
        try:
            rewritten = rewrite_card(post, {"is_figure": True, "digest_date": str(publication or "")})
        except Exception as exc:
            warnings.append(f"Главная цифра, пост #{fig_id}: {exc}")
            rewritten, fallback_flags = validate_rewrite_output(post, {"is_figure": True}, {})
            rewritten["_quality_flags"] = fallback_flags
        if rewritten.get("value"):
            main_figure = {
                "value": rewritten["value"],
                "description": rewritten.get("description") or post.get("number_desc") or "",
                "post_id": fig_id,
                "link": post.get("link", ""),
                "quality_flags": rewritten.get("_quality_flags", []),
            }

    # === ГЛАВНОЕ ВИДЕО ===
    main_video = None
    if vid_id:
        post = by_id[vid_id]
        try:
            rewritten = rewrite_card(post, {"is_video": True, "digest_date": str(publication or "")})
        except Exception as exc:
            warnings.append(f"Главное видео, пост #{vid_id}: {exc}")
            rewritten, fallback_flags = validate_rewrite_output(post, {"is_video": True}, {})
            rewritten["_quality_flags"] = fallback_flags
        main_video = {
            "title": rewritten.get("title") or post.get("title") or "Главное видео",
            "text": rewritten.get("text") or extractive_summary(post),
            "image_file": post.get("image_file", ""),
            "link": post.get("link", ""),
            "post_id": vid_id,
            "quality_flags": rewritten.get("_quality_flags", []),
            "headline_options": rewritten.get("headline_options", []),
        }

    # === ЦИТАТА – дословно из исходника ===
    main_quote = None
    main_quote_rubric = None
    if q_id:
        post = by_id[q_id]
        try:
            rewritten = rewrite_card(post, {"is_quote": True})
        except Exception as exc:
            warnings.append(f"Цитата, пост #{q_id}: {exc}")
            rewritten, fallback_flags = validate_rewrite_output(post, {"is_quote": True}, {})
            rewritten["_quality_flags"] = fallback_flags
        if rewritten.get("quote_text"):
            main_quote = {
                "text": rewritten["quote_text"],
                "author_name": rewritten.get("author_name") or "",
                "author_role": rewritten.get("author_role") or "",
                "photo_file": post.get("image_file", ""),
                "post_id": q_id,
                "quality_flags": rewritten.get("_quality_flags", []),
            }
            main_quote_rubric = post.get("rubric_candidate")

    # === РУБРИКИ: принадлежность определяет классификация, а не желание заполнить сетку ===
    plan_rank: Dict[int, int] = {}
    rank = 0
    for rubric in plan.get("rubrics", []) or []:
        for pid in rubric.get("post_ids", []) or []:
            if pid in by_id and pid not in plan_rank:
                plan_rank[pid] = rank
                rank += 1

    grouped: Dict[str, List[Dict[str, Any]]] = {name: [] for name in CANONICAL_RUBRIC_ORDER}
    for post in classified:
        pid = post["post_id"]
        if pid in used_ids or pid in excluded_reasons:
            continue
        if post.get("importance", 0) < MIN_CARD_IMPORTANCE:
            excluded_reasons.setdefault(pid, f"Низкая значимость: {post.get('importance', 0)}/10")
            continue
        rubric_name = canonicalize_rubric(post.get("rubric_candidate")) or "СОБЫТИЯ"
        grouped[rubric_name].append(post)

    selected_by_rubric: Dict[str, List[Dict[str, Any]]] = {}
    for rubric_name, rubric_posts in grouped.items():
        rubric_posts.sort(
            key=lambda p: (
                -int(p.get("importance", 0)),
                plan_rank.get(p["post_id"], 10_000),
                str(p.get("date", "")),
            )
        )
        selected = rubric_posts[:max_cards]
        selected_by_rubric[rubric_name] = selected
        for overflow in rubric_posts[max_cards:]:
            excluded_reasons.setdefault(
                overflow["post_id"],
                f"В рубрике уже выбраны {max_cards} более значимые новости",
            )

    rubrics_skeleton: List[Dict[str, Any]] = []
    rewrite_tasks: List[Dict[str, Any]] = []
    for rubric_name in CANONICAL_RUBRIC_ORDER:
        selected = selected_by_rubric.get(rubric_name) or []
        if not selected and not (main_quote and rubric_name == main_quote_rubric):
            continue
        rubric_obj = {
            "name": rubric_name,
            "icon": RUBRIC_ICONS.get(rubric_name, "rubric_events.png"),
            "cards": [None] * len(selected),
            "quote_before": None,
        }
        rubric_idx = len(rubrics_skeleton)
        rubrics_skeleton.append(rubric_obj)
        for card_idx, post in enumerate(selected):
            rewrite_tasks.append({
                "rubric_idx": rubric_idx,
                "card_idx": card_idx,
                "pid": post["post_id"],
                "position": card_idx + 1,
                "rubric_name": rubric_name,
            })

    def _rewrite_one(task):
        post = by_id[task["pid"]]
        context = {
            "rubric": task["rubric_name"],
            "position_in_rubric": task["position"],
            "digest_date": str(publication or ""),
        }
        try:
            rewritten = rewrite_card(post, context)
            return task, rewritten, None
        except Exception as exc:
            fallback, fallback_flags = validate_rewrite_output(post, context, {})
            fallback["_quality_flags"] = fallback_flags
            return task, fallback, str(exc)

    if rewrite_tasks:
        if progress:
            progress(0, len(rewrite_tasks), f"Перепись карточек 0/{len(rewrite_tasks)}")
        with ThreadPoolExecutor(max_workers=LLM_PARALLEL_WORKERS) as executor:
            futures = [executor.submit(_rewrite_one, task) for task in rewrite_tasks]
            done = 0
            for future in as_completed(futures):
                task, rewritten, error = future.result()
                done += 1
                if progress:
                    progress(done, len(rewrite_tasks), f"Перепись карточек {done}/{len(rewrite_tasks)}")
                pid = task["pid"]
                post = by_id[pid]
                if error:
                    warnings.append(f"Карточка #{pid}: использован фактический текст из-за ошибки LLM ({error})")
                position = task["position"]
                has_image = bool(post.get("image_file")) and position % 2 == 1
                rubrics_skeleton[task["rubric_idx"]]["cards"][task["card_idx"]] = {
                    "post_id": pid,
                    "title": rewritten.get("title") or str(post.get("title") or "").upper(),
                    "text": rewritten.get("text") or extractive_summary(post),
                    "image_file": post.get("image_file", "") if has_image else "",
                    "has_image": has_image,
                    "position": position,
                    "link": post.get("link", ""),
                    "quality_flags": rewritten.get("_quality_flags", []),
                    "headline_options": rewritten.get("headline_options", []),
                }
                used_ids.add(pid)

    # Цитата может быть единственным материалом своей рубрики: тогда она
    # ставится сразу после заголовка раздела (позиция 0).
    if main_quote and main_quote_rubric:
        for rubric in rubrics_skeleton:
            rubric["quote_before"] = None
        target = next((r for r in rubrics_skeleton if r["name"] == main_quote_rubric), None)
        if target:
            target["quote_before"] = 1 if target["cards"] else 0

    video_after_rubric_idx = 0
    if main_video and rubrics_skeleton:
        for idx, rubric in enumerate(rubrics_skeleton, start=1):
            if rubric["name"] == "ПРОИЗВОДСТВО" and rubric.get("cards"):
                video_after_rubric_idx = idx
                break
        if not video_after_rubric_idx:
            video_after_rubric_idx = next(
                (idx for idx, rubric in enumerate(rubrics_skeleton, start=1) if rubric.get("cards")),
                1,
            )

    # Независимые генерации могут предложить один и тот же каламбур.
    # Выбираем следующую уже проверенную альтернативу без нового API-вызова.
    seen_titles = {str(item["title"]).casefold() for item in main_block}
    if main_video:
        seen_titles.add(str(main_video["title"]).casefold())
    for rubric in rubrics_skeleton:
        for card in rubric.get("cards") or []:
            if str(card["title"]).casefold() in seen_titles:
                alternative = next((opt for opt in card.get("headline_options", [])
                                    if str(opt["title"]).casefold() not in seen_titles), None)
                if alternative:
                    card["title"] = alternative["title"]
                else:
                    card.setdefault("quality_flags", []).append("Заголовок повторяется в выпуске; выберите другой")
            seen_titles.add(str(card["title"]).casefold())

    final_used = {item["post_id"] for item in main_block}
    for block in (main_figure, main_video, main_quote):
        if block and block.get("post_id"):
            final_used.add(block["post_id"])
    for rubric in rubrics_skeleton:
        for card in rubric.get("cards") or []:
            if card:
                final_used.add(card["post_id"])

    for pid in sorted(all_post_ids - final_used):
        excluded_reasons.setdefault(pid, "Не вошел в число наиболее значимых новостей выпуска")

    excluded = [
        {
            "post_id": pid,
            "title": by_id[pid].get("title") or by_id[pid].get("topic") or f"Пост #{pid}",
            "rubric": by_id[pid].get("rubric_candidate"),
            "importance": by_id[pid].get("importance", 0),
            "reason": excluded_reasons[pid],
        }
        for pid in sorted(all_post_ids - final_used)
    ]

    subject_topics = [
        str(topic).strip() for topic in (plan.get("subject_topics") or [])
        if str(topic).strip()
    ][:3]
    if not subject_topics:
        subject_topics = [
            str(by_id[item["post_id"]].get("topic") or item["title"]).strip()
            for item in main_block[:3]
        ]

    quality_flag_count = sum(
        len(item.get("quality_flags") or []) for item in main_block
    )
    quality_flag_count += sum(
        len((block or {}).get("quality_flags") or [])
        for block in (main_figure, main_video, main_quote)
    )
    quality_flag_count += sum(
        len(card.get("quality_flags") or [])
        for rubric in rubrics_skeleton
        for card in (rubric.get("cards") or [])
        if card
    )

    return {
        "digest_date": str(publication or ""),
        "subject_topics": subject_topics,
        "main_block": main_block,
        "main_figure": main_figure,
        "main_video": main_video,
        "main_quote": main_quote,
        "main_quote_rubric": main_quote_rubric,
        "rubrics": rubrics_skeleton,
        "video_after_rubric_idx": video_after_rubric_idx,
        "warnings": warnings,
        "excluded": excluded,
        "_classified": classified,
        "_plan": plan,
        "_stats": {
            "input_posts": len(all_post_ids),
            "placed_posts": len(final_used),
            "excluded_posts": len(excluded),
            "quality_flags": quality_flag_count,
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
        "digest_date": draft.get("digest_date", ""),
        "avoid_titles": [card["title"]],
    }
    rewritten = rewrite_card(post, context)
    card["title"] = rewritten.get("title", card["title"])
    card["text"] = rewritten.get("text", card["text"])
    card["quality_flags"] = rewritten.get("_quality_flags", [])
    card["headline_options"] = rewritten.get("headline_options", [])
    return card


def regenerate_card_headline(
    draft: Dict[str, Any],
    rubric_idx: int,
    card_idx: int,
) -> Dict[str, Any]:
    """Конструирует новый заголовок, не меняя уже согласованную подводку."""
    classified = draft.get("_classified", [])
    by_id = {p["post_id"]: p for p in classified}

    rubric = draft["rubrics"][rubric_idx]
    card = rubric["cards"][card_idx]
    pid = card["post_id"]
    post = by_id.get(pid)
    if not post:
        raise ValueError(f"Пост #{pid} не найден")

    import time
    context = {
        "rubric": rubric["name"],
        "position_in_rubric": card["position"],
        "title_only": True,
        "digest_date": draft.get("digest_date", ""),
        "approved_lead": card.get("text", ""),
        "avoid_titles": [card["title"]] + [opt["title"] for opt in card.get("headline_options", [])],
        "_seed": time.time_ns(),
    }
    rewritten = rewrite_card(post, context)
    card["title"] = rewritten.get("title", card["title"])
    card["quality_flags"] = rewritten.get("_quality_flags", [])
    card["headline_options"] = rewritten.get("headline_options", [])
    return card
