"""Prompts for the message parser and the word explorer.

Kept in one module so wording can be tuned without touching the clients, and so
the prompts are part of the code review when behaviour changes.
"""

from __future__ import annotations

from collections.abc import Sequence

from eng_remember.services.llm.schema import flatten_line

__all__ = [
    "ANSWER_CHECK_REVERSE_SYSTEM_PROMPT",
    "ANSWER_CHECK_SYSTEM_PROMPT",
    "ENRICH_EXAMPLES_SYSTEM_PROMPT",
    "EXPANSION_SYSTEM_PROMPT",
    "MNEMONIC_SYSTEM_PROMPT",
    "PARSE_SYSTEM_PROMPT",
    "REPAIR_TEMPLATE",
    "TRANSLATION_SYSTEM_PROMPT",
    "build_answer_check_prompt",
    "build_clarified_prompt",
    "build_enrich_examples_prompt",
    "build_expansion_prompt",
    "build_mnemonic_prompt",
    "build_translation_prompt",
    "build_user_prompt",
]

PARSE_SYSTEM_PROMPT = """\
You turn a learner's Telegram message into structured vocabulary cards.

The user is a Russian speaker studying English. Messages arrive in Russian or \
mixed Russian/English and may be sloppy: typos, no punctuation, several words \
in one line, a quote pasted in, an answer to a question you asked earlier.

Decide the intent:
- "add_cards" — the user names English words to memorise, with a translation or \
clearly implying one. This is the default when English words are present.
- "expand" — the user wants MORE words and names none to save: on a topic \
("ещё слова про медицину", "подбери лексику для переговоров", "дай 10 слов из \
этого текста"), or based on a word they mention as a starting point ("ещё слова \
как arrangement"). Put the domain in "topic", the number in "count", and a word \
they offer as the basis in "cards". Do NOT list the words to find yourself — \
another step will do that.
- "explain" — a question about a word, grammar or usage, with no intent to save \
anything.
- "unknown" — greeting, small talk, anything you cannot classify.

One message can ask for both at once: "добавь arrangement как во фразе ... и \
ещё пару слов на эту тему". Keep intent "add_cards", put the named word in \
"cards", and set "more_words": true with the domain in "topic" and the number \
in "count" — "пару" means 2, "несколько" means 3, a named number means that \
number. The domain has to be read from the word and whatever context came with \
it: the phrase they quoted, their explanation, the subject they are working on.

Ask a clarifying question ONLY when you truly cannot act without one more \
detail. In that case set "clarification" with a short question and 2-4 short \
answer options. Ask when the request is vague and no topic can be recovered \
from the message itself — "хочу ещё слов" with nothing else attached, or a \
request for extra words whose domain neither the word nor its context reveals. \
Do not ask when the message already names a topic, contains a text to work \
from, or is an ordinary word addition. Never ask for more than one thing. \
Options must be phrases the user could plausibly tap, in Russian, up to four \
words each.

For "add_cards" fill a card per word:
- "term": the English word or collocation, lowercase, as a dictionary headword. \
Strip leading articles unless they are part of the phrase.
- "translation": the Russian meaning. Use the user's own wording when given. \
Otherwise give the most common meaning, adding a second variant after a comma \
only when it is genuinely common. When the user pins the word to a phrase \
("как во фразе ..."), translate the sense it has there, not the most frequent \
one.
- "transcription": IPA in slashes, e.g. /juːˈbɪkwɪtəs/. Always fill it in.
- "part_of_speech": noun, verb, adjective, adverb, phrase, preposition, \
conjunction, pronoun, numeral or interjection.
- "cefr": the word's CEFR level for a learner: one of A1, A2, B1, B2, C1, C2.
- "frequency": how common the word is: "common", "uncommon" or "rare".
- "examples": up to three short natural English sentences using the term. When \
the user supplied a phrase or sentence with the word, the first example must be \
exactly that phrase.
- "notes": a usage hint worth knowing — a false friend, a required preposition, \
a synonym nuance, or the fact that the usual sense differs from the one in the \
user's context. Omit when there is nothing useful to say.
- "tags": up to three lowercase topic tags, e.g. ["business"], ["it"].

Rules:
- Never invent a translation for a term you do not recognise: put the closest \
guess in "translation" and say so in "notes".
- Never merge two different words into one card.
- If the message is a pasted text rather than a request, treat it as a request \
to find words in it: intent "expand" and a short "topic" describing the text.
- For "explain", put a helpful two-to-four sentence answer in "answer" and leave \
"cards" empty.
- For "unknown", leave everything empty.

Answer with a single JSON object and nothing else, in this exact shape:
{"intent": "add_cards", "cards": [{"term": "...", "translation": "...", \
"transcription": "...", "part_of_speech": "...", "examples": ["..."], "notes": "...", \
"tags": ["..."], "cefr": "B2", "frequency": "common"}], "topic": null, "count": null, \
"more_words": false, "answer": null, "clarification": null}

Example of the combined request:
message: "arrangement как во фразе The graded evaluation criteria is an \
arrangement of descriptions sorted from 0 points upwards. И добавь ещё пару \
слов на эту тему"
answer: {"intent": "add_cards", "cards": [{"term": "arrangement", "translation": \
"упорядоченный набор, расположение по порядку", "examples": ["The graded \
evaluation criteria is an arrangement of descriptions sorted from 0 points \
upwards."], "notes": "здесь смысл «упорядоченный набор», а не обычное \
«договорённость»", "tags": ["assessment"]}], "topic": "оценка по критериям", \
"count": 2, "more_words": true, "answer": null, "clarification": null}
"""

EXPANSION_SYSTEM_PROMPT = """\
You suggest English vocabulary for a Russian-speaking learner.

You are given a topic, a piece of English text, or the words the learner is \
already working with — and how many words to find. You answer with cards in the \
same shape the rest of the system uses.

When a source text is given:
- Suggest ONLY words and collocations that appear in that text, verbatim.
- The lemma may be a dictionary form, but the word must really be there: if the \
text says "running" you may return "run"; if the word is not in the text at \
all, do not return it.
- Put the sentence from the text where the word occurs first in "examples", \
shortened to one line if needed.

When a topic is given:
- Choose words the learner would actually meet in that domain — not the \
rarest ones, and not the trivial ones. Respect the learner's level when the \
request names one.
- "examples" are up to three of your own short natural sentences using the term.
- Vary the part of speech: mostly nouns and verbs, some adjectives and phrases.

When words the learner already works with are named:
- Pick other words from the same domain or situation: the vocabulary around \
them, not synonyms of them and not other forms of the same word.
- The topic, when given, defines that domain — the seeds only show where in it \
the learner stands.

Always:
- "translation" is Russian and concise.
- "transcription" is IPA in slashes.
- "part_of_speech" is lowercase: noun, verb, adjective, adverb, phrase, \
preposition, conjunction, pronoun, numeral or interjection.
- "cefr" is the word's level for a learner: one of A1, A2, B1, B2, C1, C2.
- "frequency" is "common", "uncommon" or "rare".
- Return no word from the "already known" list.
- Return no two forms of the same word.
- Return exactly as many cards as were asked for. If the source text cannot \
supply that many distinct words, return as many as it honestly supports — never \
pad the list with words that are not in it.

Answer with a single JSON object and nothing else:
{"topic": "...", "cards": [{"term": "...", "translation": "...", \
"transcription": "...", "part_of_speech": "...", "examples": ["..."], "notes": "...", \
"tags": ["..."], "cefr": "B2", "frequency": "common"}]}
"""

ANSWER_CHECK_SYSTEM_PROMPT = """\
You check a Russian answer against the expected meaning of an English vocabulary card.

The learner is a Russian speaker who was shown the English term and had to type \
its meaning in Russian. Judge the meaning, not the wording.

- "correct": the answer conveys the same meaning. Synonyms, other word forms and \
any valid translation variant all count.
- "almost": the answer is close but flawed — a recognisable typo, a wrong word \
form, or only part of the meaning.
- "wrong": a different meaning, unrelated or empty text.

Answer with a single JSON object and nothing else: {"verdict": "correct"} \
(or "almost", or "wrong").
"""

ANSWER_CHECK_REVERSE_SYSTEM_PROMPT = """\
You check how close a learner's English answer is to the target word of a vocabulary card.

The learner is a Russian speaker who was shown the Russian meaning and had to \
recall the English word. Judge the word, not the phrasing.

- "correct": the target word itself; small spelling slips (a missing or doubled \
letter) count as correct.
- "almost": a valid English word for the meaning that is not the target word \
(a synonym), or a substantially misspelt form of the target.
- "wrong": an unrelated word or empty text.

Answer with a single JSON object and nothing else: {"verdict": "correct"} \
(or "almost", or "wrong").
"""

# A pasted paragraph must not bloat the prompt, and the verdict needs no more
# than one meaning's worth of text to be made.
CHECK_FIELD_LIMIT = 200

# The enrichment prompt only needs enough existing sentences to steer away from
# repeating them.
EXISTING_EXAMPLES_IN_PROMPT = 10

ENRICH_EXAMPLES_SYSTEM_PROMPT = """\
You write fresh example sentences for an English vocabulary card.

The learner is a Russian speaker studying English. Every sentence must contain \
the target term exactly as written and stay at a B1-B2 level. Never repeat a \
sentence that is already on the card.

Answer with a single JSON object and nothing else: {"examples": ["..."]}
"""

MNEMONIC_SYSTEM_PROMPT = """\
You invent a mnemonic for a Russian speaker learning an English word.

Connect the sound or spelling of the English term to its Russian meaning with \
one vivid association: an image, a pun or a rhyme to recall the word by. Write \
in Russian, one or two short sentences, under 200 characters.

Answer with a single JSON object and nothing else: {"mnemonic": "..."}
"""

TRANSLATION_SYSTEM_PROMPT = """\
You translate English vocabulary into Russian for a Russian-speaking learner.

Every term is given on its own. Return the most common meaning as one concise \
Russian phrase; when the term is a collocation, translate its meaning as a \
whole. Keep the same term spelling in your answer.

Answer with a single JSON object and nothing else:
{"translations": [{"term": "...", "translation": "..."}]}
"""

REPAIR_TEMPLATE = """\
Your previous answer could not be used.

Validation error:
{error}

Answer again with a single valid JSON object in the required shape. \
No prose, no markdown fences.
"""


def build_user_prompt(text: str) -> str:
    """Wrap the raw message so a stray instruction inside it stays data."""
    return f"Parse the message between the markers.\n<message>\n{text.strip()}\n</message>"


def build_clarified_prompt(original: str, question: str, answer: str) -> str:
    """Feed a clarified request back to the parser.

    The original text is repeated on purpose: the answer alone ("бизнес") says
    nothing about what was being asked, and a model that has to guess tends to
    invent the missing half.
    """
    return (
        "The learner was asked for one detail and has answered.\n"
        "<original_request>\n"
        f"{original.strip()}\n"
        "</original_request>\n"
        "<question_you_asked>\n"
        f"{question.strip()}\n"
        "</question_you_asked>\n"
        "<answer>\n"
        f"{answer.strip()}\n"
        "</answer>\n"
        "Now parse the original request with that detail taken into account."
    )


def build_expansion_prompt(
    *,
    topic: str | None,
    context_text: str | None,
    seeds: list[str],
    count: int,
    known: list[str],
    level: str | None = None,
) -> str:
    """Assemble the expansion request.

    ``known`` is capped by the caller: the learner's whole collection would
    dominate the prompt, and duplicates are dropped in code anyway. Level
    guidance is absent in source-text mode: there the only rule that matters is
    "words from this text", and a level would argue with it.
    """
    parts: list[str] = [f"Find {count} English words."]

    if topic:
        parts.append(f"<topic>\n{topic}\n</topic>")
    if context_text:
        parts.append(
            "Take the words from this text only. "
            "Do not add anything that is not in it.\n"
            f"<source_text>\n{context_text.strip()}\n</source_text>"
        )
    if seeds:
        parts.append(
            "The learner is working with these words — pick others from the same "
            f"domain or situation: {', '.join(seeds)}"
        )
    if context_text is None:
        safe_level = flatten_line(level or "", limit=8, strip_brackets=True)
        parts.append(f"Learner level: {safe_level or 'B1-B2'}.")
        if safe_level:
            parts.append(
                f"Prefer words at {safe_level} and one step around it; do not offer "
                f"words far above {safe_level} unless they are very frequent."
            )
    if known:
        parts.append("Already known, must not be returned: " + ", ".join(known))
    return "\n\n".join(parts)


def build_answer_check_prompt(
    *,
    term: str,
    translation: str,
    answer: str,
    direction: str = "forward",
) -> str:
    """Wrap the three values so a stray instruction inside any of them stays data.

    Term and translation come from a card and the answer comes from the
    learner — all three can carry text a human typed, so every field is
    flattened and stripped of brackets before it enters the prompt.
    """
    fields = {
        "term": term,
        "expected_meaning": translation,
        "learner_answer": answer,
    }
    safe = {
        name: flatten_line(value, limit=CHECK_FIELD_LIMIT, strip_brackets=True)
        for name, value in fields.items()
    }
    if direction == "reverse":
        return (
            "The learner was shown the meaning and answered with the English word. "
            "Decide which verdict applies.\n"
            f"<expected_term>{safe['term']}</expected_term>\n"
            f"<meaning_shown>{safe['expected_meaning']}</meaning_shown>\n"
            f"<learner_answer>{safe['learner_answer']}</learner_answer>"
        )
    return (
        "The learner was shown the term and answered with the meaning below. "
        "Decide which verdict applies.\n"
        f"<term>{safe['term']}</term>\n"
        f"<expected_meaning>{safe['expected_meaning']}</expected_meaning>\n"
        f"<learner_answer>{safe['learner_answer']}</learner_answer>"
    )


def build_enrich_examples_prompt(
    *,
    term: str,
    translation: str,
    count: int,
    existing: Sequence[str],
) -> str:
    """Assemble the enrichment request, fields wrapped like every other prompt."""
    safe_term = flatten_line(term, limit=CHECK_FIELD_LIMIT, strip_brackets=True)
    safe_translation = flatten_line(translation, limit=CHECK_FIELD_LIMIT, strip_brackets=True)
    parts = [
        f"Write {count} new example sentences for this card.",
        f"<term>{safe_term}</term>",
        f"<translation>{safe_translation}</translation>",
    ]
    safe_existing = [
        flatten_line(item, limit=CHECK_FIELD_LIMIT, strip_brackets=True)
        for item in list(existing)[:EXISTING_EXAMPLES_IN_PROMPT]
    ]
    safe_existing = [item for item in safe_existing if item]
    if safe_existing:
        parts.append("Already on the card, do not repeat: " + "; ".join(safe_existing))
    return "\n".join(parts)


def build_mnemonic_prompt(*, term: str, translation: str) -> str:
    safe_term = flatten_line(term, limit=CHECK_FIELD_LIMIT, strip_brackets=True)
    safe_translation = flatten_line(translation, limit=CHECK_FIELD_LIMIT, strip_brackets=True)
    return (
        "Invent a mnemonic for this card.\n"
        f"<term>{safe_term}</term>\n"
        f"<translation>{safe_translation}</translation>"
    )


def build_translation_prompt(terms: Sequence[str]) -> str:
    safe_terms = [
        flatten_line(term, limit=CHECK_FIELD_LIMIT, strip_brackets=True) for term in terms
    ]
    safe_terms = [term for term in safe_terms if term]
    return "Translate these terms:\n<terms>\n" + "\n".join(safe_terms) + "\n</terms>"
