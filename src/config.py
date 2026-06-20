"""
Единые константы приложения. Меняешь здесь — меняется везде.
"""
from pathlib import Path

# === ПУТИ ===
ROOT_DIR = Path(__file__).parent.parent
PROMPTS_DIR = ROOT_DIR / "prompts"
TEMPLATES_DIR = ROOT_DIR / "templates"
ASSETS_DIR = ROOT_DIR / "assets" / "icons"
CACHE_DIR = ROOT_DIR / ".cache"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "output"
DEFAULT_INPUT_DIR = ROOT_DIR / "sample_input"

# === LLM ===
CLASSIFY_BATCH_SIZE = 10        # классифицируем по 10 постов в одном запросе
CLASSIFY_TEMPERATURE = 0.2
PLAN_TEMPERATURE = 0.3
REWRITE_TEMPERATURE = 0.7
LLM_MAX_RETRIES = 5             # сколько раз ретраить упавший запрос
LLM_PARALLEL_WORKERS = 1         # последовательно, чтобы не упираться в лимит TPM

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
