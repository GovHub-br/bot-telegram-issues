from datetime import datetime, timedelta, timezone

import pytest

from issues_bot.config import ConfigError, Settings, parse_ids, parse_project, parse_users
from issues_bot.formatting import ago, issue_detail, parse_number, parse_ref, split_title_body, truncate
from issues_bot.github import Comment, Issue

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def make_issue(**kw) -> Issue:
    base = dict(
        repo="rag",
        number=7,
        title="Erro <script> no login & cadastro",
        state="open",
        state_reason=None,
        author="ana",
        url="https://github.com/o/r/issues/7",
        body="Passos:\n1. abrir\n<!-- template -->",
        labels=["bug"],
        created_at=NOW - timedelta(days=2),
        updated_at=NOW - timedelta(hours=1),
    )
    return Issue(**(base | kw))


@pytest.mark.parametrize(
    "raw, expected",
    [("", set()), ("123", {123}), ("1, 2 ,3", {1, 2, 3}), ("4 5\n6", {4, 5, 6})],
)
def test_parse_ids(raw, expected):
    assert parse_ids(raw) == expected


def test_parse_ids_invalid():
    with pytest.raises(ConfigError, match="ALLOWED_CHATS"):
        parse_ids("123, abc", "ALLOWED_CHATS")


def test_settings_from_env_access_rules(monkeypatch):
    env = {"TELEGRAM_TOKEN": "t", "GITHUB_TOKEN": "g", "GITHUB_ORG": "GovHub-br", "ALLOWED_USERS": "*",
           "ALLOWED_CHATS": "", "DEFAULT_LABELS": "arquitetura, bug ,", "GITHUB_REPOS": "gov-hub, GovHub-br/rag"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    s = Settings.from_env()
    assert s.allow_everyone and s.is_allowed(999, None)
    assert s.default_labels == ("arquitetura", "bug")
    assert s.github_org == "GovHub-br" and s.github_repos == ("gov-hub", "rag")


def test_parse_users_accepts_ids_and_usernames():
    assert parse_users("123, @Arthrok beltrano_1") == ({123}, {"arthrok", "beltrano_1"})
    with pytest.raises(ConfigError):
        parse_users("@a!")


def test_is_allowed_by_username_ignores_case():
    s = Settings("t", "g", "o", allowed_users=frozenset(), allowed_usernames=frozenset({"arthrok"}))
    assert s.is_allowed(99, 99, "ArthRok")
    assert not s.is_allowed(99, 99, "outra_pessoa")
    assert not s.is_allowed(99, 99, None)


def test_is_allowed_by_user_or_chat():
    s = Settings("t", "g", "o/r", allowed_users=frozenset({1}), allowed_chats=frozenset({-100}))
    assert s.is_allowed(1, 5)
    assert s.is_allowed(2, -100)
    assert not s.is_allowed(2, 5)
    assert not s.is_allowed(None, None)


@pytest.mark.parametrize("text, expected", [("42", 42), ("#42", 42), (" #7 ", 7), ("x", None), ("4 2", None)])
def test_parse_number(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("graphrag-tais#42", ("graphrag-tais", 42)),
        ("GovHub-br/graphrag-tais#42", ("graphrag-tais", 42)),
        ("graphrag-tais 42", ("graphrag-tais", 42)),
        ("#42", (None, 42)),
        ("42", (None, 42)),
        ("https://github.com/GovHub-br/gov-hub/issues/5#issuecomment-1", ("gov-hub", 5)),
        ("oi tudo bem", None),
        ("repo#x", None),
    ],
)
def test_parse_ref(text, expected):
    assert parse_ref(text) == expected


def test_split_title_body():
    assert split_title_body("Título | corpo | com pipe") == ("Título", "corpo | com pipe")
    assert split_title_body("Só título") == ("Só título", "")


def test_truncate():
    assert truncate("abc", 5) == "abc"
    assert truncate("abcdef", 4) == "abc…"


@pytest.mark.parametrize(
    "delta, expected",
    [
        (timedelta(seconds=10), "agora há pouco"),
        (timedelta(minutes=1), "há 1 minuto"),
        (timedelta(hours=5), "há 5 horas"),
        (timedelta(days=1), "há 1 dia"),
        (timedelta(days=45), "há 1 mês"),
        (timedelta(days=800), "há 2 anos"),
    ],
)
def test_ago(delta, expected):
    assert ago(NOW - delta, now=NOW) == expected


def test_issue_detail_escapes_html_and_strips_comments():
    text = issue_detail(make_issue())
    assert "&lt;script&gt;" in text and "<script>" not in text
    assert "template" not in text
    assert "<code>bug</code>" in text


def test_issue_detail_fits_telegram_limit():
    huge = "<&>" * 3000  # cada caractere vira uma entidade HTML
    comments = [Comment("bob", huge, NOW, "u")] * 3
    text = issue_detail(make_issue(body=huge, comments=40), comments)
    assert len(text) <= 4096
    assert "mostrando os 3 mais recentes" in text


def test_closed_not_planned_icon():
    text = issue_detail(make_issue(state="closed", state_reason="not_planned", closed_at=NOW))
    assert text.startswith("⚪") and "não planejada" in text
    assert "rag#7" in text


@pytest.mark.parametrize(
    "raw, expected",
    [("", None), ("GovHub-br/4", ("GovHub-br", 4)), ("https://github.com/orgs/GovHub-br/projects/4/", ("GovHub-br", 4))],
)
def test_parse_project(raw, expected):
    assert parse_project(raw) == expected


def test_parse_project_invalid():
    with pytest.raises(ConfigError):
        parse_project("GovHub-br")
