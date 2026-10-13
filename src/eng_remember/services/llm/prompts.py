"""Prompts for the message parser and the word explorer.

Kept in one module so wording can be tuned without touching the clients, and so
the prompts are part of the code review when behaviour changes.
"""

from __future__ import annotations

__all__ = [
    "EXPANSION_SYSTEM_PROMPT",
    "PARSE_SYSTEM_PROMPT",
    "REPAIR_TEMPLATE",
    "build_clarified_prompt",
    "build_expansion_prompt",
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
- "expand" — the user wants MORE words: on a topic ("ещё слова про медицину", \
"подбери лексику для переговоров", "дай 10 слов из этого текста"), or related \
to a word they just added. Put the domain in "topic" and, if they named a \
number, in "count". Do NOT list those words yourself — another step will find \
them. Put into "cards" only words the learner explicitly wants more of.
- "explain" — a question about a word, grammar or usage, with no intent to save \
anything.
- "unknown" — greeting, small talk, anything you cannot classify.

Ask a clarifying question ONLY when you truly cannot act without one more \
detail. In that case set "clarification" with a short question and 2-4 short \
answer options. Ask when the request is vague and no topic can be recovered \
from the message itself — "хочу ещё слов" with nothing else attached. Do not \
ask when the message already names a topic, contains a text to work from, or is \
an ordinary word addition. Never ask for more than one thing. Options must be \
phrases the user could plausibly tap, in Russian, up to four words each.

For "add_cards" fill a card per word:
- "term": the English word or collocation, lowercase, as a dictionary headword. \
Strip leading articles unless they are part of the phrase.
- "translation": the Russian meaning. Use the user's own wording when given. \
Otherwise give the most common meaning, adding a second variant after a comma \
only when it is genuinely common.
- "transcription": IPA in slashes, e.g. /juːˈbɪkwɪtəs/. Always fill it in.
- "part_of_speech": noun, verb, adjective, adverb, phrase, preposition, \
conjunction, pronoun, numeral or interjection.
- "example": one short natural English sentence using the term.
- "notes": a usage hint worth knowing — a false friend, a required preposition, \
a synonym nuance. Omit when there is nothing useful to say.
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
"transcription": "...", "part_of_speech": "...", "example": "...", "notes": "...", \
"tags": ["..."]}], "topic": null, "count": null, "answer": null, "clarification": null}
"""

EXPANSION_SYSTEM_PROMPT = """\
You suggest English vocabulary for a Russian-speaking learner.

You are given either a topic or a piece of English text, and how many words to \
find. You answer with cards in the same shape the rest of the system uses.

When a source text is given:
- Suggest ONLY words and collocations that appear in that text, verbatim.
- The lemma may be a dictionary form, but the word must really be there: if the \
text says "running" you may return "run"; if the word is not in the text at \
all, do not return it.
- Put the sentence from the text where the word occurs into "example", \
shortened to one line if needed.

When only a topic is given:
- Choose words a B1-B2 learner would actually meet in that domain — not the \
rarest ones, and not the trivial ones.
- "example" is your own short natural sentence using the term.
- Vary the part of speech: mostly nouns and verbs, some adjectives and phrases.

Always:
- "translation" is Russian and concise.
- "transcription" is IPA in slashes.
- "part_of_speech" is lowercase: noun, verb, adjective, adverb, phrase, \
preposition, conjunction, pronoun, numeral or interjection.
- Return no word from the "already known" list.
- Return no two forms of the same word.
- Return exactly as many cards as were asked for. If the source text cannot \
supply that many distinct words, return as many as it honestly supports — never \
pad the list with words that are not in it.

Answer with a single JSON object and nothing else:
{"topic": "...", "cards": [{"term": "...", "translation": "...", \
"transcription": "...", "part_of_speech": "...", "example": "...", "notes": "...", \
"tags": ["..."]}]}
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
) -> str:
    """Assemble the expansion request.

    ``known`` is capped by the caller: the learner's whole collection would
    dominate the prompt, and duplicates are dropped in code anyway.
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
            "The learner is working with these words, so pick others related to "
            f"the same idea: {', '.join(seeds)}"
        )
    if known:
        parts.append("Already known, must not be returned: " + ", ".join(known))
    return "\n\n".join(parts)
