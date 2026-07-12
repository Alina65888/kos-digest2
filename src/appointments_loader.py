"""
Чтение Excel с людьми для дайджеста кадровых назначений + валидация.
Зеркалит src/excel_loader.py: понятные ошибки вместо stacktrace.
"""
import pandas as pd
from pathlib import Path
from typing import List, Dict, Any, Tuple

from .config import (
    APPOINTMENTS_REQUIRED_COLUMNS,
    APPOINTMENTS_ALL_KNOWN_COLUMNS,
    APPOINTMENT_SECTION_KEYS,
)
from .excel_loader import ExcelValidationError

COLUMN_ALIASES = {
    "фио": "name",
    "имя": "name",
    "новая должность": "new_position",
    "должность": "new_position",
    "предыдущая должность": "previous_position",
    "прежняя должность": "previous_position",
    "раздел": "section",
    "заметки": "notes",
    "заметка": "notes",
    "имя файла фото": "image_file",
    "фото": "image_file",
    "пенсия": "is_retirement",
    "уход на пенсию": "is_retirement",
    "порядок": "order",
}

SECTION_ALIASES = {
    "key": "key",
    "ключевые": "key",
    "ключевые назначения": "key",
    "в фокусе": "key",
    "в фокусе: ключевые назначения": "key",
    "new_faces": "new_faces",
    "новые лица": "new_faces",
    "новое лицо": "new_faces",
    "new_challenge": "new_challenge",
    "новый вызов": "new_challenge",
    "новый вызов в сибуре": "new_challenge",
    "departed": "departed",
    "ушли": "departed",
    "ушел": "departed",
    "ушёл": "departed",
    "кто ушел из команды": "departed",
    "кто ушёл из команды": "departed",
}

TRUE_VALUES = {"да", "true", "1", "yes", "истина"}


def load_people(xlsx_path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Читает Excel с людьми, возвращает (people, warnings).
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

    if len(df) == 0:
        raise ExcelValidationError(
            "Excel-файл пустой. Заполните строки с людьми и сохраните файл."
        )

    df.columns = [str(c).strip().lower() for c in df.columns]
    df.columns = [COLUMN_ALIASES.get(c, c) for c in df.columns]

    missing = [c for c in APPOINTMENTS_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ExcelValidationError(
            f"В Excel не хватает обязательных колонок: {', '.join(missing)}.\n"
            f"Должны быть: {', '.join(APPOINTMENTS_ALL_KNOWN_COLUMNS)}"
        )

    for col in APPOINTMENTS_ALL_KNOWN_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df = df.fillna("")

    warnings: List[str] = []
    people: List[Dict[str, Any]] = []

    for i, row in df.iterrows():
        row_idx = i + 2  # реальная строка в Excel с учётом заголовка
        name = str(row.get("name", "")).strip()
        if not name:
            warnings.append(f"Строка {row_idx}: пустое ФИО, пропускаю.")
            continue

        raw_section = str(row.get("section", "")).strip().lower()
        section = SECTION_ALIASES.get(raw_section)
        if section is None:
            warnings.append(
                f"Строка {row_idx} ({name}): неизвестный раздел '{raw_section}'. "
                f"Допустимые значения: {', '.join(sorted(set(SECTION_ALIASES.values())))}. "
                "Строка пропущена."
            )
            continue

        raw_order = row.get("order", "")
        try:
            order = int(raw_order) if str(raw_order).strip() != "" else row_idx
        except (TypeError, ValueError):
            order = row_idx

        person = {
            "row_idx": row_idx,
            "name": name,
            "new_position": str(row.get("new_position", "")).strip(),
            "previous_position": str(row.get("previous_position", "")).strip(),
            "section": section,
            "notes": str(row.get("notes", "")).strip(),
            "image_file": str(row.get("image_file", "")).strip(),
            "is_retirement": str(row.get("is_retirement", "")).strip().lower() in TRUE_VALUES,
            "order": order,
        }

        if not person["new_position"] and section != APPOINTMENT_SECTION_KEYS[-1]:
            warnings.append(f"Строка {row_idx} ({name}): не указана новая должность.")

        people.append(person)

    if not people:
        raise ExcelValidationError("После валидации не осталось ни одного человека.")

    return people, warnings
