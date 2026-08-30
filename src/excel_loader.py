"""
Чтение Excel с постами + валидация.
Если что-то не так — выдаём ПОНЯТНУЮ человеческую ошибку, а не stacktrace.
"""
import pandas as pd
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import List, Dict, Any, Tuple
from .config import REQUIRED_COLUMNS, ALL_KNOWN_COLUMNS, DIGEST_WINDOW_DAYS


class ExcelValidationError(Exception):
    """Понятная ошибка для пользователя"""
    pass


def load_posts(xlsx_path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Читает Excel, возвращает (posts, warnings).
    Падает с ExcelValidationError, если файл невалидный.
    """
    xlsx_path = Path(xlsx_path)
    if not xlsx_path.exists():
        raise ExcelValidationError(f"Файл не найден: {xlsx_path}")

    try:
        df = pd.read_excel(xlsx_path, engine="openpyxl")
    except Exception as e:
        raise ExcelValidationError(
            f"Не удалось открыть {xlsx_path.name}: {e}\n\n"
            "Возможные причины:\n"
            "- Файл повреждён или не в формате .xlsx\n"
            "- Файл открыт в Excel (закройте и попробуйте снова)\n"
            "- Файл создан в старом формате .xls (пересохраните как .xlsx)"
        )

    # === ВАЛИДАЦИЯ ===

    # 1. Есть ли вообще данные?
    if len(df) == 0:
        raise ExcelValidationError(
            "Excel-файл пустой. Заполните строки с постами и сохраните файл."
        )

    # 2. Маппинг альтернативных названий колонок
    df.columns = [str(c).strip().lower() for c in df.columns]
    COLUMN_ALIASES = {
        "дата публикации поста": "date",
        "ссылка на пост": "link",
        "текст поста": "text",
        "текст оригинального поста": "text",
        "имя файла фото": "image_file",
        "автор поста": "author",
        "заголовок поста": "title",
    }
    df.columns = [COLUMN_ALIASES.get(c, c) for c in df.columns]

    # 3. Все ли обязательные колонки на месте?
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ExcelValidationError(
            f"В Excel не хватает обязательных колонок: {', '.join(missing)}.\n"
            f"Должны быть: {', '.join(ALL_KNOWN_COLUMNS)}\n\n"
            f"Также принимаются: Дата публикации поста, Ссылка на пост, Текст поста"
        )

    # 4. Предупредим о лишних колонках (но не падаем)
    warnings = []

    # 4. Добавим недостающие optional-колонки как пустые
    for col in ALL_KNOWN_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df = df.fillna("")

    # 5. Подсчёт постов с фото
    have_image = (df["image_file"].astype(str).str.strip() != "").sum()
    if have_image == 0:
        warnings.append(
            "⚠ Ни у одного поста не заполнена колонка image_file. "
            "Дайджест соберётся, но без фотографий. "
            "Укажите имена файлов из папки с картинками."
        )
    elif have_image < len(df) // 2:
        warnings.append(
            f"У {have_image} из {len(df)} постов есть фото. "
            "Это нормально, но проверьте, что важные посты с картинками."
        )

    # 6. Превращаем в список dict
    posts = []
    for i, row in df.iterrows():
        post = {
            "row_idx": i + 2,  # реальная строка в Excel (с учётом заголовка)
            "date": str(row.get("date", "")),
            "author": str(row.get("author", "")).strip(),
            "title": str(row.get("title", "")).strip(),
            "text": str(row.get("text", "")).strip(),
            "link": str(row.get("link", "")).strip(),
            "image_file": str(row.get("image_file", "")).strip(),
        }
        # Пустые тексты — пропускаем с предупреждением
        if not post["text"]:
            warnings.append(f"Строка {post['row_idx']}: пустой text, пропускаю.")
            continue
        posts.append(post)

    if not posts:
        raise ExcelValidationError("После фильтрации не осталось ни одного непустого поста.")

    return posts, warnings


def filter_posts_by_period(
    posts: List[Dict[str, Any]],
    digest_date: date | datetime,
    window_days: int = DIGEST_WINDOW_DAYS,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """Оставляет публикации за указанное окно, не теряя строки без даты.

    Даты считаются включительно: для выпуска 30 августа окно в 14 дней –
    с 17 по 30 августа. Строки с пустой или нераспознанной датой остаются в
    наборе, но пользователь получает предупреждение.
    """
    if isinstance(digest_date, datetime):
        end_date = digest_date.date()
    elif isinstance(digest_date, date):
        end_date = digest_date
    else:
        parsed_end = pd.to_datetime(digest_date, errors="coerce")
        if pd.isna(parsed_end):
            raise ExcelValidationError(f"Не удалось распознать дату выпуска: {digest_date}")
        end_date = parsed_end.date()

    start_date = end_date - timedelta(days=max(1, window_days) - 1)
    kept: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    warnings: List[str] = []
    undated_rows: List[int] = []
    invalid_rows: List[int] = []

    for post in posts:
        raw_date = str(post.get("date") or "").strip()
        if not raw_date:
            kept.append(post)
            undated_rows.append(int(post.get("row_idx") or 0))
            continue

        parsed = pd.to_datetime(raw_date, errors="coerce")
        if pd.isna(parsed):
            kept.append(post)
            invalid_rows.append(int(post.get("row_idx") or 0))
            continue

        post_date = parsed.date()
        post["date"] = post_date.isoformat()
        if start_date <= post_date <= end_date:
            kept.append(post)
        else:
            excluded.append({
                **post,
                "exclude_reason": (
                    f"Дата {post_date.strftime('%d.%m.%Y')} вне периода "
                    f"{start_date.strftime('%d.%m.%Y')}–{end_date.strftime('%d.%m.%Y')}"
                ),
            })

    if undated_rows:
        warnings.append(
            f"У {len(undated_rows)} пост(ов) не указана дата – они оставлены в отборе. "
            f"Строки: {', '.join(map(str, undated_rows[:8]))}."
        )
    if invalid_rows:
        warnings.append(
            f"У {len(invalid_rows)} пост(ов) дата не распознана – они оставлены в отборе. "
            f"Строки: {', '.join(map(str, invalid_rows[:8]))}."
        )
    if excluded:
        warnings.append(
            f"За пределами последних {window_days} дней исключено постов: {len(excluded)}."
        )

    return kept, excluded, warnings


def validate_images_dir(images_dir: Path, posts: List[Dict[str, Any]]) -> List[str]:
    """
    Проверяет, что все image_file из постов реально существуют в папке.
    Возвращает список предупреждений.
    """
    warnings = []
    images_dir = Path(images_dir)

    if not images_dir.exists():
        warnings.append(f"Папка с картинками не найдена: {images_dir}. Фото не будут добавлены.")
        return warnings

    # Карта файлов в папке (case-insensitive)
    files_in_dir = {f.name.lower(): f.name for f in images_dir.iterdir() if f.is_file()}

    missing = []
    for post in posts:
        img = post.get("image_file", "").strip()
        if not img:
            continue
        if img.lower() not in files_in_dir:
            missing.append(f"строка {post['row_idx']}: '{img}'")
        else:
            # Поправим регистр имени файла, если нужно
            real_name = files_in_dir[img.lower()]
            if real_name != img:
                post["image_file"] = real_name

    if missing:
        warnings.append(
            f"⚠ Не найдено в папке: {len(missing)} файл(ов). "
            f"Примеры: {'; '.join(missing[:3])}"
        )

    return warnings
