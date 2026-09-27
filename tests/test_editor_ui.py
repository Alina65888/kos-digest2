"""Проверки состояния редактора: выбор варианта, перегенерация и перенос."""
from pathlib import Path
from unittest.mock import patch
from datetime import date
import io
import pandas as pd

from streamlit.testing.v1 import AppTest


def editor():
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=20)
    app.session_state["digest_mode"] = "kos"
    app.session_state["draft"] = {
        "subject_topics": [], "main_block": [], "main_figure": None,
        "main_video": None, "main_quote": None, "_classified": [
            {"post_id": 1, "text": "В столовой появились новые блюда."}],
        "rubrics": [{"name": "ЗАБОТА О ЛЮДЯХ", "icon": "rubric_care.png", "cards": [{
            "post_id": 1, "position": 1, "title": "ПЕРЕМЕНЫ ПОДАНЫ",
            "text": "Согласованная подводка.", "link": "https://example.test/source",
            "headline_options": [{"title": "МЕНЮ С ПРОДОЛЖЕНИЕМ", "why_it_fits": "Обновили меню."}],
        }]}],
    }
    return app.run()


def test_alternative_updates_widget_and_preserves_lead():
    app = editor()
    assert not app.exception
    app.selectbox(key="headline_0_0").select("МЕНЮ С ПРОДОЛЖЕНИЕМ").run()
    assert not app.exception
    assert app.text_input(key="c_t_0_0").value == "МЕНЮ С ПРОДОЛЖЕНИЕМ"
    assert app.text_area(key="c_x_0_0").value == "Согласованная подводка."
    assert app.session_state["draft"]["rubrics"][0]["cards"][0]["title"] == "МЕНЮ С ПРОДОЛЖЕНИЕМ"


def test_new_headline_does_not_restore_stale_widget_value_or_lose_edited_lead():
    app = editor()
    app.text_area(key="c_x_0_0").set_value("Подводка с правкой редактора.").run()
    with patch("src.pipeline.rewrite_card", return_value={
        "title": "СЕГОДНЯ В МЕНЮ", "headline_options": [], "_quality_flags": [],
    }) as rewrite:
        app.button(key="rh_0_0").click().run()
    assert not app.exception
    assert app.text_input(key="c_t_0_0").value == "СЕГОДНЯ В МЕНЮ"
    assert app.text_area(key="c_x_0_0").value == "Подводка с правкой редактора."
    assert rewrite.call_args.args[1]["approved_lead"] == "Подводка с правкой редактора."


def test_editor_can_move_card_to_initially_empty_rubric():
    app = editor()
    app.selectbox(key="mv_0_0").select("ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ").run()
    app.button(key="mvb_0_0").click().run()
    assert not app.exception
    target = next(r for r in app.session_state["draft"]["rubrics"] if r["name"] == "ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ")
    assert target["cards"][0]["title"] == "ПЕРЕМЕНЫ ПОДАНЫ"
    assert target["cards"][0]["text"] == "Согласованная подводка."


def test_native_upload_can_pin_a_post_after_period_filter():
    upload = io.BytesIO()
    pd.DataFrame([
        {"Дата публикации поста": "2026-08-01", "Текст поста": "Старый пост."},
        {"Дата публикации поста": "2026-09-20", "Текст поста": "Новая производственная новость."},
    ]).to_excel(upload, index=False)

    def uploaded_file(label, *args, **kwargs):
        return upload if label == "Excel с постами" else ([] if kwargs.get("accept_multiple_files") else None)

    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=20)
    app.session_state["digest_mode"] = "kos"
    with patch("streamlit.file_uploader", side_effect=uploaded_file):
        app.run()
        app.date_input[0].set_value(date(2026, 9, 27)).run()
        assert not app.exception
        picker = next(widget for widget in app.multiselect if widget.label == "Обязательно в «Главное»")
        picker.select(2).run()
        with patch("src.pipeline.build_digest_draft", return_value={"rubrics": [], "main_block": [], "subject_topics": []}) as build:
            next(button for button in app.button if button.label == "Сгенерировать дайджест").click().run()
        assert not app.exception
        assert build.call_args.kwargs["pinned_main_ids"] == [2]
        assert build.call_args.kwargs["digest_date"] == date(2026, 9, 27)
        assert [p["post_id"] for p in build.call_args.args[0]] == [2]
