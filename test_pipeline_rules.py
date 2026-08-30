"""Регрессионные тесты редакционных правил дайджеста КОС."""
import sys
import types
import unittest
from unittest.mock import patch

# Тестируем чистые правила пайплайна без сетевого SDK и API-ключа.
_llm_stub = types.ModuleType("src.llm_client")
_llm_stub.llm_json = lambda *args, **kwargs: {}
_llm_stub.load_prompt = lambda name: name
sys.modules.setdefault("src.llm_client", _llm_stub)

from src.pipeline import (
    NEWS_RUBRICS,
    _grounded_quote,
    _keyword_rubric,
    _normalise_rubric,
    _plain_text,
    _safe_card_text,
    _safe_title,
    build_digest_draft,
)


def _post(title, text, rubric, *, importance=5, include=True, number=None):
    return {
        "date": "2026-08-29",
        "author": "Редакция",
        "title": title,
        "text": text,
        "link": "https://example.com/source",
        "image_file": "",
        "rubric": "",
        "rubric_candidate": rubric,
        "rubric_confidence": 0.91,
        "importance": importance,
        "include_in_digest": include,
        "exclude_reason": "бытовое сообщение" if not include else None,
        "has_number": bool(number),
        "number_value": number,
        "number_desc": "тонн в год" if number else None,
        "has_quote": False,
        "quote_text": None,
        "quote_author_name": None,
        "quote_author_role": None,
        "is_video": False,
        "is_special": False,
        "summary_short": text[:300],
    }


def _classified(posts, progress=None):
    return [dict(post) for post in posts]


def _rewrite(post, context):
    if context.get("is_figure"):
        # Намеренная выдумка: валидатор обязан вернуть число исходника.
        return {"value": "999", "description": "999 подтвержденных тонн"}
    if context.get("is_video"):
        return {"text": post["text"]}
    return {
        "title": post["title"],
        "text": post["text"],
    }


class EditorialSafetyTests(unittest.TestCase):
    def test_manual_rubric_has_priority(self):
        post = {
            "title": "Встреча команды",
            "text": "На встрече команда разобрала проект А3.",
            "rubric": "ПСС",
        }
        self.assertEqual(_normalise_rubric("СОБЫТИЯ", post), "ПСС")

    def test_keyword_fallback_uses_narrow_meaning(self):
        self.assertEqual(
            _keyword_rubric({"title": "Маршруты", "text": "Обновили маршруты мобильных обходов ПСС."}),
            "ПСС",
        )
        self.assertEqual(
            _keyword_rubric({"title": "ДМС", "text": "Доступна консультация педиатра по ДМС."}),
            "ЗАБОТА О ЛЮДЯХ",
        )
        self.assertEqual(
            _keyword_rubric({"title": "Медали", "text": "Команда получила две медали на турнире."}),
            "ДОСТИЖЕНИЯ",
        )
        self.assertEqual(
            _normalise_rubric(
                "СОБЫТИЯ",
                {"title": "ДМС", "text": "Доступна консультация педиатра по ДМС."},
            ),
            "ЗАБОТА О ЛЮДЯХ",
        )

    def test_invented_result_and_number_fall_back_to_source(self):
        post = {
            "title": "Испытания масла",
            "text": "Образец не прошел проверку. Новая партия ожидается 15 сентября.",
            "link": "https://example.com/source",
        }
        title = _safe_title("МАСЛО ПРОШЛО ПРОВЕРКУ", post)
        text = _safe_card_text("Масло успешно прошло проверку 20 сентября.", post)

        self.assertEqual(title, "ИСПЫТАНИЯ МАСЛА")
        self.assertIn("не прошел", _plain_text(text).lower())
        self.assertNotIn("20", _plain_text(text))
        self.assertLessEqual(len(_plain_text(text)), 300)

    def test_generated_copy_requires_verbatim_evidence(self):
        post = {
            "title": "Обновление лаборатории",
            "text": "В лаборатории установили новый анализатор.",
            "link": "",
        }
        generated = _safe_card_text(
            "Лаборатория ускорила выпуск продукции.",
            post,
            evidence=["Лаборатория ускорила выпуск продукции"],
            require_evidence=True,
        )

        self.assertEqual(
            _plain_text(generated),
            "В лаборатории установили новый анализатор.",
        )

    def test_fallback_title_keeps_two_word_minimum(self):
        post = {"title": "Ремонт", "text": "Ремонт."}
        self.assertEqual(len(_safe_title("", post).split()), 2)

    def test_quote_must_be_verbatim(self):
        exact = {
            "text": "Иван сказал: «Работу продолжим в сентябре».",
            "quote_text": "Работу продолжим в сентябре",
        }
        paraphrase = dict(exact, quote_text="Работа продолжится осенью")
        self.assertEqual(_grounded_quote(exact), "Работу продолжим в сентябре")
        self.assertIsNone(_grounded_quote(paraphrase))


class DistributionTests(unittest.TestCase):
    @patch("src.pipeline.rewrite_card", side_effect=_rewrite)
    @patch("src.pipeline.plan_digest")
    @patch("src.pipeline.classify_posts", side_effect=_classified)
    def test_three_main_no_cross_rubric_moves_and_noise_filtered(
        self, _mock_classify, mock_plan, _mock_rewrite
    ):
        posts = [
            _post("Новый узел", "Ввели новый производственный узел на 120 тонн в год.", "ПРОИЗВОДСТВО", importance=10),
            _post("Маршруты обходов", "Команда ПСС обновила маршруты мобильных обходов.", "ПСС", importance=9),
            _post("Стоп-карта", "Аппаратчик применил стоп-карту и остановил работу.", "БЕЗОПАСНОСТЬ", importance=9),
            _post("Педиатр по ДМС", "По ДМС доступны консультации педиатра.", "ЗАБОТА О ЛЮДЯХ"),
            _post("Подготовка к ЕГЭ", "Открыта подготовка к ЕГЭ по химии.", "КАРЬЕРА"),
            _post("Форум руководства", "Форум состоится 5 сентября.", "СОБЫТИЯ"),
            _post("Команда получила награду", "Команда получила награду конкурса.", "ДОСТИЖЕНИЯ"),
            _post("Проектная мощность", "Установка рассчитана на 120 тонн в год.", "ПРОИЗВОДСТВО", number="120"),
            _post("Найдена кружка", "В комнате найдена синяя кружка.", "СОБЫТИЯ", include=False),
        ]
        mock_plan.return_value = {
            "subject_topics": ["выдуманная тема"],
            "main_block": [
                {"post_id": 1}, {"post_id": 2}, {"post_id": 3}, {"post_id": 4}
            ],
            "main_figure_post_id": 8,
            "main_video_post_id": None,
            "main_quote_post_id": None,
            "rubrics": [{"name": "СОБЫТИЯ", "post_ids": [4, 5, 6, 7, 8]}],
        }

        draft = build_digest_draft(posts)

        self.assertEqual(len(draft["main_block"]), 3)
        self.assertEqual(draft["main_figure"]["value"], "120")
        self.assertEqual(draft["_stats"]["excluded_posts"], 1)
        self.assertEqual(draft["_stats"]["placed_posts"], 8)

        locations = {row["title"]: row["rubric"] for row in draft["_routing"]}
        self.assertEqual(locations["Педиатр по ДМС"], "ЗАБОТА О ЛЮДЯХ")
        self.assertEqual(locations["Подготовка к ЕГЭ"], "КАРЬЕРА")
        self.assertEqual(locations["Форум руководства"], "СОБЫТИЯ")
        self.assertEqual(locations["Команда получила награду"], "ДОСТИЖЕНИЯ")
        self.assertEqual(locations["Найдена кружка"], "ИСКЛЮЧЕНО")

    @patch("src.pipeline.rewrite_card", side_effect=_rewrite)
    @patch("src.pipeline.plan_digest")
    @patch("src.pipeline.classify_posts", side_effect=_classified)
    def test_overflow_is_preserved_instead_of_truncated(
        self, _mock_classify, mock_plan, _mock_rewrite
    ):
        posts = [
            _post(
                f"Производственная новость {index}",
                f"Производство завершило операцию {index}.",
                "ПРОИЗВОДСТВО",
                importance=10 - min(index, 5),
            )
            for index in range(1, 11)
        ]
        mock_plan.return_value = {
            "subject_topics": [],
            "main_block": [{"post_id": 1}, {"post_id": 2}, {"post_id": 3}],
            "main_figure_post_id": None,
            "main_video_post_id": None,
            "main_quote_post_id": None,
        }

        draft = build_digest_draft(posts)
        production = next(r for r in draft["rubrics"] if r["name"] == "ПРОИЗВОДСТВО")

        self.assertEqual(len(production["cards"]), 7)
        self.assertEqual(draft["_stats"]["placed_posts"], 10)
        self.assertTrue(any("Все сохранены" in warning for warning in draft["warnings"]))
        self.assertEqual(tuple(r["name"] for r in draft["rubrics"]), NEWS_RUBRICS)


if __name__ == "__main__":
    unittest.main()
