"""
Единые константы приложения. Меняешь здесь — меняется везде.
"""
from pathlib import Path

# === ПУТИ ===
ROOT_DIR = Path(__file__).parent.parent
PROMPTS_DIR = ROOT_DIR / "prompts"
TEMPLATES_DIR = ROOT_DIR / "templates"
ASSETS_DIR = ROOT_DIR / "assets" / "icons"
APPOINTMENTS_ASSETS_DIR = ASSETS_DIR / "appointments"
CACHE_DIR = ROOT_DIR / ".cache"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "output"
DEFAULT_INPUT_DIR = ROOT_DIR / "sample_input"

# === LLM ===
CLASSIFY_BATCH_SIZE = 8         # небольшие батчи снижают риск потери отдельных постов
CLASSIFY_TEMPERATURE = 0.2
PLAN_TEMPERATURE = 0.15
REWRITE_TEMPERATURE = 0.45
LLM_MAX_RETRIES = 5             # сколько раз ретраить упавший запрос
LLM_PARALLEL_WORKERS = 1         # последовательно, чтобы не упираться в лимит TPM
CLASSIFY_TEXT_LIMIT = 5000
REWRITE_TEXT_LIMIT = 5000

# === РЕДАКТОРСКИЕ ОГРАНИЧЕНИЯ ===
DIGEST_WINDOW_DAYS = 14
MAIN_BLOCK_SIZE = 4
MIN_MAIN_IMPORTANCE = 6
MIN_CARD_IMPORTANCE = 4
MAX_CARDS_PER_RUBRIC = 4
# Эта рубрика строится на явной связке «просили -> сделали». Если забрать
# такой пост в «Главное», читатель теряет саму механику обратной связи.
MAIN_BLOCK_RESERVED_RUBRICS = {"ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ"}

# === КАНОНИЧЕСКИЙ ПОРЯДОК РУБРИК ===
CANONICAL_RUBRIC_ORDER = [
    "ПРОИЗВОДСТВО",
    "ПСС",
    "БЕЗОПАСНОСТЬ",
    "ЗАБОТА О ЛЮДЯХ",
    "КАРЬЕРА",
    "СОБЫТИЯ",
    "ДОСТИЖЕНИЯ",
    "ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ",
]

# === ИКОНКИ РУБРИК ===
RUBRIC_ICONS = {
    "ПРОИЗВОДСТВО": "rubric_production.png",
    "ПСС": "rubric_pss.png",
    "БЕЗОПАСНОСТЬ": "rubric_safety.png",
    "ЗАБОТА О ЛЮДЯХ": "rubric_care.png",
    "КАРЬЕРА": "rubric_career.png",
    "СОБЫТИЯ": "rubric_events.png",
    "ДОСТИЖЕНИЯ": "rubric_achievements.png",
    "ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ": "rubric_you_asked.png",
}

# === РАЗМЕРЫ КАРТИНОК (под слоты в шаблоне Outlook) ===
SIZE_MAIN = (286, 161)   # карточка в блоке ГЛАВНОЕ
SIZE_CARD = (265, 176)   # обычная карточка в рубрике
SIZE_VIDEO = (320, 180)  # обложка видео
SIZE_PHOTO = (80, 80)    # портрет автора цитаты (круглый)

# === ОБЯЗАТЕЛЬНЫЕ КОЛОНКИ EXCEL ===
REQUIRED_COLUMNS = ["text"]
OPTIONAL_COLUMNS = ["date", "author", "title", "link", "image_file"]
ALL_KNOWN_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

# === ФИРМЕННЫЕ ЦВЕТА (для подсветки в UI) ===
BRAND_TEAL = "#008C95"
BRAND_DARK = "#00313C"
BRAND_MINT = "#77E2C3"

# ===========================================================================
# === ДАЙДЖЕСТ КАДРОВЫХ НАЗНАЧЕНИЙ (второй тип дайджеста) ===
# ===========================================================================

# Канонический порядок разделов и их заголовки в письме
APPOINTMENT_SECTIONS = [
    {"key": "key", "title": "В фокусе: ключевые назначения"},
    {"key": "new_faces", "title": "Новые лица"},
    {"key": "new_challenge", "title": "Новый вызов в СИБУРе"},
    {"key": "departed", "title": "Кто ушёл из команды?"},
]
APPOINTMENT_SECTION_KEYS = [s["key"] for s in APPOINTMENT_SECTIONS]
APPOINTMENT_SECTION_TITLES = {s["key"]: s["title"] for s in APPOINTMENT_SECTIONS}

# Постоянные картинки-баннеры (фирменный дизайн, не меняются от письма к письму)
APPOINTMENT_HEADER_BANNER = "header_banner.png"
APPOINTMENT_FOOTER_BANNER = "footer_banner.png"
APPOINTMENT_SECTION_BANNERS = {
    "key": "section_key.png",
    "new_faces": "section_new_faces.png",
    "new_challenge": "section_new_challenge.png",
    "departed": "section_departed.png",
}

# Разделы, для которых карточка содержит фото + био (образование/карьера)
APPOINTMENT_BIO_SECTIONS = {"key", "new_faces"}
# Раздел, где вместо био — поздравление с переходом внутри СИБУРа
APPOINTMENT_CONGRATS_SECTION = "new_challenge"
# Раздел, где карточки — просто список без фото (иногда с личным прощанием)
APPOINTMENT_DEPARTED_SECTION = "departed"

APPOINTMENT_CLOSING_NOTE = (
    "Мы благодарим коллег за работу в компании и желаем удачи в построении карьеры "
    "за её пределами."
)

SIZE_PERSON_PHOTO = (200, 200)  # фото карточки-персоны в дайджесте назначений

APPOINTMENTS_REQUIRED_COLUMNS = ["name", "new_position", "section"]
APPOINTMENTS_OPTIONAL_COLUMNS = [
    "previous_position", "notes", "image_file", "is_retirement", "order",
]
APPOINTMENTS_ALL_KNOWN_COLUMNS = APPOINTMENTS_REQUIRED_COLUMNS + APPOINTMENTS_OPTIONAL_COLUMNS

MONTHS_RU_GENITIVE = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
