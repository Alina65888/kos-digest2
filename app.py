"""
Streamlit-приложение для сборки дайджеста «Главные новости КОС».

Запуск:
    streamlit run app.py
"""
import os
import io
import copy
import logging
import tempfile
import zipfile
import html as html_lib
from pathlib import Path
from datetime import datetime

import streamlit as st

from src.config import BRAND_TEAL, BRAND_DARK, BRAND_MINT
from src.excel_loader import load_posts, validate_images_dir, ExcelValidationError
from src.pipeline import build_digest_draft, regenerate_single_card
from src.templater import build_html
from src.llm_client import clear_cache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ── Страница ──────────────────────────────────────────────────
st.set_page_config(
    page_title="Конструктор дайджеста КОС",
    page_icon="📰",
    layout="wide",
)

# ── Утилиты ───────────────────────────────────────────────────

def _esc(text: str) -> str:
    """HTML-escape пользовательского ввода для безопасного рендера."""
    return html_lib.escape(str(text)) if text else ""


def _get_images_tmp_dir() -> Path:
    """Возвращает стабильный temp-dir для картинок внутри сессии."""
    if "images_tmp_dir" not in st.session_state:
        st.session_state.images_tmp_dir = Path(tempfile.mkdtemp(prefix="kos_img_"))
    return st.session_state.images_tmp_dir


def _save_uploaded_images(uploaded_files) -> Path:
    """Сохраняет загруженные файлы во временную папку сессии."""
    tmp_dir = _get_images_tmp_dir()
    for f in tmp_dir.iterdir():
        if f.is_file():
            f.unlink()
    for f in uploaded_files:
        (tmp_dir / f.name).write_bytes(f.getbuffer())
    return tmp_dir


def _progress_callback(widget):
    def cb(current, total, text):
        try:
            pct = min(current / total, 1.0) if total else 0.0
            widget.progress(pct, text=text)
        except Exception:
            pass
    return cb


# ── Состояние сессии ──────────────────────────────────────────
for _k, _v in [("draft", None), ("images_dir", ""), ("available_images", []),
               ("generation_done", False), ("export_ready", False)]:
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ══════════════════════════════════════════════════════════════
#  CSS — Корпоративная дизайн-система СИБУР
# ══════════════════════════════════════════════════════════════

st.markdown("""
<style>
/* ── Базовые переменные ──────────────────────────────── */
:root {
    --teal: #008C95;
    --teal-light: #e6f5f6;
    --teal-hover: #007a82;
    --dark: #00313C;
    --mint: #77E2C3;
    --mint-light: #e8faf4;
    --bg-card: #ffffff;
    --bg-secondary: #f7f9fa;
    --border: #e4e8eb;
    --text-primary: #1a2b33;
    --text-secondary: #5f7078;
    --text-muted: #8fa0a8;
    --shadow-sm: 0 1px 3px rgba(0,49,60,0.06);
    --shadow-md: 0 4px 12px rgba(0,49,60,0.08);
    --shadow-lg: 0 8px 24px rgba(0,49,60,0.10);
    --radius-sm: 6px;
    --radius-md: 10px;
    --radius-lg: 14px;
    --transition: 0.2s cubic-bezier(0.4,0,0.2,1);
}

/* ── Глобальная типографика ──────────────────────────── */
.main .block-container { max-width: 1100px; padding-top: 1.5rem; }
section[data-testid="stSidebar"] { background: linear-gradient(180deg, #00313C 0%, #004550 100%); }
section[data-testid="stSidebar"] * { color: #c8dfe3 !important; }
section[data-testid="stSidebar"] .stMarkdown h3 { color: #77E2C3 !important; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 1.5px; margin-top: 1.2rem; }
section[data-testid="stSidebar"] label { color: #9bbcc2 !important; font-size: 0.82rem; }
section[data-testid="stSidebar"] .stButton > button {
    border: 1px solid rgba(119,226,195,0.3) !important;
    color: #77E2C3 !important;
    background: transparent !important;
    transition: var(--transition);
}
section[data-testid="stSidebar"] .stButton > button:hover {
    background: rgba(119,226,195,0.1) !important;
    border-color: #77E2C3 !important;
}
section[data-testid="stSidebar"] .stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #008C95, #00a8b3) !important;
    border: none !important;
    color: #fff !important;
    font-weight: 600 !important;
    box-shadow: 0 4px 14px rgba(0,140,149,0.35) !important;
}
section[data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {
    background: linear-gradient(135deg, #007a82, #008C95) !important;
    box-shadow: 0 6px 20px rgba(0,140,149,0.45) !important;
    transform: translateY(-1px);
}

/* ── Sidebar Title ───────────────────────────────────── */
.sidebar-title { display: flex; align-items: center; gap: 10px; padding: 6px 0 0 0; }
.sidebar-title-icon { font-size: 1.6rem; }
.sidebar-title h1 { font-size: 1.15rem !important; font-weight: 700 !important; color: #fff !important; margin: 0 !important; letter-spacing: -0.01em; }
.sidebar-version { font-size: 0.7rem; color: #5f9ea8 !important; letter-spacing: 0.5px; margin-top: -4px; }

/* ── Степпер ─────────────────────────────────────────── */
.stepper { display: flex; gap: 0; margin: 0 0 1.5rem 0; padding: 0; }
.step {
    flex: 1; text-align: center; padding: 14px 12px 12px;
    background: var(--bg-secondary); border: 1px solid var(--border);
    position: relative; transition: var(--transition);
}
.step:first-child { border-radius: var(--radius-md) 0 0 var(--radius-md); }
.step:last-child { border-radius: 0 var(--radius-md) var(--radius-md) 0; }
.step-num {
    display: inline-flex; align-items: center; justify-content: center;
    width: 26px; height: 26px; border-radius: 50%;
    font-size: 0.78rem; font-weight: 700; margin-bottom: 4px;
    background: var(--border); color: var(--text-muted);
}
.step-label { font-size: 0.78rem; color: var(--text-muted); font-weight: 500; }
.step.active { background: var(--teal-light); border-color: var(--teal); }
.step.active .step-num { background: var(--teal); color: #fff; }
.step.active .step-label { color: var(--teal); font-weight: 600; }
.step.done { background: var(--mint-light); border-color: #b3edd9; }
.step.done .step-num { background: #34c38f; color: #fff; }
.step.done .step-label { color: #2a9d6f; }

/* ── Карточки контента ───────────────────────────────── */
.kos-card {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: var(--radius-md); padding: 18px 20px;
    margin-bottom: 12px; transition: var(--transition);
    box-shadow: var(--shadow-sm); position: relative;
}
.kos-card:hover { box-shadow: var(--shadow-md); border-color: #c8d8dc; }
.kos-card-accent { border-left: 4px solid var(--teal); }
.kos-card-header {
    display: flex; align-items: center; gap: 10px;
    margin-bottom: 8px;
}
.kos-badge {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 3px 10px; border-radius: 20px;
    font-size: 0.7rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.5px;
}
.kos-badge-teal { background: var(--teal-light); color: var(--teal); }
.kos-badge-mint { background: var(--mint-light); color: #2a9d6f; }
.kos-badge-muted { background: #f0f2f4; color: var(--text-secondary); }
.kos-card-title {
    font-size: 0.95rem; font-weight: 600; color: var(--dark);
    line-height: 1.4; margin-bottom: 4px;
}
.kos-card-text {
    font-size: 0.88rem; color: var(--text-primary); line-height: 1.55;
}
.kos-card-meta {
    font-size: 0.72rem; color: var(--text-muted); margin-top: 8px;
    display: flex; align-items: center; gap: 12px;
}
.kos-card-meta-dot::before { content: "·"; margin-right: 0; }

/* ── Секция рубрики ──────────────────────────────────── */
.rubric-section {
    margin: 28px 0 8px 0; display: flex; align-items: center; gap: 12px;
    border-bottom: 2px solid var(--teal); padding-bottom: 8px;
}
.rubric-section-title {
    font-size: 1.1rem; font-weight: 700; color: var(--dark);
    letter-spacing: 0.02em;
}

/* ── Главная цифра ───────────────────────────────────── */
.figure-block {
    display: flex; align-items: stretch; border-radius: var(--radius-md);
    overflow: hidden; box-shadow: var(--shadow-md); margin: 8px 0 12px 0;
}
.figure-number {
    background: linear-gradient(135deg, #008C95, #00a8b3);
    color: #fff; padding: 20px 28px; display: flex; align-items: center;
    justify-content: center; min-width: 140px;
}
.figure-number span { font-size: 2.4rem; font-weight: 800; line-height: 1; }
.figure-desc {
    background: var(--mint-light); padding: 18px 22px; flex: 1;
    display: flex; align-items: center;
    font-size: 0.92rem; color: var(--dark); line-height: 1.5;
}

/* ── Цитата ──────────────────────────────────────────── */
.quote-block {
    background: linear-gradient(135deg, #f6fcfc, #edf8f5);
    border-left: 4px solid var(--teal); border-radius: 0 var(--radius-md) var(--radius-md) 0;
    padding: 22px 24px; margin: 8px 0 12px 0; position: relative;
}
.quote-mark {
    font-size: 3rem; color: var(--mint); font-family: Georgia, serif;
    line-height: 0; position: absolute; top: 32px; left: 16px; opacity: 0.5;
}
.quote-text {
    font-size: 1rem; font-style: italic; color: var(--dark);
    line-height: 1.6; margin: 0 0 10px 24px;
}
.quote-author { font-size: 0.82rem; font-weight: 600; color: var(--teal); margin-left: 24px; }
.quote-role { font-size: 0.78rem; color: var(--text-secondary); margin-left: 24px; }

/* ── Пустое состояние ────────────────────────────────── */
.empty-state {
    text-align: center; padding: 60px 30px; color: var(--text-muted);
    background: var(--bg-secondary); border-radius: var(--radius-lg);
    border: 2px dashed var(--border);
}
.empty-state-icon { font-size: 3rem; margin-bottom: 12px; opacity: 0.4; }
.empty-state-title { font-size: 1.1rem; font-weight: 600; color: var(--text-secondary); margin-bottom: 6px; }
.empty-state-desc { font-size: 0.88rem; line-height: 1.5; max-width: 480px; margin: 0 auto; }

/* ── Экспорт ─────────────────────────────────────────── */
.export-section {
    background: linear-gradient(135deg, var(--teal-light), var(--mint-light));
    border: 1px solid #c8e8e6; border-radius: var(--radius-lg);
    padding: 24px 28px; margin: 24px 0 12px 0;
}
.export-title { font-size: 1rem; font-weight: 700; color: var(--dark); margin-bottom: 4px; }
.export-desc { font-size: 0.85rem; color: var(--text-secondary); margin-bottom: 14px; }

/* ── Onboarding landing ──────────────────────────────── */
.landing-hero {
    text-align: center; padding: 40px 20px 30px 20px;
}
.landing-icon { font-size: 3.5rem; margin-bottom: 8px; }
.landing-title { font-size: 1.6rem; font-weight: 700; color: var(--dark); margin-bottom: 6px; }
.landing-subtitle { font-size: 0.95rem; color: var(--text-secondary); margin-bottom: 28px; }
.landing-steps {
    display: flex; gap: 16px; justify-content: center; flex-wrap: wrap;
    margin-bottom: 28px;
}
.landing-step-card {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: var(--radius-md); padding: 20px 18px; width: 200px;
    text-align: center; box-shadow: var(--shadow-sm); transition: var(--transition);
}
.landing-step-card:hover { box-shadow: var(--shadow-md); transform: translateY(-2px); }
.landing-step-num {
    width: 32px; height: 32px; border-radius: 50%; background: var(--teal);
    color: #fff; display: inline-flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 0.85rem; margin-bottom: 10px;
}
.landing-step-label { font-size: 0.85rem; font-weight: 600; color: var(--dark); margin-bottom: 4px; }
.landing-step-desc { font-size: 0.78rem; color: var(--text-muted); line-height: 1.4; }

/* ── Таблица формата ─────────────────────────────────── */
.format-table {
    width: 100%; border-collapse: separate; border-spacing: 0;
    border-radius: var(--radius-md); overflow: hidden;
    box-shadow: var(--shadow-sm); margin: 12px 0 0 0;
}
.format-table th {
    background: var(--dark); color: #fff; padding: 10px 14px;
    font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.5px;
    font-weight: 600; text-align: left;
}
.format-table td {
    padding: 9px 14px; font-size: 0.82rem; color: var(--text-primary);
    border-bottom: 1px solid var(--border); background: var(--bg-card);
}
.format-table tr:last-child td { border-bottom: none; }
.format-table code {
    background: var(--teal-light); color: var(--teal); padding: 2px 7px;
    border-radius: 4px; font-size: 0.78rem; font-weight: 600;
}

/* ── Общие утилиты ───────────────────────────────────── */
.mt-0 { margin-top: 0; }
.mb-0 { margin-bottom: 0; }
.text-center { text-align: center; }
.muted { color: var(--text-muted); }

/* ── Скрытие ненужных элементов Streamlit ────────────── */
#MainMenu { visibility: hidden; }
footer { visibility: hidden; }
header[data-testid="stHeader"] { background: transparent; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════
#  SIDEBAR
# ══════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("""
    <div class="sidebar-title">
        <span class="sidebar-title-icon">📰</span>
        <div>
            <h1>Дайджест КОС</h1>
        </div>
    </div>
    <div class="sidebar-version">v2.1 &middot; Веб-конструктор</div>
    """, unsafe_allow_html=True)

    st.markdown("### Источники данных")

    uploaded_xlsx = st.file_uploader(
        "Excel с постами",
        type=["xlsx"],
        help="Файл .xlsx с колонками: date, author, title, text, link, image_file",
    )

    uploaded_images = st.file_uploader(
        "Фотографии",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
        help="Все фотографии, на которые ссылается колонка image_file",
    )
    if uploaded_images:
        st.caption(f"Загружено файлов: {len(uploaded_images)}")

    digest_date = st.date_input("Дата выпуска", value=datetime.now())

    st.markdown("### Параметры")

    use_cache = st.checkbox("Кэш LLM-запросов", value=True,
                            help="Повторные запросы на тех же данных бесплатны")
    os.environ["LLM_CACHE_ENABLED"] = "1" if use_cache else "0"

    with st.expander("Расширенные", expanded=False):
        debug_mode = st.checkbox("Debug-режим", value=False,
                                 help="Показать промежуточный JSON")
        if st.button("Очистить кэш", use_container_width=True):
            n = clear_cache()
            st.toast(f"Удалено {n} кэшированных файлов", icon="🗑")

    st.markdown("---")

    generate_btn = st.button(
        "Сгенерировать дайджест",
        type="primary",
        use_container_width=True,
        disabled=not uploaded_xlsx,
    )


# ══════════════════════════════════════════════════════════════
#  СТЕППЕР — визуальный индикатор прогресса
# ══════════════════════════════════════════════════════════════

def _render_stepper(active: int):
    """active: 1=Загрузка, 2=Редактирование, 3=Экспорт"""
    steps = [("Загрузка", "1"), ("Генерация и редактирование", "2"), ("Экспорт", "3")]
    html_parts = []
    for i, (label, num) in enumerate(steps, 1):
        cls = "done" if i < active else ("active" if i == active else "")
        check = "✓" if i < active else num
        html_parts.append(f'<div class="step {cls}"><div class="step-num">{check}</div><div class="step-label">{label}</div></div>')
    st.markdown(f'<div class="stepper">{"".join(html_parts)}</div>', unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════
#  ГЕНЕРАЦИЯ
# ══════════════════════════════════════════════════════════════

if generate_btn:
    if not uploaded_xlsx:
        st.error("Загрузите Excel-файл с постами в боковой панели.")
        st.stop()

    tmp_dir = Path(tempfile.mkdtemp(prefix="kos_gen_"))
    tmp_xlsx = tmp_dir / "_tmp_posts.xlsx"
    with open(tmp_xlsx, "wb") as f:
        f.write(uploaded_xlsx.getbuffer())

    try:
        posts, excel_warnings = load_posts(tmp_xlsx)
    except ExcelValidationError as e:
        st.error(f"Ошибка в Excel-файле:\n\n{e}")
        st.stop()

    if uploaded_images:
        images_dir = _save_uploaded_images(uploaded_images)
    else:
        images_dir = tmp_dir / "empty_images"
        images_dir.mkdir(exist_ok=True)

    image_warnings = validate_images_dir(images_dir, posts)

    st.session_state.images_dir = str(images_dir)
    st.session_state.available_images = sorted([
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
        draft = build_digest_draft(posts, progress=_progress_callback(progress_widget))
        st.session_state.draft = draft
        st.session_state.generation_done = True
        progress_widget.progress(1.0, text="Готово!")
        st.toast("Черновик дайджеста готов!", icon="✅")

        if debug_mode:
            with st.expander("Debug: draft.json", expanded=False):
                st.json(draft)

    except Exception as e:
        st.error(f"Ошибка генерации:\n\n{e}\n\nПроверьте API-ключ и подключение к интернету.")
        logging.exception("Pipeline failed")


# ══════════════════════════════════════════════════════════════
#  ОСНОВНОЙ КОНТЕНТ
# ══════════════════════════════════════════════════════════════

draft = st.session_state.draft

# ── Пустое состояние (landing) ────────────────────────────────
if not draft:
    _render_stepper(1)
    st.markdown("""
    <div class="landing-hero">
        <div class="landing-icon">📰</div>
        <div class="landing-title">Конструктор дайджеста КОС</div>
        <div class="landing-subtitle">Автоматическая сборка корпоративного дайджеста с помощью AI</div>
        <div class="landing-steps">
            <div class="landing-step-card">
                <div class="landing-step-num">1</div>
                <div class="landing-step-label">Загрузите данные</div>
                <div class="landing-step-desc">Excel с постами и папку с фотографиями</div>
            </div>
            <div class="landing-step-card">
                <div class="landing-step-num">2</div>
                <div class="landing-step-label">Сгенерируйте</div>
                <div class="landing-step-desc">AI классифицирует, отберёт главные и перепишет</div>
            </div>
            <div class="landing-step-card">
                <div class="landing-step-num">3</div>
                <div class="landing-step-label">Отредактируйте</div>
                <div class="landing-step-desc">Поправьте заголовки, тексты, фото</div>
            </div>
            <div class="landing-step-card">
                <div class="landing-step-num">4</div>
                <div class="landing-step-label">Скачайте</div>
                <div class="landing-step-desc">Готовый .htm для вставки в Outlook</div>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("#### Формат Excel-файла")
    st.markdown("""
    <table class="format-table">
        <tr><th>Колонка</th><th>Описание</th><th>Обязательная</th><th>Пример</th></tr>
        <tr><td><code>text</code></td><td>Полный текст поста</td><td>Да</td><td>Сегодня на площадке...</td></tr>
        <tr><td><code>date</code></td><td>Дата публикации</td><td>Нет</td><td>2026-05-09</td></tr>
        <tr><td><code>author</code></td><td>Автор или спикер</td><td>Нет</td><td>Иван Иванов, инженер</td></tr>
        <tr><td><code>title</code></td><td>Заголовок поста</td><td>Нет</td><td>Дефекты в полиэтилене</td></tr>
        <tr><td><code>link</code></td><td>Ссылка на портал</td><td>Нет</td><td>https://social.sibur.ru/...</td></tr>
        <tr><td><code>image_file</code></td><td>Имя файла фото</td><td>Нет</td><td>PHOTO-12345.jpg</td></tr>
    </table>
    """, unsafe_allow_html=True)
    st.stop()


# ── Дайджест загружен — показываем редактор ───────────────────
_render_stepper(2)

# ── Статистика ────────────────────────────────────────────────
main_count = len(draft.get("main_block", []))
rubric_count = len(draft.get("rubrics", []))
card_count = sum(len(r.get("cards", [])) for r in draft.get("rubrics", []))

stats = draft.get("_stats", {})
input_posts = stats.get("input_posts", "—")
placed_posts = stats.get("placed_posts", "—")

stat_cols = st.columns(5)
stat_cols[0].metric("Постов на входе", input_posts)
stat_cols[1].metric("Размещено", placed_posts)
stat_cols[2].metric("Главных", main_count)
stat_cols[3].metric("Рубрик", rubric_count)
stat_cols[4].metric("Карточек", card_count)

# ── Предупреждения ────────────────────────────────────────────
if draft.get("warnings"):
    with st.expander(f"Предупреждения ({len(draft['warnings'])})", expanded=False, icon="⚠️"):
        for w in draft["warnings"]:
            st.warning(w)

# ── Тема письма ───────────────────────────────────────────────
st.markdown('<div class="rubric-section"><div class="rubric-section-title">Тема письма</div></div>',
            unsafe_allow_html=True)
topics_str = ", ".join(draft.get("subject_topics", []))
new_topics = st.text_input("Темы через запятую", value=topics_str, label_visibility="collapsed")
draft["subject_topics"] = [t.strip() for t in new_topics.split(",") if t.strip()]


# ══════════════════════════════════════════════════════════════
#  БЛОК «ГЛАВНОЕ»
# ══════════════════════════════════════════════════════════════

st.markdown('<div class="rubric-section"><div class="rubric-section-title">/ ГЛАВНОЕ /</div></div>',
            unsafe_allow_html=True)

main_block = draft.get("main_block", [])
if not main_block:
    st.markdown("""<div class="empty-state">
        <div class="empty-state-icon">📌</div>
        <div class="empty-state-title">Нет главных новостей</div>
        <div class="empty-state-desc">AI не выбрал ни одной главной новости. Проверьте, достаточно ли постов в Excel.</div>
    </div>""", unsafe_allow_html=True)
else:
    main_cols = st.columns(2)
    for idx, item in enumerate(main_block):
        with main_cols[idx % 2]:
            title_esc = _esc(item["title"])
            img_info = _esc(item.get("image_file") or "нет")
            link_esc = _esc(item.get("link") or "нет")
            st.markdown(f"""<div class="kos-card kos-card-accent">
                <div class="kos-card-header">
                    <span class="kos-badge kos-badge-teal">Главное #{idx+1}</span>
                </div>
                <div class="kos-card-title">{title_esc}</div>
                <div class="kos-card-meta">
                    <span>Пост #{item["post_id"]}</span>
                    <span>Фото: {img_info}</span>
                    <span>Ссылка: {"есть" if item.get("link") else "нет"}</span>
                </div>
            </div>""", unsafe_allow_html=True)
            with st.expander("Редактировать", icon="✏️"):
                new_title = st.text_area("Заголовок", value=item["title"], key=f"main_title_{idx}")
                new_image = st.selectbox(
                    "Фото", options=[""] + st.session_state.available_images,
                    index=(st.session_state.available_images.index(item["image_file"]) + 1)
                          if item.get("image_file") in st.session_state.available_images else 0,
                    key=f"main_image_{idx}")
                new_link = st.text_input("Ссылка", value=item.get("link", ""), key=f"main_link_{idx}")
                if st.button("Сохранить", key=f"save_main_{idx}", use_container_width=True):
                    draft["main_block"][idx]["title"] = new_title
                    draft["main_block"][idx]["image_file"] = new_image
                    draft["main_block"][idx]["link"] = new_link
                    st.toast("Сохранено", icon="✅")
                    st.rerun()


# ══════════════════════════════════════════════════════════════
#  ГЛАВНАЯ ЦИФРА
# ══════════════════════════════════════════════════════════════

st.markdown('<div class="rubric-section"><div class="rubric-section-title">Главная цифра</div></div>',
            unsafe_allow_html=True)

fig = draft.get("main_figure")
if fig:
    val_esc = _esc(fig["value"])
    desc_esc = _esc(fig["description"])
    st.markdown(f"""<div class="figure-block">
        <div class="figure-number"><span>{val_esc}</span></div>
        <div class="figure-desc">{desc_esc}</div>
    </div>""", unsafe_allow_html=True)
    with st.expander("Редактировать", icon="✏️"):
        new_val = st.text_input("Значение", value=fig["value"], key="fig_val")
        new_desc = st.text_area("Описание", value=fig["description"], key="fig_desc")
        if st.button("Сохранить", key="save_fig", use_container_width=True):
            draft["main_figure"]["value"] = new_val
            draft["main_figure"]["description"] = new_desc
            st.toast("Сохранено", icon="✅")
            st.rerun()
else:
    st.markdown("""<div class="empty-state" style="padding:30px">
        <div class="empty-state-desc">Главная цифра не выбрана</div>
    </div>""", unsafe_allow_html=True)
    if st.button("Добавить вручную", key="add_fig"):
        draft["main_figure"] = {"value": "0", "description": "Описание цифры", "post_id": None}
        st.rerun()


# ══════════════════════════════════════════════════════════════
#  ГЛАВНОЕ ВИДЕО
# ══════════════════════════════════════════════════════════════

st.markdown('<div class="rubric-section"><div class="rubric-section-title">Главное видео</div></div>',
            unsafe_allow_html=True)

mv = draft.get("main_video")
if mv:
    title_esc = _esc(mv["title"])
    text_esc = _esc(mv["text"])
    img_esc = _esc(mv.get("image_file") or "нет")
    st.markdown(f"""<div class="kos-card kos-card-accent">
        <div class="kos-card-header">
            <span class="kos-badge kos-badge-mint">Видео</span>
        </div>
        <div class="kos-card-title">{title_esc}</div>
        <div class="kos-card-text">{text_esc}</div>
        <div class="kos-card-meta"><span>Обложка: {img_esc}</span></div>
    </div>""", unsafe_allow_html=True)
    with st.expander("Редактировать", icon="✏️"):
        new_title = st.text_input("Заголовок", value=mv["title"], key="video_title")
        new_text = st.text_area("Описание", value=mv["text"], key="video_text")
        new_image = st.selectbox(
            "Обложка", options=[""] + st.session_state.available_images,
            index=(st.session_state.available_images.index(mv["image_file"]) + 1)
                  if mv.get("image_file") in st.session_state.available_images else 0,
            key="video_image")
        if st.button("Сохранить", key="save_video", use_container_width=True):
            draft["main_video"]["title"] = new_title
            draft["main_video"]["text"] = new_text
            draft["main_video"]["image_file"] = new_image
            st.toast("Сохранено", icon="✅")
            st.rerun()
else:
    st.markdown("""<div class="empty-state" style="padding:30px">
        <div class="empty-state-desc">Главного видео в этом выпуске нет</div>
    </div>""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════
#  ЦИТАТА
# ══════════════════════════════════════════════════════════════

st.markdown('<div class="rubric-section"><div class="rubric-section-title">Цитата</div></div>',
            unsafe_allow_html=True)

mq = draft.get("main_quote")
if mq and mq.get("text"):
    text_esc = _esc(mq["text"])
    name_esc = _esc(mq.get("author_name", ""))
    role_esc = _esc(mq.get("author_role", ""))
    rubric_esc = _esc(draft.get("main_quote_rubric") or "не указана")
    st.markdown(f"""<div class="quote-block">
        <div class="quote-mark">&ldquo;</div>
        <div class="quote-text">{text_esc}</div>
        <div class="quote-author">{name_esc}</div>
        <div class="quote-role">{role_esc}</div>
        <div class="kos-card-meta" style="margin-top:12px"><span>Рубрика: {rubric_esc}</span></div>
    </div>""", unsafe_allow_html=True)
    with st.expander("Редактировать", icon="✏️"):
        new_text = st.text_area("Текст цитаты", value=mq["text"], key="q_text")
        new_name = st.text_input("Автор", value=mq.get("author_name", ""), key="q_name")
        new_role = st.text_input("Должность", value=mq.get("author_role", ""), key="q_role")
        new_photo = st.selectbox(
            "Фото автора", options=[""] + st.session_state.available_images,
            index=(st.session_state.available_images.index(mq.get("photo_file", "")) + 1)
                  if mq.get("photo_file") in st.session_state.available_images else 0,
            key="q_photo")
        rubric_options = [r["name"] for r in draft.get("rubrics", [])]
        if rubric_options:
            current_idx = (rubric_options.index(draft.get("main_quote_rubric"))
                           if draft.get("main_quote_rubric") in rubric_options else 0)
            new_rubric = st.selectbox("Рубрика для цитаты", options=rubric_options,
                                      index=current_idx, key="q_rubric")
            new_pos = st.number_input("После карточки #", min_value=1, max_value=10,
                                       value=1, key="q_pos")
        else:
            new_rubric = None
            new_pos = 1
        if st.button("Сохранить", key="save_q", use_container_width=True):
            draft["main_quote"]["text"] = new_text
            draft["main_quote"]["author_name"] = new_name
            draft["main_quote"]["author_role"] = new_role
            draft["main_quote"]["photo_file"] = new_photo
            if new_rubric:
                draft["main_quote_rubric"] = new_rubric
                for r in draft["rubrics"]:
                    r["quote_before"] = new_pos if r["name"] == new_rubric else None
            st.toast("Сохранено", icon="✅")
            st.rerun()
else:
    st.markdown("""<div class="empty-state" style="padding:30px">
        <div class="empty-state-desc">Цитата не выбрана. Можно добавить вручную.</div>
    </div>""", unsafe_allow_html=True)
    if st.button("Добавить цитату", key="add_q"):
        draft["main_quote"] = {
            "text": "Текст цитаты",
            "author_name": "Имя",
            "author_role": "Должность",
            "photo_file": "",
            "post_id": None,
        }
        if draft.get("rubrics"):
            draft["main_quote_rubric"] = draft["rubrics"][0]["name"]
            draft["rubrics"][0]["quote_before"] = 1
        st.rerun()


# ══════════════════════════════════════════════════════════════
#  РУБРИКИ
# ══════════════════════════════════════════════════════════════

for r_idx, rubric in enumerate(draft.get("rubrics", [])):
    st.markdown(f'<div class="rubric-section"><div class="rubric-section-title">/ {_esc(rubric["name"])} /</div></div>',
                unsafe_allow_html=True)

    if not rubric.get("cards"):
        st.markdown("""<div class="empty-state" style="padding:24px">
            <div class="empty-state-desc">Нет карточек в этой рубрике</div>
        </div>""", unsafe_allow_html=True)
        continue

    for c_idx, card in enumerate(rubric["cards"]):
        has_img = card.get("has_image", False)
        badge = '<span class="kos-badge kos-badge-mint">С фото</span>' if has_img else '<span class="kos-badge kos-badge-muted">Без фото</span>'
        title_esc = _esc(card["title"])
        text_safe = card["text"]
        img_esc = _esc(card.get("image_file", "")) if has_img else "—"

        st.markdown(f"""<div class="kos-card">
            <div class="kos-card-header">
                <span class="kos-badge kos-badge-teal">#{card["position"]}</span>
                {badge}
            </div>
            <div class="kos-card-title">{title_esc}</div>
            <div class="kos-card-text">{text_safe}</div>
            <div class="kos-card-meta">
                <span>Пост #{card["post_id"]}</span>
                <span>Фото: {img_esc}</span>
            </div>
        </div>""", unsafe_allow_html=True)

        with st.expander("Редактировать", icon="✏️"):
            new_title = st.text_input("Заголовок (КАПСОМ)", value=card["title"],
                                       key=f"c_t_{r_idx}_{c_idx}")
            new_text = st.text_area("Текст", value=card["text"],
                                     key=f"c_x_{r_idx}_{c_idx}", height=120)
            new_has = st.checkbox("С фото", value=has_img, key=f"c_h_{r_idx}_{c_idx}")
            new_img = st.selectbox(
                "Фото", options=[""] + st.session_state.available_images,
                index=(st.session_state.available_images.index(card.get("image_file", "")) + 1)
                      if card.get("image_file") in st.session_state.available_images else 0,
                key=f"c_i_{r_idx}_{c_idx}")

            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("Сохранить", key=f"sv_{r_idx}_{c_idx}", use_container_width=True):
                    card["title"] = new_title
                    card["text"] = new_text
                    card["has_image"] = new_has
                    card["image_file"] = new_img if new_has else ""
                    st.toast("Сохранено", icon="✅")
                    st.rerun()
            with col_b:
                if st.button("Перегенерировать", key=f"rg_{r_idx}_{c_idx}",
                             use_container_width=True,
                             help="AI придумает новый заголовок и текст"):
                    try:
                        with st.spinner("Генерирую новый вариант..."):
                            regenerate_single_card(draft, r_idx, c_idx)
                        st.toast("Карточка перегенерирована", icon="🎲")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Ошибка перегенерации: {e}")


# ══════════════════════════════════════════════════════════════
#  ЭКСПОРТ
# ══════════════════════════════════════════════════════════════

st.markdown("---")
_render_stepper(3)

st.markdown(f"""<div class="export-section">
    <div class="export-title">Экспорт дайджеста</div>
    <div class="export-desc">Дайджест будет собран в .htm файл, совместимый с Outlook. Все фотографии будут обработаны и включены в архив.</div>
</div>""", unsafe_allow_html=True)

export_cols = st.columns([2, 1])
with export_cols[0]:
    st.caption("Дата выпуска: " + digest_date.strftime("%d.%m.%Y"))
with export_cols[1]:
    if st.button("Собрать и скачать", type="primary", use_container_width=True, icon="📥"):
        try:
            draft_copy = copy.deepcopy(draft)
            output_tmp = Path(tempfile.mkdtemp(prefix="kos_out_"))
            images_dir = (Path(st.session_state.images_dir)
                          if st.session_state.images_dir else output_tmp)

            with st.spinner("Собираю HTML и обрабатываю фотографии..."):
                html_path = build_html(
                    draft=draft_copy,
                    images_dir=images_dir,
                    output_dir=output_tmp,
                    digest_date=digest_date.strftime("%Y-%m-%d"),
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
                file_name=f"digest_{digest_date.strftime('%Y-%m-%d')}.zip",
                mime="application/zip",
                use_container_width=True,
                icon="⬇️",
            )
            st.toast("Дайджест собран!", icon="✅")
        except Exception as e:
            st.error(f"Ошибка сборки:\n\n{e}\n\nПроверьте, загружены ли фотографии.")
            logging.exception("build_html failed")
