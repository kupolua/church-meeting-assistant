"""
How the council speaks — one block of style rules for every prompt that
writes text people will read (topic titles and bodies in chunked_analyze,
the protocol wording in ingestion/protocol_draft).

WHY A SEPARATE MODULE. On 15.11.2026 the council read the first «Теми» on
their phones and all said the same thing: the text is correct and sounds like
a ministry report — «Регулювання поведінки Валентина» where they would say
«Оцінка поведінки Валентина». The fix the user asked for first was a glossary
in the prompt, not fine-tuning (no data for that yet). The glossary lives here
so the analysis and the draft say the same things in the same voice, and so
the examples can be replaced from `topic_corrections` once the council has
corrected enough topics in the app (web/routes/api_topics.py records every
«before → after» pair).

The model this is for is gemma4:26b through Ollama; a 26B model follows a
glossary like this reliably, which is why it goes before any training.
"""

from __future__ import annotations

# Pairs the council itself flagged. Left: how the model put it; right: how
# people in the church say it. Extend from topic_corrections, not from taste.
GLOSSARY: list[tuple[str, str]] = [
    ("Регулювання поведінки …", "Оцінка поведінки … / Розмова про поведінку …"),
    ("Здійснення заходів щодо …", "Що робимо з …"),
    ("Забезпечення функціонування служіння", "Щоб служіння працювало"),
    ("Оптимізація процесу …", "Як спростити …"),
    ("Проведення роботи з …", "Робота з … / Розмова з …"),
    ("Питання щодо …", "Про …"),
    ("Імплементація / реалізація рішення", "Виконання рішення / що вже зроблено"),
    ("Контроль за виконанням", "Хто перевірить"),
    ("Організація взаємодії між служіннями", "Як служінням працювати разом"),
    ("Виявлення проблемних аспектів", "Що не працює"),
]

STYLE_RULES = (
    "МОВА. Пиши так, як говорять між собою люди в церковній громаді, а не як "
    "у звіті установи. Назва теми — живе коротке речення або словосполучення "
    "про суть розмови, без канцеляриту: без «регулювання», «здійснення», "
    "«забезпечення», «оптимізація», «імплементація», «щодо», «питання про». "
    "Віддієслівні іменники заміняй дієсловами або простими словами. Імена "
    "людей — як їх називають у розмові. Не пом'якшуй і не офіціалізуй: якщо "
    "йшлося про поведінку людини — так і пиши «поведінка», «розмова з …».\n"
    "Приклади (ліворуч — так НЕ треба, праворуч — так треба):\n"
    + "\n".join(f"  «{bad}» → «{good}»" for bad, good in GLOSSARY)
)
