"""Сценарии редактора на вымышленных постах, без API и корпоративных данных."""
import io
import json
from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from src.editorial_rules import (
    normalize_classification, validate_rewrite_output, headline_options,
    find_announcement_report_duplicates, sanitize_card_text, source_links,
)
from src.excel_loader import load_posts, filter_posts_by_period
from src.pipeline import build_digest_draft, classify_posts, rewrite_card
from src.timeliness import assess_timeliness
from src.templater import build_html


def workbook(rows):
    stream = io.BytesIO()
    pd.DataFrame(rows).to_excel(stream, index=False)
    stream.seek(0)
    return stream


def test_native_export_preserves_caption_original_and_excel_dates():
    serial = (date(2026, 9, 20) - date(1899, 12, 30)).days
    posts, _ = load_posts(workbook([
        {"Дата публикации поста": serial, "Текст поста": "Комментарий коллеги.",
         "Текст оригинального поста": "В столовой обновили меню.", "Репост": "Да",
         "Ссылка на пост": "https://example.test/post"},
        {"Дата публикации поста": "21.09.2026", "Текст поста": "",
         "Текст оригинального поста": "Основной текст репоста."},
        {"Текст поста": "", "Текст оригинального поста": ""},
    ]))
    assert len(posts) == 2
    assert posts[0]["text"] == "Комментарий коллеги.\n\nВ столовой обновили меню."
    assert posts[0]["is_repost"] is True
    assert posts[0]["date"] == "2026-09-20"
    assert posts[1]["text"] == "Основной текст репоста."
    assert posts[0]["link"] == "https://example.test/post"
    kept, excluded, _ = filter_posts_by_period(posts, date(2026, 9, 27))
    assert len(kept) == 2 and not excluded


def test_duplicate_aliases_and_duplicate_repost_text_are_coalesced():
    posts, _ = load_posts(workbook([
        {"text": "", "Текст поста": "Обновили меню.", "Текст оригинального поста": "Обновили меню."},
    ]))
    assert posts[0]["text"] == "Обновили меню."


def test_reasoned_classification_is_not_overridden_by_equipment_words():
    text = "На Фабрике процессов изучали поиск потерь. Оборудование, компрессор, агрегат, установка, ремонт, полиэтилен."
    result = normalize_classification({"text": text}, {
        "rubric_candidate": "ПРОИЗВОДСТВЕННАЯ СИСТЕМА СИБУРА",
        "rubric_evidence": "На Фабрике процессов изучали поиск потерь.",
        "rubric_reason": "Учебная практика методов ПСС, а не реальный ремонт.",
    })
    assert result["rubric_candidate"] == "ПСС"
    assert result["rubric_evidence"]


def test_explicit_editor_rubric_wins():
    result = normalize_classification({"rubric": "ЗАБОТА О ЛЮДЯХ", "text": "Встреча с застройщиком."},
                                      {"rubric_candidate": "СОБЫТИЯ"})
    assert result["rubric_candidate"] == "ЗАБОТА О ЛЮДЯХ"


def option(title, anchor):
    return {"title": title, "source_anchor": anchor, "technique": "метафора",
            "why_it_fits": "Образ связан с предметом новости."}


def test_anchored_metaphor_survives_without_literal_overlap():
    post = {"title": "Обновление меню", "text": "В столовых появились новые блюда."}
    raw = {"title": "Перемены поданы", "headline_options": [
        option("НОВЫЕ БЛЮДА В СТОЛОВОЙ", post["text"]),
        option("Перемены поданы", post["text"]),
    ]}
    result, flags = validate_rewrite_output(post, {"title_only": True}, raw)
    assert result["title"] == "ПЕРЕМЕНЫ ПОДАНЫ"
    assert not flags
    assert len(headline_options(post, {}, raw)) == 2


def test_fabricated_anchor_and_fabricated_result_are_rejected():
    post = {"title": "Испытания масла", "text": "Образец не прошел проверку. Испытания продолжаются."}
    raw = {"title": "МАСЛО ПРОШЛО ПРОВЕРКУ", "headline_options": [
        option("МАСЛО ПРОШЛО ПРОВЕРКУ", post["text"]),
        option("12 ПОБЕД ПОДРЯД", "Команда одержала 12 побед."),
    ]}
    assert headline_options(post, {}, raw) == []


def test_proverb_number_exception_does_not_allow_literal_new_numbers():
    post = {"text": "Перед подъемом нужно проверить страховку и крепления."}
    raw = {"headline_options": [option("СЕМЬ РАЗ ПРОВЕРЬ, ОДИН РАЗ ПОДНИМИСЬ", post["text"]),
                                 option("СЕМЬ СОТРУДНИКОВ ПРОВЕРИЛИ СТРАХОВКУ", post["text"])]}
    options = headline_options(post, {}, raw)
    assert len(options) == 1
    assert options[0]["title"].startswith("СЕМЬ РАЗ")


def test_main_figure_keeps_qualifier_and_unit():
    post = {"text": "Более 12 млн рублей направили на ремонт.",
            "number_value": "Более 12 млн рублей", "number_desc": "на ремонт"}
    result, _ = validate_rewrite_output(post, {"is_figure": True}, {"value": "12", "description": "на ремонт"})
    assert result["value"] == "Более 12 млн рублей"


@pytest.mark.parametrize("text,status", [
    ("19 сентября пройдет встреча. Приглашаем коллег.", "expired"),
    ("Регистрация на встречу 29 сентября открыта до 17 сентября.", "expired"),
    ("Опрос доступен до 23 сентября.", "expired"),
    ("29 сентября пройдет встреча. Приглашаем коллег.", "current"),
    ("19 сентября прошла встреча, участники обсудили обучение.", "current"),
    ("8 сентября прошла встреча. Приглашаем на следующую 29 сентября.", "review"),
    ("Приглашаем на встречу завтра.", "review"),
])
def test_timeliness_uses_publication_date_and_keeps_recaps(text, status):
    assert assess_timeliness({"text": text, "date": "2026-09-10"}, date(2026, 9, 27))["status"] == status


def test_december_announcement_is_not_expired_in_january():
    post = {"text": "Приглашаем на встречу 10 января.", "date": "2026-12-29"}
    assert assess_timeliness(post, date(2027, 1, 3))["status"] == "current"


def test_previous_round_report_does_not_hide_next_invitation():
    posts = [{"post_id": 1, "title": "Итоги клуба", "date": "2026-09-10", "text": "9 сентября прошла встреча клуба."},
             {"post_id": 2, "title": "Анонс клуба", "date": "2026-09-20", "text": "29 сентября пройдет встреча клуба."}]
    assert find_announcement_report_duplicates(posts) == {}


def test_multiple_source_links_survive_and_invented_link_is_not_used():
    post = {"link": "https://example.test/post", "text": "Запись https://example.test/register?code=abc&x=1 и опрос https://example.test/poll"}
    generated = '<a href="https://example.test/register?code=abc&x=1">Запись</a>, <a href="https://example.test/poll">опрос</a>, <a href="javascript:alert(1)">еще</a>'
    cleaned = sanitize_card_text(generated, post["link"], source_links(post))
    assert "register?code=abc&amp;x=1" in cleaned
    assert 'href="https://example.test/poll"' in cleaned
    assert "javascript" not in cleaned


def test_pinned_main_beats_feature_selection_and_keeps_original_title():
    posts = [{"post_id": 7, "title": "Около 12 специалистов завершили ремонт", "text": "Около 12 специалистов завершили ремонт.", "date": "2026-09-20"}]
    classified = [{**posts[0], "rubric_candidate": "ПРОИЗВОДСТВО", "importance": 4,
                   "has_number": True, "number_value": "Около 12", "number_desc": "специалистов"}]
    with patch("src.pipeline.classify_posts", return_value=classified), patch("src.pipeline.plan_digest", return_value={"main_figure_post_id": 7}), patch("src.pipeline.rewrite_card") as rewrite:
        draft = build_digest_draft(posts, digest_date="2026-09-27", pinned_main_ids=[7], preserve_main_titles=True)
    assert draft["main_block"][0]["title"] == posts[0]["title"]
    assert draft["main_figure"] is None
    assert not draft["excluded"]
    rewrite.assert_not_called()


def test_expired_and_editor_excluded_posts_never_go_to_llm():
    posts = [{"post_id": 2, "text": "Приглашаем на встречу 19 сентября.", "date": "2026-09-10"},
             {"post_id": 5, "text": "Рядовая новость.", "date": "2026-09-20"}]
    with patch("src.pipeline.classify_posts") as classify, patch("src.pipeline.plan_digest") as plan:
        draft = build_digest_draft(posts, digest_date="2026-09-27", pinned_main_ids=[2], excluded_post_ids=[5])
    assert draft["_stats"]["excluded_posts"] == 2
    assert not draft["main_block"]
    classify.assert_not_called()
    plan.assert_not_called()


def test_date_and_verified_alternatives_cross_actual_pipeline_boundary():
    post = {"post_id": 3, "date": "2026-09-20", "title": "Меню столовой", "text": "В столовой появились новые блюда."}
    response = {"title": "ПЕРЕМЕНЫ ПОДАНЫ", "headline_options": [option("ПЕРЕМЕНЫ ПОДАНЫ", post["text"])], "text": post["text"]}
    with patch("src.pipeline.llm_json", return_value=response) as llm:
        result = rewrite_card(post, {"digest_date": "2026-09-27"})
    payload = json.loads(llm.call_args.args[1])
    assert payload["context"]["digest_date"] == "2026-09-27"
    assert payload["post"]["date"] == "2026-09-20"
    assert result["headline_options"][0]["title"] == result["title"]


def test_foreign_classification_id_is_not_attached():
    post = {"post_id": 3, "text": "Ремонт оборудования."}
    with patch("src.pipeline.llm_json", return_value={"items": [{"id": 99, "rubric_candidate": "КАРЬЕРА"}]}):
        classified = classify_posts([post], digest_date="2026-09-27")
    assert classified[0]["post_id"] == 3
    assert classified[0]["rubric_candidate"] == "ПРОИЗВОДСТВО"


def test_preserved_title_is_plain_text_and_lead_links_stay_clickable(tmp_path):
    draft = {"subject_topics": [], "main_block": [
        {"post_id": 1, "title": "Потери < 5%", "link": "https://example.test/source", "image_file": ""}],
        "rubrics": [{"name": "ПРОИЗВОДСТВО", "icon": "rubric_production.png", "cards": [
            {"post_id": 2, "title": "ПРОВЕРКА ПРОЙДЕНА", "text": '<a href="https://example.test/details">Подробности</a>',
             "link": "https://example.test/source2", "image_file": "", "has_image": False, "position": 1}], "quote_before": None}]}
    content = build_html(draft, tmp_path, tmp_path, "TEST").read_text()
    assert "Потери &lt; 5%" in content
    assert '<a href="https://example.test/details">Подробности</a>' in content
