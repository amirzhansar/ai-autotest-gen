"""Unit tests for the helper functions of autotest_gen.py (no browser, no network, no AI)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import autotest_gen as ag  # noqa: E402


# ---------- check_url ----------

@pytest.mark.parametrize("raw, problem", [
    ("", "the URL is empty"),
    ("Open profile.", "the URL must not contain spaces"),
    ("ftp://site.kz", "the URL must start with http:// or https://"),
])
def test_check_url_rejects_invalid_input(raw, problem):
    assert ag.check_url(raw) == (None, problem)


def test_check_url_rejects_word_without_domain():
    url, problem = ag.check_url("github")
    assert url is None
    assert "does not look like a site address" in problem


@pytest.mark.parametrize("raw, expected", [
    ("localhost:3000", "http://localhost:3000"),
    ("127.0.0.1:8080/login", "http://127.0.0.1:8080/login"),
    ("http://localhost", "http://localhost"),
])
def test_check_url_local_addresses_get_http(raw, expected):
    assert ag.check_url(raw) == (expected, "")


def test_check_url_unknown_host_is_a_warning_not_an_error(monkeypatch):
    import socket

    def fail(*args, **kwargs):
        raise socket.gaierror("not found")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    url, problem = ag.check_url("no-such-site.kz/path")
    assert url == "https://no-such-site.kz/path"
    assert problem.startswith("?")


# ---------- small helpers ----------

@pytest.mark.parametrize("key, ok", [
    ("AQ.Ab8RN6", True),
    ("AIzaSyD", True),
    ("sk-ant-123", False),
    ("", False),
])
def test_is_gemini_key(key, ok):
    assert ag.is_gemini_key(key) is ok


@pytest.mark.parametrize("name, slug", [
    ("Login", "login"),
    ("check my name!", "check_my_name"),
    ("   ", "scenario"),
    ("a" * 60, "a" * 40),
])
def test_slugify(name, slug):
    assert ag.slugify(name) == slug


def test_extract_code_from_fenced_block():
    answer = "Here you go:\n```python\nimport re\n\ndef test_x(page):\n    pass\n```\nDone."
    assert ag.extract_code(answer) == "import re\n\ndef test_x(page):\n    pass\n"


def test_extract_code_without_fence_returns_text():
    assert ag.extract_code("def test_x(page): pass") == "def test_x(page): pass\n"


def test_prompt_contains_guess_mark():
    assert ag.AI_GUESS_MARK in ag.SYSTEM_PROMPT


# ---------- input validation ----------

def feed(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_: next(it))


def test_yes_reasks_on_typo(monkeypatch):
    feed(monkeypatch, "yy", "n")
    assert ag.yes("? ") is False


def test_yes_empty_means_yes(monkeypatch):
    feed(monkeypatch, "")
    assert ag.yes("? ") is True


def test_ask_number_reasks_until_valid(monkeypatch):
    feed(monkeypatch, "23", "abc", "-1", "2")
    assert ag.ask_number("? ", 2) == 2


def test_ask_number_default(monkeypatch):
    feed(monkeypatch, "")
    assert ag.ask_number("? ", 5) == 0


def test_read_multiline_stops_at_end(monkeypatch):
    feed(monkeypatch, "step 1", "step 2", "end", "ignored")
    assert ag.read_multiline("title") == "step 1\nstep 2"
