"""Agent do Haystack que transforma um relato livre em rascunho de issue.

O Agent tem duas tools:

- `buscar_issues`: busca issues parecidas no repositório (só leitura), para apontar
  duplicadas e relacionadas;
- `propor_issue`: devolve o rascunho (título, corpo, labels). É a condição de saída
  do Agent e também o jeito de obter saída estruturada: os modelos MLX do Ollama não
  aceitam `format`/JSON schema, mas aceitam tool calling.

O Agent **nunca cria** a issue: quem cria é o bot, depois que a pessoa confirma.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from importlib.resources import files
from typing import Any

import httpx
from haystack.components.agents import Agent
from haystack.components.generators.chat.types.protocol import ChatGenerator
from haystack.dataclasses import ChatMessage, ImageContent
from haystack.tools import Tool
from haystack_integrations.components.generators.ollama import OllamaChatGenerator
from jinja2 import Template

from .config import DrafterSettings
from .github import GitHubReader, IssueTemplate
from .people import Person, find_people

log = logging.getLogger(__name__)

MAX_TITLE = 120
MAX_LABELS = 3
# Labels comuns, usadas quando o rascunho não é para um repositório específico (Open WebUI).
FALLBACK_LABELS = ["bug", "enhancement", "documentation", "question"]
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAGIC = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG": "image/png", b"RIFF": "image/webp", b"GIF8": "image/gif"}
_REF_RE = re.compile(r"(?:(?P<repo>[\w.-]+)?#)?(?P<n>\d+)")


class DraftError(RuntimeError):
    pass


def _format_found(issues: list[dict[str, Any]]) -> str:
    if not issues:
        return "Nenhuma issue encontrada com esses termos."
    lines = []
    for it in issues:
        state = "aberta" if it["state"] == "open" else "fechada"
        labels = f" [{', '.join(it['labels'])}]" if it["labels"] else ""
        snippet = " ".join(it["body"].split())[:160]
        lines.append(f"{it['repo']}#{it['number']} ({state}){labels}: {it['title']}\n   {snippet}")
    return "\n".join(lines)


class IssueDrafter:
    def __init__(
        self,
        settings: DrafterSettings,
        *,
        chat_generator: ChatGenerator | None = None,
        github: GitHubReader | None = None,
    ):
        self.settings = settings
        self.github = github or GitHubReader(settings.github_token, settings.github_org, settings.github_repos)
        self.chat_generator = chat_generator or OllamaChatGenerator(
            model=settings.ollama_model,
            url=settings.ollama_url,
            timeout=settings.ollama_timeout,
            think=False,
            # Contexto maior: o template lido entra na conversa junto com a busca.
            generation_kwargs={"temperature": 0.2, "num_ctx": 16384},
        )
        self._prompt = Template(files("issue_drafter").joinpath("prompts/system.md").read_text("utf-8"))

    # ---- tools -----------------------------------------------------------------

    def _search_tool(self) -> Tool:
        def buscar_issues(termos: str) -> dict[str, Any]:
            found = self.github.search(termos)
            log.info("buscar_issues(%r) -> %s", termos, [i["number"] for i in found])
            return {"issues": found, "texto": _format_found(found)}

        return Tool(
            name="buscar_issues",
            description="Busca issues (abertas e fechadas) em todos os repositórios da organização por palavras-chave.",
            parameters={
                "type": "object",
                "properties": {"termos": {"type": "string", "description": "2 a 4 palavras-chave"}},
                "required": ["termos"],
            },
            function=buscar_issues,
            outputs_to_string={"source": "texto"},
            outputs_to_state={"similares": {"source": "issues"}},
        )

    @staticmethod
    def _person_tool(people: list[Person]) -> Tool:
        def buscar_pessoa(nome: str) -> dict[str, Any]:
            matches = find_people(nome, people)
            log.info("buscar_pessoa(%r) -> %s", nome, [(m.person.login, m.score) for m in matches])
            found = [{"login": m.person.login, "name": m.person.name, "score": m.score} for m in matches]
            if not found:
                text = f"Ninguém parecido com {nome!r} entre quem pode ser responsável neste repositório."
            else:
                text = "\n".join(f"{m.person.login} — {m.person.name or 'sem nome no perfil'} (semelhança {m.score}%)"
                                 for m in matches)
            return {"candidatos": found, "texto": text}

        return Tool(
            name="buscar_pessoa",
            description="Procura, por semelhança de nome, quem pode ser responsável pela issue no repositório.",
            parameters={
                "type": "object",
                "properties": {"nome": {"type": "string", "description": "Nome como aparece no relato"}},
                "required": ["nome"],
            },
            function=buscar_pessoa,
            outputs_to_string={"source": "texto"},
            outputs_to_state={"candidatos_responsavel": {"source": "candidatos"}},
        )

    @staticmethod
    def _template_tool(templates: list[IssueTemplate]) -> Tool:
        by_name = {t.name: t for t in templates}

        def ler_template(nome: str) -> str:
            tpl = by_name.get(nome) or next((t for t in templates if t.name.lower() == nome.strip().lower()), None)
            if not tpl:
                return f"Template {nome!r} não existe. Opções: {', '.join(by_name)}"
            log.info("ler_template(%r)", tpl.name)
            return tpl.body

        return Tool(
            name="ler_template",
            description="Devolve o modelo (Markdown) de um tipo de issue, para ser preenchido.",
            parameters={
                "type": "object",
                "properties": {"nome": {"type": "string", "enum": list(by_name)}},
                "required": ["nome"],
            },
            function=ler_template,
        )

    @staticmethod
    def _propose_tool(labels: list[str], template_names: list[str]) -> Tool:
        def propor_issue(
            title: str,
            body: str,
            labels: list[str],
            duplicada_de: str | None = None,
            template: str | None = None,
            responsavel: str | None = None,
        ) -> dict[str, Any]:
            return {
                "title": title, "body": body, "labels": labels, "duplicada_de": duplicada_de,
                "template": template, "responsavel": responsavel,
            }

        return Tool(
            name="propor_issue",
            description="Entrega o rascunho final da issue. Sempre a última ação.",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": f"Até {MAX_TITLE} caracteres"},
                    "body": {"type": "string", "description": "Markdown com as seções pedidas"},
                    "labels": {
                        "type": "array",
                        "items": {"type": "string", "enum": labels},
                        "minItems": 1,
                        "maxItems": MAX_LABELS,
                    },
                    "duplicada_de": {
                        "type": "string",
                        "description": "repositorio#numero da issue aberta duplicada, se houver",
                    },
                    "responsavel": {
                        "type": "string",
                        "description": "login escolhido com buscar_pessoa, só se o relato pedir um responsável",
                    },
                    **(
                        {"template": {"type": "string", "enum": template_names, "description": "Modelo usado"}}
                        if template_names
                        else {}
                    ),
                },
                "required": ["title", "body", "labels"],
            },
            function=propor_issue,
            outputs_to_state={"rascunho": {}},
        )

    # ---- execução --------------------------------------------------------------

    def allowed_labels(self, repo: str | None) -> list[str]:
        defaults = list(self.settings.default_labels)
        try:
            repo_labels = self.github.labels(repo) if repo else FALLBACK_LABELS
        except httpx.HTTPError as exc:
            raise DraftError(f"não consegui ler as labels de {repo}") from exc
        return defaults + [lb for lb in repo_labels if lb not in defaults]

    def issue_templates(self, repo: str | None) -> list[IssueTemplate]:
        """Templates do próprio repositório; sem eles, os do ISSUE_TEMPLATES_REPO."""
        try:
            for source in (repo, self.settings.templates_repo):
                if source and (found := self.github.templates(source)):
                    return found
        except httpx.HTTPError as exc:
            log.warning("Não consegui ler os templates de issue: %s", exc)
        return []

    def draft(self, texto: str, repo: str | None = None, imagens: list[str] | None = None) -> dict[str, Any]:
        texto = texto.strip()
        images = self._images(imagens or [])
        if not texto and not images:
            raise DraftError("O relato está vazio.")

        labels = self.allowed_labels(repo)
        templates = self.issue_templates(repo)
        people = self.assignable_people(repo)
        system_prompt = self._prompt.render(
            org=self.settings.github_org, repo=repo, labels=labels, default_labels=self.settings.default_labels,
            templates=templates, people=bool(people), images=bool(images),
        )
        tools = [self._search_tool()]
        if people:
            tools.append(self._person_tool(people))
        if templates:
            tools.append(self._template_tool(templates))
        tools.append(self._propose_tool(labels, [t.name for t in templates]))
        agent = Agent(
            chat_generator=self.chat_generator,
            tools=tools,
            system_prompt=system_prompt,
            exit_conditions=["propor_issue"],
            max_agent_steps=self.settings.max_agent_steps,
            state_schema={
                "similares": {"type": list[dict]},
                "candidatos_responsavel": {"type": list[dict]},
                "rascunho": {"type": dict},
            },
        )
        agent.warm_up()
        relato = texto or "(Relato só com imagem: escreva a issue a partir do print.)"
        result = agent.run(messages=[ChatMessage.from_user(content_parts=[relato, *images])])

        rascunho = result.get("rascunho")
        if not rascunho:
            last = result.get("last_message")
            log.warning("O modelo terminou sem chamar propor_issue: %s", last.text if last else None)
            raise DraftError("O modelo não chegou a propor a issue.")
        draft = self._clean(rascunho, result.get("similares") or [], labels, templates)
        return draft | self._assignee(rascunho.get("responsavel"), result.get("candidatos_responsavel") or [])

    @staticmethod
    def _images(encoded: list[str]) -> list[ImageContent]:
        """Imagens em base64 (prints enviados pelo Telegram) → conteúdo para o modelo com visão."""
        if len(encoded) > MAX_IMAGES:
            raise DraftError(f"Mande no máximo {MAX_IMAGES} imagens.")
        images = []
        for item in encoded:
            try:
                raw = base64.b64decode(item, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise DraftError("Uma das imagens não veio em base64 válido.") from exc
            if len(raw) > MAX_IMAGE_BYTES:
                raise DraftError("Imagem grande demais (máximo 8 MB).")
            mime = next((m for magic, m in _MAGIC.items() if raw.startswith(magic)), None)
            if not mime:
                raise DraftError("Formato de imagem não suportado (use JPEG, PNG, WEBP ou GIF).")
            images.append(ImageContent(base64_image=item, mime_type=mime))
        return images

    def assignable_people(self, repo: str | None) -> list[Person]:
        if not repo:
            return []
        try:
            return self.github.assignable_people(repo)
        except httpx.HTTPError as exc:
            log.warning("Não consegui listar quem pode ser responsável em %s: %s", repo, exc)
            return []

    @staticmethod
    def _assignee(chosen: Any, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        """O responsável só vale se saiu de buscar_pessoa: o modelo não pode inventar alguém."""
        unique: dict[str, dict[str, Any]] = {}
        for c in sorted(candidates, key=lambda c: -c["score"]):
            unique.setdefault(c["login"].lower(), c)
        picked = unique.get(str(chosen or "").strip().lstrip("@").lower())
        return {
            "responsavel": {"login": picked["login"], "name": picked["name"]} if picked else None,
            "candidatos_responsavel": list(unique.values())[:3],
        }

    def _clean(
        self,
        rascunho: dict[str, Any],
        similares: list[dict[str, Any]],
        allowed: list[str],
        templates: list[IssueTemplate] = (),
    ) -> dict[str, Any]:
        title = " ".join(str(rascunho.get("title", "")).split()).rstrip(".")
        if not title:
            raise DraftError("O modelo propôs uma issue sem título.")
        if len(title) > MAX_TITLE:
            title = title[: MAX_TITLE - 1].rstrip() + "…"

        lookup = {lb.lower(): lb for lb in allowed}
        labels: list[str] = []
        for lb in rascunho.get("labels") or []:
            name = lookup.get(str(lb).strip().lower())
            if name and name not in labels:
                labels.append(name)
        template = next((t for t in templates if t.name == rascunho.get("template")), None)
        if template:  # labels definidas no próprio template entram junto
            labels += [lookup[lb.lower()] for lb in template.labels if lb.lower() in lookup and lookup[lb.lower()] not in labels]
        labels = labels[:MAX_LABELS] or list(self.settings.default_labels)

        unique: dict[tuple[str, int], dict[str, Any]] = {}
        for it in similares:
            unique.setdefault((it["repo"], it["number"]), {k: it[k] for k in ("repo", "number", "title", "state", "url")})

        return {
            "title": title,
            "body": str(rascunho.get("body", "")).strip(),
            "labels": labels,
            "duplicada_de": self._duplicate(rascunho.get("duplicada_de"), unique),
            "similares": list(unique.values())[:5],
            "template": template.name if template else None,
            "modelo": self.settings.ollama_model,
        }

    @staticmethod
    def _duplicate(raw: Any, found: dict[tuple[str, int], dict[str, Any]]) -> dict[str, Any] | None:
        """'repo#12', '#12' ou 12 → a issue correspondente entre as que a busca achou.
        Só vale se o Agent de fato encontrou essa issue e ela está aberta."""
        m = _REF_RE.fullmatch(str(raw or "").strip().split("/")[-1])
        if not m:
            return None
        number = int(m["n"])
        candidates = [
            it for (repo, n), it in found.items()
            if n == number and (not m["repo"] or repo.lower() == m["repo"].lower())
        ]
        if len(candidates) != 1 or candidates[0]["state"] != "open":
            return None
        return {"repo": candidates[0]["repo"], "number": number}


def to_markdown(draft: dict[str, Any]) -> str:
    """Versão legível do rascunho, usada na resposta para o Open WebUI."""
    parts = [f"### {draft['title']}", "", "**Labels:** " + ", ".join(f"`{lb}`" for lb in draft["labels"]), ""]
    if draft.get("template"):
        parts += [f"**Modelo:** {draft['template']}", ""]
    if draft["duplicada_de"]:
        dup = draft["duplicada_de"]
        parts += [f"> ⚠️ Pode ser duplicada de {dup['repo']}#{dup['number']}", ""]
    parts.append(draft["body"])
    if draft["similares"]:
        parts += ["", "---", "**Issues parecidas:**"]
        parts += [
            f"- [{s['repo']}#{s['number']}]({s['url']}) {s['title']} ({s['state']})" for s in draft["similares"]
        ]
    return "\n".join(parts)
