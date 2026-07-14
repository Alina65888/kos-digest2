"""
Сборка финального .htm дайджеста кадровых назначений.

Зеркалит src/templater.py: кадрирует фото под слоты, копирует их в
appointments_YYYY-MM-DD.files/, рендерит templates/appointments_template.html.
Кадрирование (_fit_image) переиспользуется из templater.py, не дублируется.
"""
import shutil
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any
from jinja2 import Environment, FileSystemLoader

from .config import (
    TEMPLATES_DIR, SIZE_PERSON_PHOTO, APPOINTMENTS_ASSETS_DIR,
    APPOINTMENT_HEADER_BANNER, APPOINTMENT_FOOTER_BANNER, APPOINTMENT_SECTION_BANNERS,
)
from .templater import _fit_image

log = logging.getLogger(__name__)


def build_appointments_html(
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

    htm_name = f"appointments_{digest_date}.htm"
    files_dirname = f"appointments_{digest_date}.files"
    htm_path = output_dir / htm_name
    files_dir = output_dir / files_dirname
    files_dir.mkdir(exist_ok=True)

    # Постоянные баннеры (шапка, заголовки разделов, подвал) — копируем всегда,
    # независимо от того, что есть в исходных данных письма.
    for icon_file in APPOINTMENTS_ASSETS_DIR.glob("*.png"):
        shutil.copy(icon_file, files_dir / icon_file.name)

    images_dir = Path(images_dir)

    def prepare(image_file: str) -> str:
        if not image_file:
            return ""
        src = images_dir / image_file
        if not src.exists():
            for f in images_dir.iterdir():
                if f.name.lower() == image_file.lower():
                    src = f
                    break
            else:
                log.warning(f"Картинка не найдена: {src}")
                return ""
        dst_name = f"person_{Path(image_file).stem}.jpg"
        dst = files_dir / dst_name
        if _fit_image(src, dst, SIZE_PERSON_PHOTO):
            return dst_name
        return ""

    for section in draft.get("sections", []) or []:
        section["banner"] = APPOINTMENT_SECTION_BANNERS.get(section.get("key"), "")
        for person in section.get("people", []) or []:
            if person.get("image_file"):
                person["image_file"] = prepare(person["image_file"])

    period_label = draft.get("period_label") or ""
    subject = (
        f"Кадровые изменения и назначения: {period_label}"
        if period_label else "Кадровые изменения и назначения"
    )

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,
    )
    template = env.get_template("appointments_template.html")

    html_output = template.render(
        subject=subject,
        assets_url=files_dirname,
        period_label=period_label,
        recipient_label=draft.get("recipient_label") or "",
        sections=draft.get("sections", []),
        org_chart_link=draft.get("org_chart_link") or "",
        contact_email=draft.get("contact_email") or "",
        header_banner=APPOINTMENT_HEADER_BANNER,
        footer_banner=APPOINTMENT_FOOTER_BANNER,
    )

    htm_path.write_text(html_output, encoding="utf-8")
    return htm_path
