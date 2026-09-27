"""Проверка разнообразия пожеланий; факты о назначении хранятся отдельно."""
import html
import re
from difflib import SequenceMatcher

BRIEFS = (
    "Акцент: возможность применять знания в новой работе. Начни с пожелания, без поздравительной формулы.",
    "Акцент: взаимопонимание с коллегами. Подбери другое начало и ритм, чем в предыдущих карточках.",
    "Акцент: удовольствие от конкретного результата работы. Не обещай будущих успехов за человека.",
    "Акцент: освоение новых задач. Достаточно одного теплого предложения без перечисления трех благ.",
    "Акцент: возможность воплощать идеи. Учитывай должность, не приписывай человеку проекты или полномочия.",
    "Акцент: профессиональный рост. Избегай карьерных вершин, громких эпитетов и универсальных тостов.",
)
RESERVE_WISHES = (
    "Пусть новая работа дает возможность применять знания и видеть результат своих усилий.",
    "Желаем найти общий язык с коллегами и вместе решать новые задачи.",
    "Интересных задач и времени, чтобы разобраться в них основательно!",
    "Хорошего старта на новом месте! Пусть рядом будут коллеги, с которыми легко работать.",
    "Пусть среди новых задач найдется место для идей, которые давно хотелось попробовать.",
    "Желаем получать удовольствие от работы и находить в ней поводы для профессиональной гордости.",
    "Удачи в новой роли! Желаем поддержки коллег, когда она особенно нужна.",
    "Пусть знакомство с новой работой принесет полезные открытия.",
    "Желаем освоиться на новом месте и найти удобный для себя рабочий ритм.",
    "Новых знаний и возможностей применять их на практике!",
    "Пусть рабочий день заканчивается с чувством удовлетворения от сделанного.",
    "Желаем сохранять интерес к профессии и смело задавать вопросы, осваивая новые задачи.",
)


def plain(text):
    text = re.sub(r"<br\s*/?>", "\n", str(text or ""), flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def normalized(text):
    return " ".join(re.findall(r"[а-яa-z0-9]+", plain(text).lower().replace("ё", "е")))


def split_message(message):
    """Поддерживает новые карточки и старые сообщения с <br><br>."""
    text = plain(message)
    paragraphs = re.split(r"\n\s*\n", text)
    if len(paragraphs) > 1:
        return "\n\n".join(paragraphs[:-1]), paragraphs[-1]
    match = re.search(r"(?<=[.!?])\s+(?=(?:Сердечно поздравляем|Поздравляем|Желаем|Пусть|Удачи)\b)", text)
    if match:
        return text[:match.start()], text[match.end():]
    return "", text


def compose_message(transition, wish):
    return "<br><br>".join(html.escape(part.strip()).replace("\n", "<br>")
                          for part in (transition, wish) if part.strip())


def wish_problem(wish, used):
    current = normalized(wish)
    if len(current.split()) < 5:
        return "Пожелание отсутствует или слишком короткое."
    if len(plain(wish)) > 400:
        return "Сократи пожелание до одного-двух предложений."
    for phrase in ("сердечно поздравляем", "новых профессиональных вершин", "уверены что", "не сомневаемся"):
        if phrase in current:
            return "Убери шаблонную формулу и недоказанное обещание: " + phrase
    for other in used:
        previous = normalized(other)
        if not previous:
            continue
        if (current == previous
                or current.split()[:3] == previous.split()[:3]
                or SequenceMatcher(None, current, previous).ratio() >= 0.78):
            return "Пожелание повторяет начало или формулировку другой карточки. Измени и смысловой акцент, и структуру."
        sentences = {normalized(s) for s in re.split(r"[.!?]+", plain(other)) if len(normalized(s).split()) >= 4}
        if any(normalized(s) in sentences for s in re.split(r"[.!?]+", plain(wish))):
            return "Одно из предложений уже использовано в другой карточке."
    return ""


def reserve_wish(used):
    return next((wish for wish in RESERVE_WISHES if not wish_problem(wish, used)), "")
