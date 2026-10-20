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

import logging
import re
from importlib.resources import files
from typing import Any

import httpx
from haystack.components.agents import Agent
from haystack.components.generators.chat.types.protocol import ChatGenerator
from haystack.dataclasses import ChatMessage
from haystack.tools import Tool
from haystack_integrations.components.generators.ollama import OllamaChatGenerator
from jinja2 import Template

from .config import DrafterSettings
from .github import GitHubReader

log = logging.getLogger(__name__)

MAX_TITLE = 120
MAX_LABELS = 3
# Labels comuns, usadas quando o rascunho não é para um repositório específico (Open WebUI).
FALLBACK_LABELS = ["bug", "enhancement", "documentation", "question"]
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
            generation_kwargs={"temperature": 0.2, "num_ctx": 8192},
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
    def _propose_tool(labels: list[str]) -> Tool:
        def propor_issue(title: str, body: str, labels: list[str], duplicada_de: str | None = None) -> dict[str, Any]:
            return {"title": title, "body": body, "labels": labels, "duplicada_de": duplicada_de}

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

    def draft(self, texto: str, repo: str | None = None) -> dict[str, Any]:
        texto = texto.strip()
        if not texto:
            raise DraftError("O relato está vazio.")

        labels = self.allowed_labels(repo)
        system_prompt = self._prompt.render(
            org=self.settings.github_org, repo=repo, labels=labels, default_labels=self.settings.default_labels
        )
        agent = Agent(
            chat_generator=self.chat_generator,
            tools=[self._search_tool(), self._propose_tool(labels)],
            system_prompt=system_prompt,
            exit_conditions=["propor_issue"],
            max_agent_steps=self.settings.max_agent_steps,
            state_schema={"similares": {"type": list[dict]}, "rascunho": {"type": dict}},
        )
        agent.warm_up()
        result = agent.run(messages=[ChatMessage.from_user(texto)])

        rascunho = result.get("rascunho")
        if not rascunho:
            last = result.get("last_message")
            log.warning("O modelo terminou sem chamar propor_issue: %s", last.text if last else None)
            raise DraftError("O modelo não chegou a propor a issue.")
        return self._clean(rascunho, result.get("similares") or [], labels)

    def _clean(self, rascunho: dict[str, Any], similares: list[dict[str, Any]], allowed: list[str]) -> dict[str, Any]:
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
