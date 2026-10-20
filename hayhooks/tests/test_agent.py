from typing import Any

import pytest
from haystack import component
from haystack.dataclasses import ChatMessage, ToolCall

from issue_drafter.agent import DraftError, IssueDrafter, to_markdown
from issue_drafter.config import DrafterSettings, _url
from issue_drafter.github import IssueTemplate, parse_template
from issue_drafter.people import Person, find_people

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
    def __init__(self, found=None, templates=None):
        self.found = found or []
        self.queries: list[str] = []
        self.templates_by_repo = templates or {}
        self.people: list[Person] = []
        self.templates_asked: list[str] = []

    def assignable_people(self, repo):
        return self.people

    def templates(self, repo):
        self.templates_asked.append(repo)
        return self.templates_by_repo.get(repo, [])

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


def make(replies, found=FOUND, templates=None, settings=SETTINGS):
    gen = ScriptedChatGenerator(replies)
    gh = FakeGitHub(found, templates)
    return IssueDrafter(settings, chat_generator=gen, github=gh), gen, gh


DAG = IssueTemplate("DAG de Dados", "Criar ou corrigir uma DAG", "## Contexto\n\nDescreva.\n\n## Tarefas\n\n- [ ] Testar", ["bug"])
GERAL = IssueTemplate("Demanda Geral de Dados", "Demanda ainda não classificada", "## Contexto\n\nDescreva.")


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


def test_uses_the_repo_template():
    drafter, gen, gh = make(
        [
            call("buscar_issues", termos="dag contratos"),
            call("ler_template", nome="DAG de Dados"),
            call("propor_issue", title="Corrigir DAG", body="## Contexto\n\nA DAG falha.", labels=["enhancement"],
                 template="DAG de Dados"),
        ],
        templates={"rag": [DAG, GERAL]},
    )
    draft = drafter.draft("a dag de contratos falha", "rag")

    system = gen.seen[0][0].text
    assert "**DAG de Dados**: Criar ou corrigir uma DAG" in system and "ler_template" in system
    assert gen.seen[2][-1].tool_call_result.result.startswith("## Contexto")  # o modelo chega ao LLM
    assert draft["template"] == "DAG de Dados"
    assert draft["labels"] == ["enhancement", "bug"]  # a label do template entra junto


def test_falls_back_to_the_org_templates_repo():
    from dataclasses import replace

    drafter, gen, gh = make(
        [call("propor_issue", title="T", body="B", labels=["bug"], template="Demanda Geral de Dados")],
        templates={"cidades": [GERAL]},
        settings=replace(SETTINGS, templates_repo="cidades"),
    )
    draft = drafter.draft("relato", "rag")
    assert gh.templates_asked == ["rag", "cidades"]
    assert draft["template"] == "Demanda Geral de Dados"


def test_without_templates_keeps_the_default_sections():
    drafter, gen, _ = make([call("propor_issue", title="T", body="B", labels=["bug"], template="inventado")])
    draft = drafter.draft("relato", "rag")
    assert draft["template"] is None
    assert "## Critérios de aceite" in gen.seen[0][0].text and "ler_template" not in gen.seen[0][0].text


def test_parse_template_front_matter():
    tpl = parse_template(
        "---\nname: Modelo dbt\nabout: Criar modelo dbt\ntitle: ''\nlabels: 'dbt, dados'\nassignees: ''\n\n---\n\n## Contexto\n",
        "modelo-dbt.md",
    )
    assert (tpl.name, tpl.about, tpl.labels, tpl.body) == ("Modelo dbt", "Criar modelo dbt", ["dbt", "dados"], "## Contexto")
    assert parse_template("## Só corpo", "sem-front-matter.md").name == "sem-front-matter"


def test_github_reader_retries_dropped_connections_and_reads_templates():
    import base64

    import httpx

    from issue_drafter.github import GitHubReader

    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        if req.url.path.endswith("/ISSUE_TEMPLATE"):
            return httpx.Response(200, json=[
                {"type": "file", "name": "dag.md", "url": "https://api.github.com/x/dag.md"},
                {"type": "file", "name": "config.yml", "url": "https://api.github.com/x/config.yml"},
            ])
        content = base64.b64encode("---\nname: DAG de Dados\nabout: DAG\n---\n## Contexto\n".encode()).decode()
        return httpx.Response(200, json={"content": content})

    reader = GitHubReader("t", "org", transport=httpx.MockTransport(handler))
    templates = reader.templates("rag")
    assert [t.name for t in templates] == ["DAG de Dados"]  # o .yml (formulário) fica de fora
    assert reader.templates("rag") is templates  # cache


PEOPLE = [
    Person("bottinolucas", "Lucas Bottino"),
    Person("jlucasiqueira", "Joao Lucas Araujo Siqueira"),
    Person("lcsgborges", "Lucas Guimarães Borges"),
    Person("arthrok", "Arthur Alves"),
]


@pytest.mark.parametrize(
    "query, best",
    [("lucas botino", "bottinolucas"), ("Botino", "bottinolucas"), ("lucas borges", "lcsgborges"),
     ("joão lucas", "jlucasiqueira"), ("@arthrok", "arthrok")],
)
def test_find_people_ranks_the_closest_name_first(query, best):
    assert find_people(query, PEOPLE)[0].person.login == best


def test_find_people_ignores_weak_matches():
    assert find_people("zé ninguém", PEOPLE) == []


def test_assignee_from_buscar_pessoa():
    drafter, gen, gh = make([
        call("buscar_pessoa", nome="lucas botino"),
        call("propor_issue", title="T", body="B", labels=["bug"], responsavel="bottinolucas"),
    ])
    gh.people = PEOPLE
    draft = drafter.draft("arruma a dag, deixe como responsável o lucas botino", "rag")
    assert draft["responsavel"] == {"login": "bottinolucas", "name": "Lucas Bottino"}
    assert [c["login"] for c in draft["candidatos_responsavel"]][0] == "bottinolucas"
    assert "bottinolucas — Lucas Bottino" in gen.seen[1][-1].tool_call_result.result
    assert "buscar_pessoa" in gen.seen[0][0].text


def test_assignee_not_returned_by_the_search_is_ignored():
    drafter, _, gh = make([call("propor_issue", title="T", body="B", labels=["bug"], responsavel="arthrok")])
    gh.people = PEOPLE
    draft = drafter.draft("relato sem pedir responsável", "rag")
    assert draft["responsavel"] is None and draft["candidatos_responsavel"] == []


def test_ambiguous_name_leaves_assignee_empty_but_keeps_candidates():
    drafter, _, gh = make([
        call("buscar_pessoa", nome="lucas"),
        call("propor_issue", title="T", body="B", labels=["bug"]),
    ])
    gh.people = PEOPLE
    draft = drafter.draft("responsável: lucas", "rag")
    assert draft["responsavel"] is None
    assert len(draft["candidatos_responsavel"]) == 3


def test_without_people_there_is_no_person_tool():
    drafter, gen, _ = make([call("propor_issue", title="T", body="B", labels=["bug"])])
    drafter.draft("relato", "rag")
    assert "buscar_pessoa" not in gen.seen[0][0].text
