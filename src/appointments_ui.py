"""
Streamlit-интерфейс дайджеста «Кадровые изменения и назначения».

Отдельная ветка UI, параллельная app.py: свои ключи session_state (префикс appt_),
свой Excel-формат (src/appointments_loader.py), свой пайплайн (src/appointments_pipeline.py)
и свой шаблон (src/appointments_templater.py). Не импортирует app.py, чтобы не запускать
повторно st.set_page_config()/сайдбар новостного дайджеста.
"""
import io
import os
import copy
import logging
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime, date

import streamlit as st

from src.config import APPOINTMENT_SECTIONS, MONTHS_RU_GENITIVE
from src.appointments_loader import load_people, ExcelValidationError
from src.excel_loader import validate_images_dir
from src.appointments_pipeline import build_appointments_draft, regenerate_single_person
from src.appointments_templater import build_appointments_html
from src.llm_client import clear_cache
from src.ui_helpers import esc as _esc

log = logging.getLogger(__name__)


# ── Форматирование периода ──────────────────────────────────────

def _format_ru_date(d: date, with_year: bool = True) -> str:
    s = f"{d.day} {MONTHS_RU_GENITIVE[d.month - 1]}"
    return f"{s} {d.year} г." if with_year else s


def _period_label(start: date, end: date) -> str:
    if not start or not end:
        return ""
    start_part = _format_ru_date(start, with_year=(start.year != end.year))
    return f"с {start_part} по {_format_ru_date(end, with_year=True)}"


# ── Состояние сессии ─────────────────────────────────────────────

def _init_state():
    defaults = [
        ("appt_draft", None),
        ("appt_images_dir", ""),
        ("appt_available_images", []),
    ]
    for k, v in defaults:
        if k not in st.session_state:
            st.session_state[k] = v


def _clear_person_widgets(s_idx=None, p_idx=None):
    fields = ("name", "pos", "prev", "img", "edu", "career", "msg", "mv")
    if s_idx is not None:
        keys = [f"appt_{field}_{s_idx}_{p_idx}" for field in ("edu", "career", "msg")]
    else:
        prefixes = tuple(f"appt_{field}_" for field in fields)
        keys = [key for key in st.session_state if key.startswith(prefixes)]
    for key in keys:
        st.session_state.pop(key, None)


def _get_images_tmp_dir() -> Path:
    if "appt_images_tmp_dir" not in st.session_state:
        st.session_state.appt_images_tmp_dir = Path(tempfile.mkdtemp(prefix="kos_appt_img_"))
    return st.session_state.appt_images_tmp_dir


def _save_uploaded_images(uploaded_files) -> Path:
    tmp_dir = _get_images_tmp_dir()
    for f in tmp_dir.iterdir():
        if f.is_file():
            f.unlink()
    for f in uploaded_files:
        (tmp_dir / f.name).write_bytes(f.getbuffer())
    return tmp_dir


def _show_photo_preview(image_name: str):
    if not image_name or not st.session_state.appt_images_dir:
        return
    img_path = Path(st.session_state.appt_images_dir) / image_name
    if img_path.exists():
        st.image(str(img_path), width=200, caption=image_name)


def _photo_selector(label: str, current_value: str, key: str):
    options = [""] + st.session_state.appt_available_images
    current_idx = options.index(current_value) if current_value in options else 0
    selected = st.selectbox(label, options=options, index=current_idx, key=key)
    if selected:
        _show_photo_preview(selected)
    uploaded = st.file_uploader(
        "Или загрузите новое фото", type=["jpg", "jpeg", "png", "webp"],
        key=f"{key}_upload")
    if uploaded:
        img_dir = Path(st.session_state.appt_images_dir) if st.session_state.appt_images_dir else _get_images_tmp_dir()
        (img_dir / uploaded.name).write_bytes(uploaded.getbuffer())
        if uploaded.name not in st.session_state.appt_available_images:
            st.session_state.appt_available_images.append(uploaded.name)
            st.session_state.appt_available_images.sort()
        selected = uploaded.name
        st.image(uploaded, width=200, caption=uploaded.name)
    return selected


def _progress_callback(widget):
    def cb(current, total, text):
        try:
            pct = min(current / total, 1.0) if total else 0.0
            widget.progress(pct, text=text)
        except Exception:
            pass
    return cb


def _mode_switch_button():
    if st.button("← сменить тип дайджеста", key="appt_switch_mode", use_container_width=True):
        st.session_state.digest_mode = None
        st.rerun()


# ── Основной рендер ────────────────────────────────────────────

def render():
    _init_state()

    with st.sidebar:
        st.markdown("""
        <div class="sidebar-title">
            <span class="sidebar-title-icon">🧑‍💼</span>
            <div><h1>Дайджест назначений</h1></div>
        </div>
        <div class="sidebar-version">v1.1 &middot; Кадровые изменения</div>
        """, unsafe_allow_html=True)

        _mode_switch_button()

        st.markdown("### Источники данных")

        uploaded_xlsx = st.file_uploader(
            "Excel с людьми",
            type=["xlsx"],
            help="Файл .xlsx с колонками: name, new_position, section, previous_position, notes, image_file, is_retirement, order",
            key="appt_xlsx",
        )

        uploaded_images = st.file_uploader(
            "Фотографии",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True,
            help="Фотографии, на которые ссылается колонка image_file",
            key="appt_images",
        )
        if uploaded_images:
            st.caption(f"Загружено файлов: {len(uploaded_images)}")

        st.markdown("### Период выпуска")
        col_start, col_end = st.columns(2)
        with col_start:
            period_start = st.date_input("С", value=datetime.now(), key="appt_period_start")
        with col_end:
            period_end = st.date_input("По", value=datetime.now(), key="appt_period_end")

        recipient_label = st.text_input(
            "Кому адресован выпуск",
            value='Рассылка всем сотрудникам «Казаньоргсинтеза»',
            key="appt_recipient",
        )

        st.markdown("### Параметры")
        use_cache = st.checkbox("Кэш LLM-запросов", value=True, key="appt_use_cache",
                                 help="Повторные запросы на тех же данных бесплатны")
        os.environ["LLM_CACHE_ENABLED"] = "1" if use_cache else "0"

        with st.expander("Расширенные", expanded=False):
            org_chart_link = st.text_input("Ссылка на PDF со схемой оргструктуры", value="", key="appt_org_link")
            contact_email = st.text_input("Контактный e-mail в подвале", value="", key="appt_contact_email")
            debug_mode = st.checkbox("Debug-режим", value=False, key="appt_debug")
            if st.button("Очистить кэш", use_container_width=True, key="appt_clear_cache"):
                n = clear_cache()
                st.toast(f"Удалено {n} кэшированных файлов", icon="🗑")

        st.markdown("---")

        generate_btn = st.button(
            "Сгенерировать дайджест",
            type="primary",
            use_container_width=True,
            disabled=not uploaded_xlsx,
            key="appt_generate",
        )

    # ── Генерация ────────────────────────────────────────────────
    if generate_btn:
        if not uploaded_xlsx:
            st.error("Загрузите Excel-файл с людьми в боковой панели.")
            st.stop()

        tmp_dir = Path(tempfile.mkdtemp(prefix="kos_appt_gen_"))
        tmp_xlsx = tmp_dir / "_tmp_people.xlsx"
        with open(tmp_xlsx, "wb") as f:
            f.write(uploaded_xlsx.getbuffer())

        try:
            people, excel_warnings = load_people(tmp_xlsx)
        except ExcelValidationError as e:
            st.error(f"Ошибка в Excel-файле:\n\n{e}")
            st.stop()

        if uploaded_images:
            images_dir = _save_uploaded_images(uploaded_images)
        else:
            images_dir = tmp_dir / "empty_images"
            images_dir.mkdir(exist_ok=True)

        image_warnings = validate_images_dir(images_dir, people)

        st.session_state.appt_images_dir = str(images_dir)
        st.session_state.appt_available_images = sorted([
            f.name for f in images_dir.iterdir()
            if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        ])

        all_warnings = excel_warnings + image_warnings
        if all_warnings:
            with st.expander(f"Предупреждения ({len(all_warnings)})", expanded=True):
                for w in all_warnings:
                    st.warning(w)

        progress_widget = st.progress(0, text="Подготовка...")
        try:
            draft = build_appointments_draft(
                people,
                period_label=_period_label(period_start, period_end),
                recipient_label=recipient_label,
                progress=_progress_callback(progress_widget),
            )
            draft["org_chart_link"] = org_chart_link
            draft["contact_email"] = contact_email
            _clear_person_widgets()
            st.session_state.appt_draft = draft
            progress_widget.progress(1.0, text="Готово!")
            st.toast("Черновик дайджеста назначений готов!", icon="✅")

            if debug_mode:
                with st.expander("Debug: draft.json", expanded=False):
                    st.json(draft)

        except Exception as e:
            st.error(f"Ошибка генерации:\n\n{e}\n\nПроверьте API-ключ и подключение к интернету.")
            logging.exception("Appointments pipeline failed")

    draft = st.session_state.appt_draft

    # ── Пустое состояние ─────────────────────────────────────────
    if not draft:
        st.markdown("""
        <div class="landing-hero">
            <div class="landing-icon">🧑‍💼</div>
            <div class="landing-title">Дайджест кадровых назначений</div>
            <div class="landing-subtitle">Карточки людей по разделам: ключевые назначения, новые лица, переходы внутри СИБУРа, кто ушёл из команды</div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("#### Формат Excel-файла")
        st.markdown("""
        <table class="format-table">
            <tr><th>Колонка</th><th>Описание</th><th>Обязательная</th><th>Пример</th></tr>
            <tr><td><code>name</code></td><td>ФИО</td><td>Да</td><td>Иванов Иван Иванович</td></tr>
            <tr><td><code>new_position</code></td><td>Новая должность</td><td>Да</td><td>Директор по МТО</td></tr>
            <tr><td><code>section</code></td><td>Раздел: key / new_faces / new_challenge / departed</td><td>Да</td><td>key</td></tr>
            <tr><td><code>previous_position</code></td><td>Предыдущая должность</td><td>Нет</td><td>Заместитель директора</td></tr>
            <tr><td><code>notes</code></td><td>Заметки для LLM (образование, карьера)</td><td>Нет</td><td>Окончил КНИТУ...</td></tr>
            <tr><td><code>image_file</code></td><td>Имя файла фото</td><td>Нет</td><td>photo_ivanov.jpg</td></tr>
            <tr><td><code>is_retirement</code></td><td>Уход на пенсию (для departed)</td><td>Нет</td><td>да</td></tr>
            <tr><td><code>order</code></td><td>Порядок внутри раздела</td><td>Нет</td><td>1</td></tr>
        </table>
        """, unsafe_allow_html=True)
        st.caption("Пожелания учитывают новую роль и другие карточки выпуска. В редакторе можно отдельно подобрать другой вариант.")
        st.caption("Пример файла: sample_input/appointments.xlsx")
        st.stop()

    # ── Статистика ───────────────────────────────────────────────
    stats = draft.get("_stats", {})
    stat_cols = st.columns(len(APPOINTMENT_SECTIONS) + 1)
    stat_cols[0].metric("Всего человек", stats.get("total_people", "—"))
    for i, s in enumerate(APPOINTMENT_SECTIONS, start=1):
        stat_cols[i].metric(s["title"].split(":")[0][:16], stats.get("per_section", {}).get(s["key"], 0))

    if draft.get("warnings"):
        with st.expander(f"Предупреждения ({len(draft['warnings'])})", expanded=False, icon="⚠️"):
            for w in draft["warnings"]:
                st.warning(w)

    # ── Разделы ──────────────────────────────────────────────────
    section_names = [s["key"] for s in draft.get("sections", [])]

    for s_idx, section in enumerate(draft.get("sections", [])):
        st.markdown(
            f'<div class="rubric-section"><div class="rubric-section-title">/ {_esc(section["title"])} /</div></div>',
            unsafe_allow_html=True)

        people = section.get("people", [])
        if not people:
            st.markdown("""<div class="empty-state" style="padding:24px">
                <div class="empty-state-desc">В этом разделе пока никого нет</div>
            </div>""", unsafe_allow_html=True)
            continue

        is_departed = section["key"] == "departed"

        for p_idx, person in enumerate(people):
            title_esc = _esc(person["name"])
            pos_esc = _esc(person.get("new_position", ""))
            st.markdown(f"""<div class="kos-card kos-card-accent">
                <div class="kos-card-header">
                    <span class="kos-badge kos-badge-teal">#{p_idx + 1}</span>
                </div>
                <div class="kos-card-title">{title_esc}</div>
                <div class="kos-card-text">{pos_esc}</div>
            </div>""", unsafe_allow_html=True)

            with st.expander("Редактировать", icon="✏️"):
                new_name = st.text_input("ФИО", value=person["name"], key=f"appt_name_{s_idx}_{p_idx}")
                new_pos = st.text_input("Новая должность", value=person.get("new_position", ""),
                                         key=f"appt_pos_{s_idx}_{p_idx}")
                new_prev = st.text_input("Предыдущая должность", value=person.get("previous_position", ""),
                                          key=f"appt_prev_{s_idx}_{p_idx}")

                if not is_departed:
                    new_img = _photo_selector("Фото", person.get("image_file", ""),
                                               key=f"appt_img_{s_idx}_{p_idx}")
                else:
                    new_img = person.get("image_file", "")

                if section["key"] in ("key", "new_faces"):
                    new_edu = st.text_area("Образование", value=person.get("education", ""),
                                            key=f"appt_edu_{s_idx}_{p_idx}", height=80)
                    new_career = st.text_area("Карьерный путь", value=person.get("career", ""),
                                               key=f"appt_career_{s_idx}_{p_idx}", height=100)
                    new_message = person.get("message", "")
                else:
                    new_edu = person.get("education", "")
                    new_career = person.get("career", "")
                    label = "Поздравление" if section["key"] == "new_challenge" else "Личное прощание (необязательно)"
                    new_message = st.text_area(label, value=person.get("message", ""),
                                                key=f"appt_msg_{s_idx}_{p_idx}", height=100)

                col_a, col_b = st.columns(2)
                with col_a:
                    if st.button("Сохранить", key=f"appt_sv_{s_idx}_{p_idx}", use_container_width=True):
                        person["name"] = new_name
                        person["new_position"] = new_pos
                        person["previous_position"] = new_prev
                        person["image_file"] = new_img
                        person["education"] = new_edu
                        person["career"] = new_career
                        person["message"] = new_message
                        st.toast("Сохранено", icon="✅")
                        st.rerun()
                with col_b:
                    if st.button("Другие пожелания" if section["key"] == "new_challenge" else "Перегенерировать",
                                 key=f"appt_rg_{s_idx}_{p_idx}", use_container_width=True,
                                 help="Меняет пожелание, сохраняя информацию о переходе" if section["key"] == "new_challenge" else "AI перепишет текст по заметкам"):
                        try:
                            with st.spinner("Генерирую новый вариант..."):
                                if section["key"] == "new_challenge":
                                    person["message"] = new_message
                                regenerate_single_person(draft, section["key"], p_idx)
                                _clear_person_widgets(s_idx, p_idx)
                            st.toast("Текст перегенерирован", icon="🎲")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Ошибка перегенерации: {e}")

                st.markdown("---")
                move_cols = st.columns([1, 1, 2])
                with move_cols[0]:
                    if p_idx > 0 and st.button("↑", key=f"appt_up_{s_idx}_{p_idx}",
                                                use_container_width=True, help="Переместить выше"):
                        people[p_idx], people[p_idx - 1] = people[p_idx - 1], people[p_idx]
                        _clear_person_widgets()
                        st.rerun()
                with move_cols[1]:
                    if p_idx < len(people) - 1 and st.button("↓", key=f"appt_dn_{s_idx}_{p_idx}",
                                                               use_container_width=True, help="Переместить ниже"):
                        people[p_idx], people[p_idx + 1] = people[p_idx + 1], people[p_idx]
                        _clear_person_widgets()
                        st.rerun()
                with move_cols[2]:
                    target = st.selectbox(
                        "Перенести в раздел",
                        options=section_names,
                        index=section_names.index(section["key"]),
                        key=f"appt_mv_{s_idx}_{p_idx}")
                    if target != section["key"]:
                        if st.button("Перенести", key=f"appt_mvb_{s_idx}_{p_idx}", use_container_width=True):
                            moved = people.pop(p_idx)
                            moved["section"] = target
                            for sec in draft["sections"]:
                                if sec["key"] == target:
                                    sec["people"].append(moved)
                                    break
                            _clear_person_widgets()
                            st.toast(f"Перенесено в {target}", icon="↗️")
                            st.rerun()

    # ── Предпросмотр ─────────────────────────────────────────────
    st.markdown("---")
    if st.button("Показать предпросмотр дайджеста", use_container_width=True, icon="👁", key="appt_preview_btn"):
        try:
            import base64
            import streamlit.components.v1 as components

            draft_preview = copy.deepcopy(draft)
            output_tmp = Path(tempfile.mkdtemp(prefix="kos_appt_preview_"))
            images_dir = (Path(st.session_state.appt_images_dir)
                          if st.session_state.appt_images_dir else output_tmp)

            with st.spinner("Собираю предпросмотр с фотографиями..."):
                html_path = build_appointments_html(
                    draft=draft_preview,
                    images_dir=images_dir,
                    output_dir=output_tmp,
                    digest_date=datetime.now().strftime("%Y-%m-%d"),
                )
                preview_html = html_path.read_text(encoding="utf-8")

                files_dir = output_tmp / html_path.name.replace(".htm", ".files")
                if files_dir.exists():
                    for img_file in files_dir.iterdir():
                        if img_file.is_file() and img_file.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif"}:
                            mime = "image/png" if img_file.suffix.lower() == ".png" else "image/jpeg"
                            b64 = base64.b64encode(img_file.read_bytes()).decode()
                            data_uri = f"data:{mime};base64,{b64}"
                            preview_html = preview_html.replace(
                                f"{files_dir.name}/{img_file.name}", data_uri)

            components.html(preview_html, height=2000, scrolling=True)
        except Exception as e:
            st.error(f"Ошибка предпросмотра: {e}")

    # ── Экспорт ──────────────────────────────────────────────────
    st.markdown("---")
    st.markdown(f"""<div class="export-section">
        <div class="export-title">Экспорт дайджеста назначений</div>
        <div class="export-desc">Дайджест будет собран в .htm файл, совместимый с Outlook, вместе с обработанными фотографиями.</div>
    </div>""", unsafe_allow_html=True)

    export_cols = st.columns([2, 1])
    with export_cols[0]:
        st.caption("Период: " + (draft.get("period_label") or "—"))
    with export_cols[1]:
        if st.button("Собрать и скачать", type="primary", use_container_width=True, icon="📥", key="appt_export_btn"):
            try:
                draft_copy = copy.deepcopy(draft)
                output_tmp = Path(tempfile.mkdtemp(prefix="kos_appt_out_"))
                images_dir = (Path(st.session_state.appt_images_dir)
                              if st.session_state.appt_images_dir else output_tmp)

                with st.spinner("Собираю HTML и обрабатываю фотографии..."):
                    html_path = build_appointments_html(
                        draft=draft_copy,
                        images_dir=images_dir,
                        output_dir=output_tmp,
                        digest_date=datetime.now().strftime("%Y-%m-%d"),
                    )

                    zip_buffer = io.BytesIO()
                    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                        zf.write(html_path, html_path.name)
                        files_dir = output_tmp / html_path.name.replace(".htm", ".files")
                        if files_dir.exists():
                            for f in files_dir.iterdir():
                                if f.is_file():
                                    zf.write(f, f"{files_dir.name}/{f.name}")
                    zip_buffer.seek(0)

                st.download_button(
                    label="Скачать ZIP-архив",
                    data=zip_buffer,
                    file_name=f"appointments_{datetime.now().strftime('%Y-%m-%d')}.zip",
                    mime="application/zip",
                    use_container_width=True,
                    icon="⬇️",
                    key="appt_download_btn",
                )
                st.toast("Дайджест собран!", icon="✅")
            except Exception as e:
                st.error(f"Ошибка сборки:\n\n{e}\n\nПроверьте, загружены ли фотографии.")
                logging.exception("build_appointments_html failed")
