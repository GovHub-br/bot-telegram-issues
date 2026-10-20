"""Endpoint Hayhooks `issue_draft`: relato livre → rascunho de issue.

- `POST /issue_draft/run`  {"texto": "...", "repo": "graphrag-tais", "imagens": ["<base64>", ...]}
      → {"result": {title, body, labels, duplicada_de: {repo, number} | null, similares, modelo}}
- `issue_draft` como modelo OpenAI-compat (Open WebUI): responde o rascunho em Markdown.

A lógica vive no pacote `issue_drafter`; este arquivo só faz a ponte com o Hayhooks.
"""

from hayhooks import BasePipelineWrapper, get_last_user_message
from hayhooks.server.logger import log

from issue_drafter.agent import DraftError, IssueDrafter, to_markdown
from issue_drafter.config import DrafterSettings


class PipelineWrapper(BasePipelineWrapper):
    def setup(self) -> None:
        settings = DrafterSettings.from_env()
        self.drafter = IssueDrafter(settings)
        log.info("issue_draft pronto: modelo {} em {}, organização {}", settings.ollama_model, settings.ollama_url,
                 settings.github_org)

    def run_api(self, texto: str = "", repo: str | None = None, imagens: list[str] | None = None) -> dict:
        """Transforma um relato livre (texto e/ou imagens em base64) em rascunho de issue.
        `repo` define de onde vêm as labels; a busca de duplicadas cobre a organização toda."""
        log.info("issue_draft [{}] {} imagem(ns): '{}...'", repo or "-", len(imagens or []), texto[:80])
        try:
            return self.drafter.draft(texto, repo, imagens)
        except DraftError as exc:
            return {"erro": str(exc)}

    def run_chat_completion(self, model: str, messages: list, body: dict) -> str:
        texto = get_last_user_message(messages) or ""
        try:
            return to_markdown(self.drafter.draft(texto))
        except DraftError as exc:
            return f"Não consegui montar o rascunho: {exc}"
