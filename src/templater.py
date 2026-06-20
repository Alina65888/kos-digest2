"""
Сборка финального .htm дайджеста.

Делает три вещи:
  1. Кадрирует/ресайзит все картинки под слоты шаблона
  2. Копирует иконки и обработанные фото в digest_YYYY-MM-DD.files/
  3. Рендерит digest_template.html через Jinja2

Совместимо с Outlook: табличная вёрстка, inline-стили.
"""
import shutil
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Tuple
from jinja2 import Environment, FileSystemLoader
from PIL import Image

from .config import (
    TEMPLATES_DIR, ASSETS_DIR,
    SIZE_MAIN, SIZE_CARD, SIZE_VIDEO, SIZE_PHOTO,
)

log = logging.getLogger(__name__)


def _fit_image(src: Path, dst: Path, size: Tuple[int, int]) -> bool:
    """
    Кадрирует по центру под нужное соотношение и ресайзит.
    Сохраняет в JPEG с высоким качеством.
    """
    try:
        with Image.open(src) as img:
            # Конвертируем «странные» режимы в RGB
            if img.mode in ("RGBA", "P", "CMYK", "LA"):
                img = img.convert("RGB")

            target_w, target_h = size
            target_ratio = target_w / target_h
            src_w, src_h = img.size
            src_ratio = src_w / src_h

            # Center-crop под целевое соотношение
            if src_ratio > target_ratio:
                new_w = int(src_h * target_ratio)
                offset = (src_w - new_w) // 2
                img = img.crop((offset, 0, offset + new_w, src_h))
            elif src_ratio < target_ratio:
                new_h = int(src_w / target_ratio)
                offset = (src_h - new_h) // 2
                img = img.crop((0, offset, src_w, offset + new_h))

            img = img.resize(size, Image.LANCZOS)
            img.save(dst, format="JPEG", quality=88)
            return True
    except Exception as e:
        log.error(f"Не удалось обработать {src}: {e}")
        return False


def build_html(
    draft: Dict[str, Any],
    images_dir: Path,
    output_dir: Path,
    digest_date: str = None,
) -> Path:
    """
    Собирает .htm и .files/ рядом. Возвращает путь к .htm.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not digest_date:
        digest_date = datetime.now().strftime("%Y-%m-%d")

    htm_name = f"digest_{digest_date}.htm"
    files_dirname = f"digest_{digest_date}.files"
    htm_path = output_dir / htm_name
    files_dir = output_dir / files_dirname
    files_dir.mkdir(exist_ok=True)

    # 1. Копируем иконки и баннер (не трогаем — png остаются png)
    for icon_file in ASSETS_DIR.glob("*.png"):
        shutil.copy(icon_file, files_dir / icon_file.name)

    # 2. Готовим картинки под нужные слоты
    images_dir = Path(images_dir)

    def prepare(image_file: str, size: Tuple[int, int], prefix: str) -> str:
        """Кадрирует/ресайзит и копирует в files-папку. Возвращает имя нового файла."""
        if not image_file:
            return ""
        src = images_dir / image_file
        if not src.exists():
            # Пробуем без учёта регистра
            for f in images_dir.iterdir():
                if f.name.lower() == image_file.lower():
                    src = f
                    break
            else:
                log.warning(f"Картинка не найдена: {src}")
                return ""
        dst_name = f"{prefix}_{Path(image_file).stem}.jpg"
        dst = files_dir / dst_name
        if _fit_image(src, dst, size):
            return dst_name
        return ""

    # --- Главные карточки ---
    for item in draft.get("main_block", []) or []:
        if item.get("image_file"):
            item["image_file"] = prepare(item["image_file"], SIZE_MAIN, "main")

    # --- Карточки рубрик ---
    for rubric in draft.get("rubrics", []) or []:
        for card in rubric.get("cards", []) or []:
            if card.get("has_image") and card.get("image_file"):
                card["image_file"] = prepare(card["image_file"], SIZE_CARD, "card")
            else:
                card["image_file"] = ""

    # --- Главное видео ---
    mv = draft.get("main_video")
    if mv and mv.get("image_file"):
        mv["image_file"] = prepare(mv["image_file"], SIZE_VIDEO, "video")

    # --- Цитата ---
    mq = draft.get("main_quote")
    if mq and mq.get("photo_file"):
        mq["photo_file"] = prepare(mq["photo_file"], SIZE_PHOTO, "quote")

    # 3. Тема письма
    topics = draft.get("subject_topics") or []
    subject = (", ".join(topics) + ". Главные новости КОС") if topics else "Главные новости КОС"

    # 4. Рендер
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,
    )
    template = env.get_template("digest_template.html")

    html_output = template.render(
        subject=subject,
        assets_url=files_dirname,
        main_block=draft.get("main_block", []),
        main_figure=draft.get("main_figure"),
        main_video=draft.get("main_video"),
        main_quote=draft.get("main_quote"),
        rubrics=draft.get("rubrics", []),
        video_after_rubric_idx=draft.get("video_after_rubric_idx", 0),
    )

    htm_path.write_text(html_output, encoding="utf-8")
    return htm_path
