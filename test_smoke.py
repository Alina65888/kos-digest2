"""
Smoke-test: проверяет, что компоненты собираются и templater работает
с реальными фотографиями без вызова LLM. Запуск:

    python test_smoke.py

Если в конце выведет «OK» — приложение собрано корректно.
"""
import sys
import shutil
from pathlib import Path

# Импорт без обращения к LLM
from src.templater import build_html


def main():
    print("=== SMOKE TEST ===\n")

    # Сделаем фиктивный draft с реальными именами картинок
    images_dir = Path("sample_input/images")
    if not images_dir.exists() or not list(images_dir.iterdir()):
        print(f"⚠ В {images_dir} нет картинок — это нормально для свежей установки.")
        print("  Тест с фейковыми именами файлов (фото в HTML не появятся).")
        sample_imgs = ["fake_1.jpg"] * 8
    else:
        files = sorted([f.name for f in images_dir.iterdir()
                        if f.suffix.lower() in {".jpg", ".jpeg", ".png"}])
        sample_imgs = (files * 4)[:8]  # на случай если меньше 8

    draft = {
        "subject_topics": ["Тестовая тема А", "Тестовая тема Б", "Тестовая тема В"],
        "main_block": [
            {"post_id": 1, "title": "Главная новость 1", "image_file": sample_imgs[0]},
            {"post_id": 2, "title": "Главная новость 2", "image_file": sample_imgs[1]},
            {"post_id": 3, "title": "Главная новость 3", "image_file": sample_imgs[2]},
            {"post_id": 4, "title": "Главная новость 4", "image_file": sample_imgs[3]},
        ],
        "main_figure": {"value": "273", "description": "сотрудника сдали ЕКЭ 2026 года"},
        "main_video": {
            "title": "📹 ГЛАВНОЕ ВИДЕО",
            "text": "Тестовый видеоблок с описанием.",
            "image_file": sample_imgs[4],
        },
        "main_quote": {
            "text": "Лучше попробовать и не дойти до конца, чем жалеть, что вообще не попробовал.",
            "author_name": "Денис Воронин",
            "author_role": "ведущий инженер, выпускник «ДНК Лидерства»",
            "photo_file": sample_imgs[5],
        },
        "main_quote_rubric": "ДОСТИЖЕНИЯ",
        "rubrics": [
            {
                "name": "ПРОИЗВОДСТВО",
                "icon": "rubric_production.png",
                "cards": [
                    {"post_id": 5, "title": "ГРАНУЛЫ С СЮРПРИЗОМ",
                     "text": "Качество плёнки начинается с гранулы. Татьяна Щербакова <a href='#'>разобрала</a> дефекты.",
                     "image_file": sample_imgs[6], "has_image": True, "position": 1},
                    {"post_id": 6, "title": "В КАДРЕ РЕМОНТ",
                     "text": "Денис Ишбулатов посетил ПЭНП-Т. В <a href='#'>ролике</a> — что делает аппаратчик.",
                     "image_file": "", "has_image": False, "position": 2},
                ],
                "quote_before": None,
            },
            {
                "name": "ДОСТИЖЕНИЯ",
                "icon": "rubric_achievements.png",
                "cards": [
                    {"post_id": 7, "title": "ИНЖЕНЕР ГОДА",
                     "text": "Булат Яруллин <a href='#'>прошёл</a> путь от аппаратчика до победителя конкурса.",
                     "image_file": sample_imgs[7], "has_image": True, "position": 1},
                ],
                "quote_before": 1,  # цитата после первой карточки
            },
        ],
        "video_after_rubric_idx": 1,
        "warnings": [],
    }

    # Очистим прошлый тестовый прогон
    test_out = Path("output/_smoke_test")
    if test_out.exists():
        shutil.rmtree(test_out)

    # Собираем HTML
    html_path = build_html(
        draft=draft,
        images_dir=images_dir,
        output_dir=test_out,
        digest_date="SMOKE",
    )

    print(f"✅ HTML собран: {html_path}")

    # Проверяем содержимое
    html = html_path.read_text(encoding="utf-8")
    img_tags = html.count("<img")
    print(f"✅ Тегов <img>: {img_tags}")

    files_dir = test_out / "digest_SMOKE.files"
    photos = sorted([f.name for f in files_dir.iterdir()
                     if any(f.name.startswith(p) for p in ["main_", "card_", "video_", "quote_"])])
    icons = sorted([f.name for f in files_dir.iterdir()
                    if not any(f.name.startswith(p) for p in ["main_", "card_", "video_", "quote_"])])

    print(f"✅ Фотографий обработано: {len(photos)}")
    for p in photos:
        print(f"     {p}")
    print(f"✅ Иконок скопировано: {len(icons)}")

    if img_tags >= 6 and (len(photos) > 0 or "fake_" in str(sample_imgs[0])):
        print("\n🎉 OK — приложение работает")
        return 0
    else:
        print("\n❌ Что-то не так")
        return 1


if __name__ == "__main__":
    sys.exit(main())
