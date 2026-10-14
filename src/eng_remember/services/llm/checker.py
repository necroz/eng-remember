"""Checking a typed answer against a card, once the simple match has failed.

The third LLM call in the system and the cheapest to avoid: it only runs when
the answer is not a literal match, and its whole job is the judgement a string
comparison cannot make — "повсеместный" for "вездесущий" is correct,
"вездесущный" is almost, "ужасный" is wrong.
"""

from __future__ import annotations

from eng_remember.domain.answers import Verdict
from eng_remember.errors import LlmNotConfiguredError
from eng_remember.services.llm.client import JsonChatClient
from eng_remember.services.llm.prompts import (
    ANSWER_CHECK_REVERSE_SYSTEM_PROMPT,
    ANSWER_CHECK_SYSTEM_PROMPT,
    build_answer_check_prompt,
)
from eng_remember.services.llm.schema import AnswerCheckResponse

__all__ = ["AnswerChecker"]

PURPOSE_CHECK = "check_answer"


class AnswerChecker:
    def __init__(self, llm: JsonChatClient | None = None) -> None:
        self._llm = llm

    @property
    def available(self) -> bool:
        return self._llm is not None and self._llm.available

    async def check(
        self,
        *,
        term: str,
        translation: str,
        answer: str,
        direction: str = "forward",
        user_id: int | None = None,
    ) -> Verdict:
        """Ask the model to grade the answer. Raises :class:`LlmNotConfiguredError`.

        Refusing instead of returning "wrong" matters: the caller has to know
        the model was never asked, so its own fallback decision stays visible in
        the code rather than being mistaken for a verdict.

        ``direction`` decides which side the learner was shown: "reverse" means
        the expected answer is the English term, not the Russian meaning.
        """
        llm = self._llm
        if llm is None or not llm.available:
            raise LlmNotConfiguredError("answer checking needs a configured LLM")

        system = (
            ANSWER_CHECK_REVERSE_SYSTEM_PROMPT
            if direction == "reverse"
            else ANSWER_CHECK_SYSTEM_PROMPT
        )
        response = await llm.complete_json(
            purpose=PURPOSE_CHECK,
            system=system,
            user=build_answer_check_prompt(
                term=term,
                translation=translation,
                answer=answer,
                direction=direction,
            ),
            schema=AnswerCheckResponse,
            user_id=user_id,
        )
        return response.verdict
