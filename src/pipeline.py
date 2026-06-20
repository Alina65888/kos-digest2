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
)
from .llm_client import llm_json, load_prompt

log = logging.getLogger(__name__)


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
            return {item["id"]: item for item in items if "id" in item}
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
    """Безопасный фолбэк, если LLM упала или вернула не тот id"""
    return {
        "topic": post.get("title") or post.get("text", "")[:80],
        "rubric_candidate": "СОБЫТИЯ",
        "importance": 5,
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
            "topic": p.get("topic", ""),
            "summary_short": (p.get("summary_short") or "")[:300],
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
            "text": (post.get("text") or "")[:2000],
            "author": post.get("author", ""),
            "summary_short": post.get("summary_short", ""),
            "quote_text": post.get("quote_text"),
            "quote_author_name": post.get("quote_author_name"),
            "quote_author_role": post.get("quote_author_role"),
            "number_value": post.get("number_value"),
            "number_desc": post.get("number_desc"),
        },
        "context": context,
    }
    return llm_json(
        system_prompt,
        json.dumps(payload, ensure_ascii=False),
        temperature=REWRITE_TEMPERATURE,
        label="rewrite",
    )


# ===========================================================================
# ОРКЕСТРАЦИЯ — ВСЕ ТРИ ШАГА
# ===========================================================================

def build_digest_draft(
    posts: List[Dict[str, Any]],
    progress: Optional[Callable] = None,
) -> Dict[str, Any]:
    """
    Полный пайплайн. Возвращает структурированный draft, готовый к рендерингу.
    """
    warnings: List[str] = []

    # Присваиваем сквозные id (1-based)
    for i, p in enumerate(posts, start=1):
        p["post_id"] = i

    # ШАГ 1
    classified = classify_posts(posts, progress=progress)

    # ШАГ 2
    plan = plan_digest(classified, progress=progress)

    by_id = {p["post_id"]: p for p in classified}

    # Множества «занятых» постов — чтобы избежать дублирования
    used_ids = set()

    # === ГЛАВНОЕ ===
    main_block = []
    for item in plan.get("main_block", []):
        pid = item.get("post_id")
        post = by_id.get(pid)
        if not post or pid in used_ids:
            continue
        used_ids.add(pid)
        main_block.append({
            "post_id": pid,
            "title": item.get("title") or post.get("topic", ""),
            "image_file": post.get("image_file", ""),
        })

    # === ГЛАВНАЯ ЦИФРА ===
    main_figure = None
    fig_id = plan.get("main_figure_post_id")
    if fig_id and fig_id in by_id and fig_id not in used_ids:
        used_ids.add(fig_id)
        post = by_id[fig_id]
        try:
            rewritten = rewrite_card(post, {"is_figure": True})
            main_figure = {
                "value": rewritten.get("value") or post.get("number_value") or "",
                "description": rewritten.get("description") or post.get("number_desc") or "",
                "post_id": fig_id,
            }
        except Exception as e:
            warnings.append(f"Главная цифра: {e}")

    # === ГЛАВНОЕ ВИДЕО ===
    main_video = None
    vid_id = plan.get("main_video_post_id")
    if vid_id and vid_id in by_id and vid_id not in used_ids:
        used_ids.add(vid_id)
        post = by_id[vid_id]
        try:
            rewritten = rewrite_card(post, {"is_video": True})
            main_video = {
                "title": rewritten.get("title") or "📹 ГЛАВНОЕ ВИДЕО",
                "text": rewritten.get("text") or post.get("summary_short", ""),
                "image_file": post.get("image_file", ""),
                "post_id": vid_id,
            }
        except Exception as e:
            warnings.append(f"Главное видео: {e}")

    # === ЦИТАТА ===
    main_quote = None
    main_quote_rubric = plan.get("main_quote_rubric")  # имя рубрики для цитаты
    q_id = plan.get("main_quote_post_id")
    if q_id and q_id in by_id and q_id not in used_ids:
        used_ids.add(q_id)
        post = by_id[q_id]
        try:
            rewritten = rewrite_card(post, {"is_quote": True})
            main_quote = {
                "text": rewritten.get("quote_text") or post.get("quote_text", ""),
                "author_name": rewritten.get("author_name") or post.get("quote_author_name", ""),
                "author_role": rewritten.get("author_role") or post.get("quote_author_role", ""),
                "photo_file": post.get("image_file", ""),
                "post_id": q_id,
            }
            # Если рубрика для цитаты не указана — определяем по rubric_candidate поста
            if not main_quote_rubric:
                main_quote_rubric = post.get("rubric_candidate", "ДОСТИЖЕНИЯ")
        except Exception as e:
            warnings.append(f"Цитата: {e}")
            main_quote = None
    else:
        reason = plan.get("no_quote_reason") or "нет подходящего интервью"
        warnings.append(f"⚠ Блок цитаты пустой: {reason}. Можно добавить вручную через интерфейс.")

    # === РУБРИКИ ===
    # Соберём все задачи на перепись в одну очередь и прогоним параллельно
    rewrite_tasks = []  # [(rubric_idx, card_idx, pid, context), ...]
    rubrics_skeleton = []  # сначала пустые контейнеры, потом заполним

    for r_data in plan.get("rubrics", []):
        rubric_name = r_data.get("name", "СОБЫТИЯ")
        post_ids = r_data.get("post_ids", []) or []
        # Фильтруем уже использованные
        post_ids = [pid for pid in post_ids if pid in by_id and pid not in used_ids]
        used_ids.update(post_ids)

        cards_skeleton = [None] * len(post_ids)
        rubric_obj = {
            "name": rubric_name,
            "icon": RUBRIC_ICONS.get(rubric_name, "rubric_events.png"),
            "cards": cards_skeleton,
            "quote_before": r_data.get("quote_position"),
            "post_ids": post_ids,
        }
        rubrics_skeleton.append(rubric_obj)

        for c_idx, pid in enumerate(post_ids):
            rewrite_tasks.append({
                "rubric_idx": len(rubrics_skeleton) - 1,
                "card_idx": c_idx,
                "pid": pid,
                "position": c_idx + 1,
                "rubric_name": rubric_name,
            })

    # Параллельная перепись карточек
    def _rewrite_one(task):
        pid = task["pid"]
        post = by_id[pid]
        try:
            r = rewrite_card(post, {
                "rubric": task["rubric_name"],
                "position_in_rubric": task["position"],
            })
            return task, r, None
        except Exception as e:
            return task, None, str(e)

    if rewrite_tasks:
        if progress:
            progress(0, len(rewrite_tasks), f"Перепись карточек 0/{len(rewrite_tasks)}")
        with ThreadPoolExecutor(max_workers=LLM_PARALLEL_WORKERS) as ex:
            futures = [ex.submit(_rewrite_one, t) for t in rewrite_tasks]
            done = 0
            for f in as_completed(futures):
                task, rewritten, err = f.result()
                done += 1
                if progress:
                    progress(done, len(rewrite_tasks),
                             f"Перепись карточек {done}/{len(rewrite_tasks)}")

                pid = task["pid"]
                post = by_id[pid]
                if err:
                    warnings.append(f"Карточка #{pid}: {err}")
                    # Сделаем плейсхолдер
                    rewritten = {
                        "title": (post.get("title") or post.get("topic") or "")[:60].upper(),
                        "text": (post.get("summary_short") or post.get("text") or "")[:200],
                    }
                pos = task["position"]
                has_image = bool(post.get("image_file")) and (pos % 2 == 1)
                rubrics_skeleton[task["rubric_idx"]]["cards"][task["card_idx"]] = {
                    "post_id": pid,
                    "title": rewritten.get("title", ""),
                    "text": rewritten.get("text", ""),
                    "image_file": post.get("image_file", "") if has_image else "",
                    "has_image": has_image,
                    "position": pos,
                }

    # === ПЕРЕСОРТИРОВКА РУБРИК В КАНОНИЧЕСКОМ ПОРЯДКЕ ===
    rubrics_skeleton.sort(key=lambda r: _canonical_index(r["name"]))

    # Уберём служебное поле post_ids
    for r in rubrics_skeleton:
        r.pop("post_ids", None)

    # === ПРИВЯЗКА ЦИТАТЫ ===
    # Если LLM указал main_quote_rubric — ставим цитату туда.
    # Если не указал — оставим quote_position, который LLM зашил в самой рубрике.
    if main_quote and main_quote_rubric:
        # Найдём рубрику и убедимся, что quote_position валиден
        for r in rubrics_skeleton:
            if r["name"] == main_quote_rubric:
                qb = r.get("quote_before")
                if not isinstance(qb, int) or not (1 <= qb <= len(r["cards"])):
                    # Поставим после первой карточки
                    r["quote_before"] = 1 if r["cards"] else None
                break
            else:
                # В других рубриках обнулим quote_before
                r["quote_before"] = None

    # === ВИДЕО — после рубрики ПРОИЗВОДСТВО, если она есть ===
    video_after_rubric_idx = 0
    if main_video:
        for idx, r in enumerate(rubrics_skeleton, start=1):
            if r["name"] == "ПРОИЗВОДСТВО":
                video_after_rubric_idx = idx
                break
        if video_after_rubric_idx == 0 and rubrics_skeleton:
            video_after_rubric_idx = 1

    # === ВАЛИДАЦИЯ ===
    if len(main_block) < 4:
        warnings.append(
            f"⚠ В блоке ГЛАВНОЕ только {len(main_block)} новостей из 4. "
            "Возможно, в выпуске мало постов с importance>=8."
        )

    return {
        "subject_topics": plan.get("subject_topics", []),
        "main_block": main_block,
        "main_figure": main_figure,
        "main_video": main_video,
        "main_quote": main_quote,
        "main_quote_rubric": main_quote_rubric,
        "rubrics": rubrics_skeleton,
        "video_after_rubric_idx": video_after_rubric_idx,
        "skipped": plan.get("skipped", []),
        "warnings": warnings,
        "_classified": classified,  # для возможной перегенерации одной карточки
        "_plan": plan,
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
    card["title"] = rewritten.get("title", card["title"])
    card["text"] = rewritten.get("text", card["text"])
    return card
