from typing import Any

import pytest
from haystack import component
from haystack.dataclasses import ChatMessage, ToolCall

from issue_drafter.agent import DraftError, IssueDrafter, to_markdown
from issue_drafter.config import DrafterSettings, _url

SETTINGS = DrafterSettings(
    github_token="t", github_org="org", ollama_url="http://x", ollama_model="m", default_labels=("arquitetura",)
)


@component
class ScriptedChatGenerator:
    """Devolve as respostas na ordem dada, como se fosse o LLM."""

    def __init__(self, replies: list[ChatMessage]):
        self.replies = list(replies)
        self.seen: list[list[ChatMessage]] = []

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage], tools: Any = None, **kwargs: Any) -> dict:
        self.seen.append(messages)
        return {"replies": [self.replies.pop(0)]}


class FakeGitHub:
    def __init__(self, found=None):
        self.found = found or []
        self.queries: list[str] = []

    def labels(self, repo):
        self.labels_repo = repo
        return ["bug", "enhancement", "Documentation"]

    def search(self, terms, limit=5):
        self.queries.append(terms)
        return self.found


def call(name: str, **args) -> ChatMessage:
    return ChatMessage.from_assistant(tool_calls=[ToolCall(tool_name=name, arguments=args)])


FOUND = [
    {"repo": "rag", "number": 12, "title": "Retriever erra coluna", "state": "open", "url": "u12", "labels": ["bug"],
     "body": "x"},
    {"repo": "rag", "number": 3, "title": "Card antigo", "state": "closed", "url": "u3", "labels": [], "body": ""},
    {"repo": "web", "number": 12, "title": "Outra coisa", "state": "open", "url": "w12", "labels": [], "body": ""},
]


def make(replies, found=FOUND):
    gen = ScriptedChatGenerator(replies)
    gh = FakeGitHub(found)
    return IssueDrafter(SETTINGS, chat_generator=gen, github=gh), gen, gh


def test_search_then_propose():
    drafter, gen, gh = make(
        [
            call("buscar_issues", termos="retriever coluna"),
            call(
                "propor_issue",
                title="Retriever traz coluna errada em faturamento.",
                body="## Contexto\nRelacionada: #12",
                labels=["BUG", "inexistente", "arquitetura", "bug"],
                duplicada_de="rag#12",
            ),
        ]
    )
    draft = drafter.draft("o retriever ta trazendo coluna errada", "rag")

    assert gh.queries == ["retriever coluna"]
    assert gh.labels_repo == "rag"
    assert draft["title"] == "Retriever traz coluna errada em faturamento"
    assert draft["labels"] == ["bug", "arquitetura"]
    assert draft["duplicada_de"] == {"repo": "rag", "number": 12}
    assert [(s["repo"], s["number"]) for s in draft["similares"]] == [("rag", 12), ("rag", 3), ("web", 12)]
    # o resultado da busca chega ao LLM como texto legível, com o repositório
    tool_result = gen.seen[1][-1].tool_call_result.result
    assert "rag#12 (aberta) [bug]: Retriever erra coluna" in tool_result


def test_labels_fall_back_to_default_and_closed_duplicate_is_ignored():
    drafter, _, _ = make(
        [
            call("buscar_issues", termos="card"),
            call("propor_issue", title="T", body="B", labels=["nada"], duplicada_de="rag#3"),
        ]
    )
    draft = drafter.draft("relato", "rag")
    assert draft["labels"] == ["arquitetura"]
    assert draft["duplicada_de"] is None  # rag#3 está fechada


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("web#12", {"repo": "web", "number": 12}),
        ("GovHub-br/web#12", {"repo": "web", "number": 12}),
        ("#12", None),  # existe em dois repositórios: ambíguo
        ("#3", None),  # fechada
        ("x", None),
        (None, None),
    ],
)
def test_duplicate_reference(raw, expected):
    found = {(it["repo"], it["number"]): it for it in FOUND}
    assert IssueDrafter._duplicate(raw, found) == expected


def test_duplicate_not_found_by_search_is_ignored():
    drafter, _, _ = make([call("propor_issue", title="T", body="B", labels=["bug"], duplicada_de="rag#99")], found=[])
    assert drafter.draft("relato", "rag")["duplicada_de"] is None


def test_prompt_lists_labels_with_defaults_first():
    drafter, gen, _ = make([call("propor_issue", title="T", body="B", labels=["bug"])])
    drafter.draft("relato", "rag")
    system = gen.seen[0][0].text
    assert "arquitetura, bug, enhancement, Documentation" in system
    assert "**org**" in system and "**rag**" in system


def test_without_repo_uses_fallback_labels():
    drafter, gen, gh = make([call("propor_issue", title="T", body="B", labels=["question"])])
    assert drafter.draft("relato")["labels"] == ["question"]
    assert not hasattr(gh, "labels_repo")
    assert "arquitetura, bug, enhancement, documentation, question" in gen.seen[0][0].text


def test_model_answering_text_is_an_error():
    drafter, _, _ = make([ChatMessage.from_assistant("Claro! Aqui está a issue...")])
    with pytest.raises(DraftError, match="não chegou a propor"):
        drafter.draft("relato", "rag")


def test_empty_text_is_an_error():
    drafter, _, _ = make([])
    with pytest.raises(DraftError):
        drafter.draft("   ")


def test_to_markdown():
    md = to_markdown(
        {"title": "T", "body": "B", "labels": ["bug"], "duplicada_de": {"repo": "rag", "number": 12},
         "similares": [{"repo": "rag", "number": 12, "title": "X", "state": "open", "url": "u"}], "modelo": "m"}
    )
    assert md.startswith("### T") and "duplicada de rag#12" in md and "[rag#12](u)" in md


@pytest.mark.parametrize(
    "raw, expected",
    [("10.0.0.10:11434", "http://10.0.0.10:11434"), ("https://h/", "https://h"), ("http://h:1", "http://h:1")],
)
def test_url_gets_scheme(raw, expected):
    assert _url(raw) == expected
