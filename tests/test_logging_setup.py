"""One call configures the whole process's logging.

The bot's operators read container logs, so the JSON shape and the level are part
of the contract: a stray non-JSON line breaks ingestion, and a debug message that
leaks through at INFO is noise the level was supposed to remove.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from eng_remember.logging_setup import configure_logging


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """configure_logging rewrites global state; put it back for other modules."""
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    yield
    root.handlers.clear()
    root.handlers.extend(handlers)
    root.setLevel(level)
    structlog.reset_defaults()


def _json_lines(stderr: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in stderr.splitlines() if line.strip()]


def test_configure_logging_json_and_level(capsys: pytest.CaptureFixture[str]) -> None:
    """structlog and stdlib logs both come out as one JSON object per line."""
    configure_logging(level="INFO", json_output=True, timestamps=False)

    structlog.get_logger("demo").info("hello", answer=42)
    logging.getLogger("third.party").warning("from stdlib")

    entries = _json_lines(capsys.readouterr().err)
    hello = next(entry for entry in entries if entry.get("event") == "hello")
    assert hello["answer"] == 42
    assert hello["level"] == "info"
    assert hello["logger"] == "demo"

    foreign = next(entry for entry in entries if entry.get("event") == "from stdlib")
    assert foreign["level"] == "warning"
    assert foreign["logger"] == "third.party"


def test_configure_logging_applies_the_level(capsys: pytest.CaptureFixture[str]) -> None:
    """Below-level events never reach the stream, ours and stdlib's alike."""
    configure_logging(level="WARNING", json_output=True, timestamps=False)

    structlog.get_logger("demo").info("hidden")
    logging.getLogger("third.party").info("also hidden")
    structlog.get_logger("demo").warning("shown")

    stderr = capsys.readouterr().err
    assert "hidden" not in stderr
    assert "shown" in stderr
    assert logging.getLogger().level == logging.WARNING


def test_console_mode_is_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    """Local work gets readable output; containers get JSON, never both."""
    configure_logging(level="INFO", json_output=False, timestamps=False)

    structlog.get_logger("demo").info("readable", answer=42)

    stderr = capsys.readouterr().err
    assert "readable" in stderr
    with pytest.raises(json.JSONDecodeError):
        json.loads(stderr.strip())
