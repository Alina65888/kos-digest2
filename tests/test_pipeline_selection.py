import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile

from src.pipeline import build_digest_draft, regenerate_card_headline
from src.templater import build_html


def fake_rewrite(post, context):
    if context.get("is_figure"):
        return {
            "value": post.get("number_value") or "",
            "description": post.get("number_desc") or "",
            "_quality_flags": [],
        }
    if context.get("is_quote"):
        return {
            "quote_text": post.get("quote_text") or "",
            "author_name": post.get("quote_author_name") or "",
            "author_role": post.get("quote_author_role") or "",
            "_quality_flags": [],
        }
    return {
        "title": (post.get("title") or "Заголовок").upper(),
        "text": post.get("text") or "Текст",
        "_quality_flags": [],
    }


def classified_from(posts, rubrics=None, importances=None):
    rubrics = rubrics or {}
    importances = importances or {}
    result = []
    for post in posts:
        pid = post["post_id"]
        result.append({
            **post,
            "topic": post.get("title", ""),
            "rubric_candidate": rubrics.get(pid, "ПРОИЗВОДСТВО"),
            "importance": importances.get(pid, 5),
            "has_number": False,
            "number_value": None,
            "number_desc": None,
            "has_quote": False,
            "is_video": False,
            "is_special": False,
            "summary_short": post.get("text", ""),
        })
    return result


class PipelineSelectionTests(unittest.TestCase):
    def test_headline_regeneration_preserves_existing_lead(self):
        draft = {
            "_classified": [
                {
                    "post_id": 1,
                    "title": "Ремонт установки",
                    "text": "Специалисты завершили ремонт установки.",
                }
            ],
            "rubrics": [
                {
                    "name": "ПРОИЗВОДСТВО",
                    "cards": [
                        {
                            "post_id": 1,
                            "position": 1,
                            "title": "СТАРЫЙ ЗАГОЛОВОК",
                            "text": "Согласованная подводка остается без изменений.",
                        }
                    ],
                }
            ],
        }
        with patch(
            "src.pipeline.rewrite_card",
            return_value={"title": "РЕМОНТ УСТАНОВКИ ЗАВЕРШЕН", "_quality_flags": []},
        ) as rewrite:
            card = regenerate_card_headline(draft, 0, 0)

        self.assertEqual(card["title"], "РЕМОНТ УСТАНОВКИ ЗАВЕРШЕН")
        self.assertEqual(card["text"], "Согласованная подводка остается без изменений.")
        self.assertTrue(rewrite.call_args.args[1]["title_only"])

    def test_quote_can_be_the_only_item_in_its_rubric(self):
        draft = {
            "subject_topics": ["испытания"],
            "main_block": [],
            "main_figure": None,
            "main_video": None,
            "main_quote": {
                "text": "Отрицательный результат уточняет требования",
                "author_name": "Булат Яруллин",
                "author_role": "ведущий инженер-технолог",
                "photo_file": "",
            },
            "main_quote_rubric": "ПРОИЗВОДСТВО",
            "rubrics": [
                {
                    "name": "ПРОИЗВОДСТВО",
                    "icon": "rubric_production.png",
                    "cards": [],
                    "quote_before": 0,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            html_path = build_html(draft, Path(tmp), Path(tmp), "QUOTE")
            html = html_path.read_text(encoding="utf-8")
        self.assertIn("Отрицательный результат уточняет требования", html)
        self.assertIn("/ ПРОИЗВОДСТВО /", html)

    def test_no_figure_is_invented_and_routing_is_not_forced(self):
        posts = [
            {"title": "Производственный результат", "text": "Команда завершила ремонт оборудования.", "date": "2026-08-30"},
            {"title": "Мини-Т", "text": "Команда внедряет Мини-Т.", "date": "2026-08-29"},
            {"title": "Курс безопасности", "text": "Открыт курс по безопасности.", "date": "2026-08-28"},
            {"title": "ДМС", "text": "Сотрудникам доступен ДМС.", "date": "2026-08-27"},
            {"title": "Анонс форума", "text": "30 августа пройдет форум, нужна регистрация.", "date": "2026-08-20"},
            {"title": "Итоги форума", "text": "30 августа прошел форум.", "date": "2026-08-30"},
            {"title": "Остановку обновили", "text": "Сотрудники просили обновить остановку. Работы завершены.", "date": "2026-08-26"},
            {"title": "Мелкий анонс", "text": "Приглашаем на короткую встречу.", "date": "2026-08-25"},
        ]
        rubrics = {
            1: "ПРОИЗВОДСТВО",
            2: "ПСС",
            3: "БЕЗОПАСНОСТЬ",
            4: "ЗАБОТА О ЛЮДЯХ",
            5: "СОБЫТИЯ",
            6: "СОБЫТИЯ",
            7: "ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ",
            8: "СОБЫТИЯ",
        }
        importances = {1: 8, 2: 7, 3: 6, 4: 5, 5: 4, 6: 6, 7: 4, 8: 2}

        def fake_classify(input_posts, progress=None):
            return classified_from(input_posts, rubrics, importances)

        plan = {
            "subject_topics": ["ремонт", "Мини-Т", "форум"],
            "main_block": [{"post_id": 1, "title": "Команда завершила ремонт оборудования"}],
            "main_figure_post_id": 4,
            "main_video_post_id": None,
            "main_quote_post_id": None,
            "rubrics": [
                {"name": "СОБЫТИЯ", "post_ids": list(range(1, 9)), "quote_position": None},
            ],
            "skipped": [],
        }

        with (
            patch("src.pipeline.classify_posts", side_effect=fake_classify),
            patch("src.pipeline.plan_digest", return_value=plan),
            patch("src.pipeline.rewrite_card", side_effect=fake_rewrite),
        ):
            draft = build_digest_draft(posts)

        self.assertIsNone(draft["main_figure"])
        self.assertEqual(draft["main_block"][0]["title"], "ПРОИЗВОДСТВЕННЫЙ РЕЗУЛЬТАТ")
        self.assertNotIn("Анонс форума", [c["title"] for r in draft["rubrics"] for c in r["cards"]])
        self.assertTrue(any("Анонс исключен" in item["reason"] for item in draft["excluded"]))
        rubric_names = [rubric["name"] for rubric in draft["rubrics"]]
        self.assertIn("ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ", rubric_names)
        self.assertNotIn("КАРЬЕРА", rubric_names)
        self.assertTrue(any(item["title"] == "Мелкий анонс" for item in draft["excluded"]))

    def test_you_asked_post_stays_in_its_rubric_even_if_plan_marks_it_main(self):
        posts = [
            {
                "title": "Производственный результат",
                "text": "Команда завершила ремонт оборудования.",
                "date": "2026-08-30",
            },
            {
                "title": "Остановку обновили",
                "text": "Сотрудники просили обновить остановку. Работы завершены.",
                "date": "2026-08-29",
            },
        ]
        rubrics = {
            1: "ПРОИЗВОДСТВО",
            2: "ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ",
        }
        importances = {1: 8, 2: 8}

        def fake_classify(input_posts, progress=None):
            return classified_from(input_posts, rubrics, importances)

        plan = {
            "subject_topics": ["ремонт", "остановка"],
            "main_block": [
                {"post_id": 1, "title": "Производственный результат"},
                {"post_id": 2, "title": "Остановку обновили"},
            ],
            "main_figure_post_id": None,
            "main_video_post_id": None,
            "main_quote_post_id": None,
            "rubrics": [
                {"name": "ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ", "post_ids": [2], "quote_position": None},
            ],
            "skipped": [],
        }

        with (
            patch("src.pipeline.classify_posts", side_effect=fake_classify),
            patch("src.pipeline.plan_digest", return_value=plan),
            patch("src.pipeline.rewrite_card", side_effect=fake_rewrite),
        ):
            draft = build_digest_draft(posts)

        self.assertEqual([item["post_id"] for item in draft["main_block"]], [1])
        feedback = next(
            rubric for rubric in draft["rubrics"]
            if rubric["name"] == "ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ"
        )
        self.assertEqual([card["post_id"] for card in feedback["cards"]], [2])

    def test_rubric_limit_excludes_lower_priority_cards_without_silent_truncation(self):
        posts = [
            {"title": f"Производство {idx}", "text": f"Производственный пост {idx}.", "date": f"2026-08-{idx + 10:02d}"}
            for idx in range(1, 10)
        ]

        def fake_classify(input_posts, progress=None):
            return classified_from(input_posts)

        plan = {
            "subject_topics": [],
            "main_block": [],
            "main_figure_post_id": None,
            "main_video_post_id": None,
            "main_quote_post_id": None,
            "rubrics": [
                {"name": "ПРОИЗВОДСТВО", "post_ids": list(range(1, 10)), "quote_position": None},
            ],
            "skipped": [],
        }

        with (
            patch("src.pipeline.classify_posts", side_effect=fake_classify),
            patch("src.pipeline.plan_digest", return_value=plan),
            patch("src.pipeline.rewrite_card", side_effect=fake_rewrite),
        ):
            draft = build_digest_draft(posts)

        production = next(r for r in draft["rubrics"] if r["name"] == "ПРОИЗВОДСТВО")
        self.assertEqual(len(production["cards"]), 4)
        self.assertEqual(len(draft["excluded"]), 5)
        self.assertTrue(all("более значимые" in item["reason"] for item in draft["excluded"]))
        self.assertEqual(draft["_stats"]["placed_posts"] + draft["_stats"]["excluded_posts"], 9)


if __name__ == "__main__":
    unittest.main()
