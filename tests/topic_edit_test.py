"""Rewriting one ### topic of polished.md: the rest of the file is untouched."""

import pytest

from church_assistant.shared.meetings_index import (
    NoSuchTopic, _parse_topics_from_polished, replace_topic,
)

DOC = """# Протокол

## Присутні
- Павло
- Віктор

## Розглянуті питання

### Регулювання поведінки Валентина
Обговорення поведінки на служінні.

- Віктор розповів про випадок (12:01)
  - Деталь (12:30)

<!-- continued in chunk 01 -->

### Друга тема
Текст другої теми.

- Теза (20:00)

## Наступні кроки

### Павло
- Поговорити з Валентином (12:40)
"""


def test_title_only_keeps_body_comments_and_the_rest():
    new, old = replace_topic(DOC, 0, title="Оцінка поведінки Валентина")
    assert old.title == "Регулювання поведінки Валентина"
    assert old.body.startswith("Обговорення поведінки") and "<!--" not in old.body
    assert "### Оцінка поведінки Валентина\nОбговорення поведінки на служінні." in new
    assert "<!-- continued in chunk 01 -->\n\n### Друга тема" in new
    # the actions section and the attendees are byte-for-byte the same
    assert new.split("## Наступні кроки")[1] == DOC.split("## Наступні кроки")[1]
    assert new.split("## Розглянуті питання")[0] == DOC.split("## Розглянуті питання")[0]
    topics = _parse_topics_from_polished(new)
    assert [t.title for t in topics] == ["Оцінка поведінки Валентина", "Друга тема"]


def test_body_only_replaces_between_headings():
    new, old = replace_topic(DOC, 1, body="Нова розмова.\n\n- Інша теза (20:00)")
    assert old.body == "Текст другої теми.\n\n- Теза (20:00)"
    topics = _parse_topics_from_polished(new)
    assert topics[1].title == "Друга тема" and topics[1].body == "Нова розмова.\n\n- Інша теза (20:00)"
    assert topics[0].body == _parse_topics_from_polished(DOC)[0].body
    assert "## Наступні кроки\n\n### Павло" in new


def test_people_under_next_steps_are_not_topics():
    with pytest.raises(NoSuchTopic):
        replace_topic(DOC, 2, title="x")


def test_empty_title_is_refused_and_whitespace_is_folded():
    with pytest.raises(ValueError):
        replace_topic(DOC, 0, title="   ")
    new, _ = replace_topic(DOC, 0, title="  Оцінка   поведінки ")
    assert "### Оцінка поведінки\n" in new


def test_no_change_is_identity():
    new, _ = replace_topic(DOC, 0)
    assert _parse_topics_from_polished(new) == _parse_topics_from_polished(DOC)
