import unittest
from datetime import date

from src.editorial_rules import normalize_classification, validate_rewrite_output
from src.excel_loader import filter_posts_by_period


class EditorialRulesTests(unittest.TestCase):
    def _post(self, title, text, author=""):
        return {"title": title, "text": text, "author": author, "link": "https://example.test/post"}

    def test_strong_content_rules_correct_wrong_model_rubric(self):
        cases = [
            (
                self._post("Мини-Т", "Команда внедряет Мини-Т и бережливую эксплуатацию оборудования."),
                "ПСС",
            ),
            (
                self._post("Курс", "Открыт курс по культуре безопасности, охране труда и Стоп-карте."),
                "БЕЗОПАСНОСТЬ",
            ),
            (
                self._post("ДМС", "Сотрудникам доступен ДМС СОГАЗ и медицинская помощь."),
                "ЗАБОТА О ЛЮДЯХ",
            ),
            (
                self._post("Серебро", "Команда заняла второе место в турнире."),
                "ДОСТИЖЕНИЯ",
            ),
            (
                self._post("Остановку обновили", "Сотрудники просили заменить павильон. Работы завершены."),
                "ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ",
            ),
        ]
        for post, expected in cases:
            with self.subTest(expected=expected):
                result = normalize_classification(
                    post,
                    {
                        "rubric_candidate": "СОБЫТИЯ",
                        "importance": 5,
                        "summary_short": post["text"],
                    },
                )
                self.assertEqual(result["rubric_candidate"], expected)

    def test_unverified_number_is_removed(self):
        post = self._post("Встреча", "Сотрудники провели рабочую встречу. Количество участников не указано.")
        result = normalize_classification(
            post,
            {
                "rubric_candidate": "СОБЫТИЯ",
                "importance": 4,
                "has_number": True,
                "number_value": "500",
                "number_desc": "500 участников",
                "summary_short": post["text"],
            },
        )
        self.assertFalse(result["has_number"])
        self.assertIsNone(result["number_value"])

    def test_quote_is_restored_from_source(self):
        post = self._post(
            "Испытания масла",
            "«Отрицательный результат уточняет требования», – сказал Булат Яруллин.",
            "Булат Яруллин, ведущий инженер-технолог",
        )
        result = normalize_classification(
            post,
            {
                "rubric_candidate": "ПРОИЗВОДСТВО",
                "importance": 6,
                "has_quote": True,
                "quote_text": "Мы уже добились успеха",
                "quote_author_name": "Булат Яруллин",
                "summary_short": post["text"],
            },
        )
        self.assertTrue(result["has_quote"])
        self.assertEqual(result["quote_text"], "Отрицательный результат уточняет требования")

    def test_rewrite_with_new_number_and_html_falls_back_to_source(self):
        post = self._post(
            "Экономия энергии",
            "Экономический эффект за июль составил 2,66 млн рублей.",
        )
        result, flags = validate_rewrite_output(
            post,
            {"rubric": "ПРОИЗВОДСТВО"},
            {
                "title": "ЭКОНОМИЯ ДОСТИГЛА 5 МИЛЛИОНОВ",
                "text": "<script>alert(1)</script> Эффект составил 5 млн рублей.",
            },
        )
        self.assertNotIn("5", result["title"])
        self.assertNotIn("5", result["text"])
        self.assertNotIn("script", result["text"].lower())
        self.assertTrue(flags)

    def test_rich_source_expands_an_overly_short_lead(self):
        post = self._post(
            "Остановочный ремонт завершен",
            (
                "Специалисты завершили остановочный ремонт на заводе поликарбонатов. "
                "Во время работ проверили оборудование и заменили изношенные узлы. "
                "Команда провела контрольный осмотр перед запуском производства. "
                "Оборудование вернули в работу по утвержденному графику."
            ),
        )
        result, flags = validate_rewrite_output(
            post,
            {"rubric": "ПРОИЗВОДСТВО"},
            {
                "title": "ОСТАНОВОЧНЫЙ РЕМОНТ ЗАВЕРШЕН",
                "text": "Специалисты завершили ремонт.",
            },
        )
        plain = result["text"].replace("&quot;", '"')
        self.assertGreaterEqual(len(plain), 170)
        self.assertGreaterEqual(plain.count("."), 2)
        self.assertTrue(any("дополнена" in flag for flag in flags))

    def test_short_source_is_not_padded_with_generic_language(self):
        post = self._post("Компрессор запущен", "После ремонта компрессор запущен в работу.")
        result, flags = validate_rewrite_output(
            post,
            {"rubric": "ПРОИЗВОДСТВО"},
            {
                "title": "КОМПРЕССОР ЗАПУЩЕН",
                "text": "После ремонта компрессор запущен в работу.",
            },
        )
        self.assertIn("компрессор", result["text"].lower())
        self.assertFalse(any("дополнена" in flag for flag in flags))

    def test_main_headline_does_not_require_a_lead(self):
        post = self._post("Серебро команды", "Команда заняла второе место в турнире.")
        result, flags = validate_rewrite_output(
            post,
            {"is_main_block": True},
            {"title": "Серебро команды"},
        )
        self.assertEqual(result, {"title": "Серебро команды"})
        self.assertEqual(flags, [])

    def test_headline_constructor_chooses_concrete_candidate(self):
        post = self._post(
            "Ремонт установки",
            (
                "Во время ремонта установки специалисты заменили 12 изношенных узлов. "
                "Оборудование вернули в работу по утвержденному графику."
            ),
        )
        result, flags = validate_rewrite_output(
            post,
            {"rubric": "ПРОИЗВОДСТВО"},
            {
                "title": "ВАЖНЫЙ ШАГ ДЛЯ ПРОИЗВОДСТВА",
                "title_candidates": [
                    "НОВЫЙ УРОВЕНЬ РЕМОНТА",
                    "18 УЗЛОВ ЗАМЕНИЛИ ЗА РЕМОНТ",
                    "12 УЗЛОВ ЗАМЕНИЛИ ЗА РЕМОНТ",
                    "КОМАНДА СНОВА ДОКАЗАЛА МАСТЕРСТВО",
                ],
                "text": post["text"],
            },
        )
        self.assertEqual(result["title"], "12 УЗЛОВ ЗАМЕНИЛИ ЗА РЕМОНТ")
        self.assertFalse(any("Заголовок заменен" in flag for flag in flags))

    def test_headline_does_not_turn_a_plan_into_a_completed_result(self):
        post = self._post(
            "Испытания компрессора",
            (
                "Команда продолжает испытания компрессора. "
                "Компрессор планируют запустить в сентябре."
            ),
        )
        result, _ = validate_rewrite_output(
            post,
            {"rubric": "ПРОИЗВОДСТВО", "title_only": True},
            {
                "title": "КОМПРЕССОР ЗАПУЩЕН В СЕНТЯБРЕ",
                "title_candidates": [
                    "ИСПЫТАНИЯ КОМПРЕССОРА ПРОДОЛЖАЮТСЯ",
                    "КОМПРЕССОР ЗАПУЩЕН ПОСЛЕ ИСПЫТАНИЙ",
                ],
            },
        )
        self.assertEqual(result, {"title": "ИСПЫТАНИЯ КОМПРЕССОРА ПРОДОЛЖАЮТСЯ"})

    def test_headline_constructor_rejects_generic_candidates(self):
        post = self._post(
            "Обучение операторов",
            "Операторы прошли обучение работе на новой панели управления.",
        )
        result, flags = validate_rewrite_output(
            post,
            {"rubric": "КАРЬЕРА", "title_only": True},
            {
                "title": "НОВЫЙ УРОВЕНЬ",
                "title_candidates": ["ВАЖНЫЙ ШАГ", "В ЦЕНТРЕ ВНИМАНИЯ"],
            },
        )
        self.assertEqual(result, {"title": "ОБУЧЕНИЕ ОПЕРАТОРОВ"})
        self.assertTrue(any("Заголовок заменен" in flag for flag in flags))

    def test_concrete_detail_beats_protocol_nominalization(self):
        post = self._post(
            "Остановочный павильон обновили",
            (
                "На встречах сотрудники просили обновить остановочный павильон у проходной. "
                "Подрядчик заменил крышу и скамейки, установил освещение. Работы завершены."
            ),
        )
        result, _ = validate_rewrite_output(
            post,
            {"rubric": "ВЫ ПРОСИЛИ — МЫ СДЕЛАЛИ", "title_only": True},
            {
                "title": "ОБНОВЛЕНИЕ ОСТАНОВКИ ЗАВЕРШЕНО",
                "title_candidates": [
                    "КРЫША, СКАМЕЙКИ И СВЕТ ДЛЯ ОСТАНОВКИ",
                    "СОТРУДНИКИ ДОЖДАЛИСЬ ОБНОВЛЕНИЯ ОСТАНОВКИ",
                ],
            },
        )
        self.assertEqual(result, {"title": "КРЫША, СКАМЕЙКИ И СВЕТ ДЛЯ ОСТАНОВКИ"})

    def test_period_filter_is_inclusive_and_keeps_undated_rows(self):
        posts = [
            {"row_idx": 2, "date": "2026-08-17", "title": "Граница", "text": "Текст"},
            {"row_idx": 3, "date": "2026-08-16", "title": "Старый", "text": "Текст"},
            {"row_idx": 4, "date": "", "title": "Без даты", "text": "Текст"},
        ]
        kept, excluded, warnings = filter_posts_by_period(posts, date(2026, 8, 30), 14)
        self.assertEqual([p["title"] for p in kept], ["Граница", "Без даты"])
        self.assertEqual([p["title"] for p in excluded], ["Старый"])
        self.assertTrue(any("не указана дата" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
