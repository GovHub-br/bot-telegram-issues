"""Conversa do /nova de ponta a ponta: PTB de verdade, com Telegram, GitHub e Hayhooks falsos."""

import itertools
import json
from typing import Any

import httpx
import pytest
from telegram import Update
from telegram.request import BaseRequest, RequestData

from issues_bot.app import build_application
from issues_bot.config import Settings
from issues_bot.drafter import DrafterClient
from issues_bot.github import GitHubClient

BOT = {"id": 999, "is_bot": True, "first_name": "Bot", "username": "bot"}
USER = {"id": 10, "is_bot": False, "first_name": "Ana", "username": "ana"}
CHAT = {"id": 10, "type": "private"}


class FakeTelegram(BaseRequest):
    """Responde à Bot API e guarda o que o bot mandou."""

    def __init__(self):
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self._ids = itertools.count(1000)

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    @property
    def read_timeout(self):
        return 1

    async def do_request(self, url, method, request_data: RequestData | None = None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.sent.append((endpoint, params))
        if endpoint == "getMe":
            result: Any = BOT | {"can_join_groups": True, "can_read_all_group_messages": False,
                                 "supports_inline_queries": False}
        elif endpoint in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
            result = {"message_id": next(self._ids), "date": 0, "chat": CHAT, "from": BOT,
                      "text": params.get("text", "")}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def texts(self) -> list[str]:
        return [p.get("text", "") for e, p in self.sent if e in ("sendMessage", "editMessageText")]


class FakeGitHub:
    def __init__(self, project_forbidden: bool = False):
        self.created: list[dict[str, Any]] = []
        self.graphql: list[dict[str, Any]] = []
        self.project_forbidden = project_forbidden

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/graphql":
            return self._graphql(json.loads(req.content))
        if path == "/orgs/o/repos":
            return httpx.Response(200, json=[{"name": n, "has_issues": True} for n in ("rag", "web")])
        if path == "/repos/o/rag/labels":
            return httpx.Response(200, json=[{"name": "bug"}, {"name": "enhancement"}])
        if path == "/repos/o/web/labels":
            return httpx.Response(200, json=[{"name": "frontend"}, {"name": "bug"}])
        if req.method == "POST" and path.endswith("/issues"):
            repo = path.split("/")[3]
            payload = json.loads(req.content) | {"repo": repo}
            self.created.append(payload)
            return httpx.Response(201, json={
                "number": 77, "node_id": "I_77", "title": payload["title"], "state": "open",
                "html_url": f"https://gh/{repo}/77", "repository_url": f"https://api.github.com/repos/o/{repo}",
                "user": {"login": "x"}, "labels": [{"name": lb} for lb in payload.get("labels", [])],
            })
        raise AssertionError(f"chamada inesperada ao GitHub: {req.method} {req.url}")

    def _graphql(self, body: dict[str, Any]) -> httpx.Response:
        self.graphql.append(body)
        q = body["query"]
        if "projectV2(number" in q:
            return httpx.Response(200, json={"data": {"organization": {"projectV2": {
                "id": "P1", "title": "Gov Hub - Project Management", "url": "https://gh/projects/4",
                "field": {"id": "F1", "options": [{"id": "O1", "name": "Project Backlog"}]}}}}})
        if "addProjectV2ItemById" in q:
            if self.project_forbidden:
                return httpx.Response(200, json={"data": {"addProjectV2ItemById": None}, "errors": [
                    {"type": "FORBIDDEN", "message": "Resource not accessible by personal access token"}]})
            return httpx.Response(200, json={"data": {"addProjectV2ItemById": {"item": {"id": "IT1"}}}})
        if "updateProjectV2ItemFieldValue" in q:
            return httpx.Response(200, json={"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "IT1"}}}})
        raise AssertionError(f"GraphQL inesperado: {q}")


DRAFT = {
    "title": "Retriever erra coluna de faturamento",
    "body": "## Contexto\nalgo",
    "labels": ["bug"],
    "duplicada_de": {"repo": "web", "number": 12},
    "similares": [
        {"repo": "web", "number": 12, "title": "Coluna errada", "state": "open", "url": "https://gh/web/12"},
        {"repo": "rag", "number": 3, "title": "Card antigo", "state": "closed", "url": "https://gh/rag/3"},
    ],
    "modelo": "qwen-teste",
}


def hayhooks_ok(req: httpx.Request) -> httpx.Response:
    assert req.url.path == "/issue_draft/run"
    assert json.loads(req.content) == {"texto": "o retriever erra a coluna", "repo": "rag"}
    return httpx.Response(200, json={"result": DRAFT})


def hayhooks_down(req: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("recusado")


class Harness:
    def __init__(self, hayhooks=None, *, project_forbidden=False):
        self.tg = FakeTelegram()
        self.github = FakeGitHub(project_forbidden)
        settings = Settings("1:x", "g", "o", allowed_users=frozenset(), allow_everyone=True,
                            default_labels=("arquitetura",), project=("GovHub-br", 4),
                            project_status="Project Backlog")
        self.app = build_application(settings)
        self.app.bot._request = (self.tg, self.tg)  # type: ignore[attr-defined]
        self.app.bot_data["gh"] = GitHubClient("g", "o", transport=httpx.MockTransport(self.github))
        if hayhooks:
            self.app.bot_data["drafter"] = DrafterClient("http://hh", transport=httpx.MockTransport(hayhooks))
        else:
            self.app.bot_data.pop("drafter", None)
        self._ids = itertools.count(1)
        self.errors: list[BaseException] = []

        async def capture(update, context):
            self.errors.append(context.error)

        self.app.add_error_handler(capture)

    async def __aenter__(self):
        await self.app.initialize()
        return self

    async def __aexit__(self, *exc):
        await self.app.shutdown()
        assert not self.errors, self.errors

    async def send(self, text: str):
        msg = {"message_id": next(self._ids), "date": 0, "chat": CHAT, "from": USER, "text": text}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self.app.process_update(Update.de_json({"update_id": next(self._ids), "message": msg}, self.app.bot))

    async def click(self, data: str):
        query = {"id": str(next(self._ids)), "from": USER, "chat_instance": "ci", "data": data,
                 "message": {"message_id": 500, "date": 0, "chat": CHAT, "from": BOT, "text": "x"}}
        await self.app.process_update(Update.de_json({"update_id": next(self._ids), "callback_query": query},
                                                     self.app.bot))


async def test_assisted_flow_creates_issue_with_suggested_and_default_labels():
    async with Harness(hayhooks_ok) as h:
        await h.send("/nova")
        assert "Em qual repositório" in h.tg.texts()[-1]
        await h.click("n:r:rag")
        assert "Me conta o que precisa virar issue" in h.tg.texts()[-1]
        await h.send("o retriever erra a coluna")
        assert any("Rascunho pronto" in t for t in h.tg.texts())
        assert "Labels" in h.tg.texts()[-1]

        await h.click("n:ok")
        preview = h.tg.texts()[-1]
        assert "Parece duplicada de" in preview and "web#12" in preview
        assert "rag#3" in preview  # parecida, de outro repositório
        assert "<b>Repositório:</b>" in preview and ">rag</a>" in preview
        assert "Retriever erra coluna de faturamento" in preview
        assert "<code>arquitetura</code> <code>bug</code>" in preview

        await h.click("n:create")
        done = h.tg.texts()[-1]
        assert "#77" in done and "criada" in done
        assert "No Project" in done and "Project Backlog" in done

    add, status = h.github.graphql[1:]
    assert add["variables"] == {"project": "P1", "content": "I_77"}
    assert status["variables"] == {"project": "P1", "item": "IT1", "field": "F1", "option": "O1"}

    created = h.github.created[0]
    assert created["repo"] == "rag"
    assert created["labels"] == ["arquitetura", "bug"]
    assert created["body"].startswith("## Contexto")
    assert "Enviado via Telegram por Ana (@ana) · rascunho gerado com qwen-teste" in created["body"]


async def test_falls_back_to_manual_when_hayhooks_is_down():
    async with Harness(hayhooks_down) as h:
        await h.send("/nova o retriever erra a coluna")
        await h.click("n:r:rag")
        assert any("Assistente indisponível" in t for t in h.tg.texts())
        assert "título" in h.tg.texts()[-1]
        await h.send("Retriever erra coluna")
        assert "Labels" in h.tg.texts()[-1]  # a descrição já veio do relato
        await h.click("n:ok")
        await h.click("n:create")

    created = h.github.created[0]
    assert created["title"] == "Retriever erra coluna"
    assert created["body"].startswith("o retriever erra a coluna")
    assert created["labels"] == ["arquitetura"]


async def test_label_is_required_and_edit_title_before_creating():
    async with Harness() as h:
        await h.send("/nova Título inicial | corpo")
        await h.click("n:r:rag")
        await h.click("n:l:0")  # desmarca "arquitetura"
        await h.click("n:ok")
        alerts = [p for e, p in h.tg.sent if e == "answerCallbackQuery" and p.get("show_alert")]
        assert alerts and "pelo menos uma label" in alerts[-1]["text"]

        await h.click("n:l:1")  # marca "bug"
        await h.click("n:ok")
        await h.click("n:e:title")
        assert "Título atual" in h.tg.texts()[-1]
        await h.send("Título corrigido")
        assert "Revise a nova issue" in h.tg.texts()[-1]
        await h.click("n:create")

    assert h.github.created[0]["title"] == "Título corrigido"
    assert h.github.created[0]["labels"] == ["bug"]


async def test_manual_flow_without_assistant():
    async with Harness() as h:
        await h.send("/nova")
        await h.click("n:r:web")
        assert "título" in h.tg.texts()[-1]
        await h.send("Meu título")
        await h.send("/pular")
        await h.click("n:ok")
        await h.click("n:cancel")
        assert "cancelada" in h.tg.texts()[-1]
    assert h.github.created == []


@pytest.mark.parametrize("raw, expected", [("", None), ("hayhooks:1416", "http://hayhooks:1416"),
                                           ("https://h/", "https://h")])
def test_hayhooks_url(raw, expected, monkeypatch):
    from issues_bot.config import _url
    assert _url(raw) == expected


async def test_issue_is_created_even_if_project_is_forbidden():
    async with Harness(project_forbidden=True) as h:
        await h.send("/nova Título | corpo")
        await h.click("n:r:rag")
        await h.click("n:ok")
        await h.click("n:create")
        done = h.tg.texts()[-1]
        assert "rag#77" in done and "criada" in done
        assert "Não consegui colocar no Project" in done and "permissão de Projects" in done
    assert len(h.github.created) == 1


async def test_only_the_team_group_can_use_the_bot():
    from dataclasses import replace

    async with Harness() as h:
        h.app.bot_data["settings"] = replace(h.app.bot_data["settings"], allow_everyone=False,
                                             allowed_chats=frozenset({-100}))
        await h.send("/issues")  # no privado
        assert h.tg.texts()[-1] == "🔒 Este bot só funciona dentro do grupo do time."

        global CHAT
        private, CHAT = CHAT, {"id": -100, "type": "supergroup", "title": "Time"}
        try:
            await h.send("/nova Título | corpo")  # no grupo liberado
            assert "Em qual repositório" in h.tg.texts()[-1]
        finally:
            CHAT = private


async def test_change_repo_on_review_reloads_labels_and_keeps_the_common_ones():
    async with Harness() as h:
        await h.send("/nova Título | corpo")
        await h.click("n:r:rag")
        await h.click("n:l:1")  # marca "bug" (arquitetura já vem marcada)
        await h.click("n:ok")
        await h.click("n:e:repo")
        assert "Em qual repositório" in h.tg.texts()[-1]
        await h.click("n:r:web")
        assert "Labels" in h.tg.texts()[-1]
        await h.click("n:ok")
        await h.click("n:create")

    created = h.github.created[0]
    assert created["repo"] == "web"
    assert created["labels"] == ["arquitetura", "bug"]  # "bug" também existe em web


async def test_last_used_repo_comes_first():
    async with Harness() as h:
        await h.send("/nova Título | corpo")
        await h.click("n:r:web")
        await h.send("/cancelar")
        await h.send("/nova")
        markup = [p for e, p in h.tg.sent if e == "sendMessage"][-1]["reply_markup"]
        assert markup["inline_keyboard"][0][0]["text"] == "⭐ web"
