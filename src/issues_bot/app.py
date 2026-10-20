"""Montagem da aplicação: registra handlers, comandos do menu e o cliente do GitHub."""

from __future__ import annotations

import logging

from telegram import BotCommand, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    Defaults,
    MessageHandler,
    filters,
)

from . import handlers as h
from . import new_issue
from .config import Settings
from .drafter import DrafterClient
from .github import GitHubClient, GitHubError

log = logging.getLogger(__name__)

MENU = [
    BotCommand("issues", "Lista as issues abertas"),
    BotCommand("issue", "Mostra uma issue — /issue 42"),
    BotCommand("buscar", "Busca issues por texto"),
    BotCommand("nova", "Cria uma issue (descreva e eu monto o rascunho)"),
    BotCommand("comentar", "Comenta numa issue — /comentar 42 texto"),
    BotCommand("ajuda", "Mostra todos os comandos"),
    BotCommand("cancelar", "Cancela o que estiver em andamento"),
]


def build_application(settings: Settings) -> Application:
    app = (
        Application.builder()
        .token(settings.telegram_token)
        .defaults(Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)))
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        # O rascunho assistido leva ~20 s; sem isso o bot inteiro esperaria por ele.
        .concurrent_updates(8)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["gh"] = GitHubClient(settings.github_token, settings.github_org, settings.github_repos)
    if settings.hayhooks_url:
        app.bot_data["drafter"] = DrafterClient(settings.hayhooks_url)

    app.add_handlers(
        [
            new_issue.conversation(),
            CommandHandler("start", h.start),
            CommandHandler(["ajuda", "help"], h.help_command),
            CommandHandler("issues", h.issues_command),
            CommandHandler("issue", h.issue_command),
            CommandHandler("buscar", h.search_command),
            CommandHandler("comentar", h.comment_command),
            CommandHandler("cancelar", h.cancel_command),
            CallbackQueryHandler(h.on_button, pattern=r"^(l|s|v|c):"),
            CallbackQueryHandler(h.on_stale_button),
            MessageHandler(filters.TEXT & ~filters.COMMAND, h.on_text),
            ChatMemberHandler(h.on_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER),
        ]
    )
    app.add_error_handler(h.on_error)
    return app


async def _post_init(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]
    gh: GitHubClient = app.bot_data["gh"]

    try:
        repos = await gh.list_repos()
    except GitHubError as exc:
        raise SystemExit(f"Não consegui listar os repositórios de {settings.github_org}: {exc.message}") from exc
    if not repos:
        raise SystemExit(f"Nenhum repositório com issues ativadas visível em {settings.github_org} (confira o token)")

    await app.bot.set_my_commands(MENU)
    me = await app.bot.get_me()
    log.info("🤖 @%s conectado a %s: %d repositórios (%s)", me.username, settings.github_org, len(repos),
             ", ".join(r.name for r in repos[:8]) + ("…" if len(repos) > 8 else ""))
    if settings.hayhooks_url:
        log.info("Rascunho assistido ligado: %s/issue_draft", settings.hayhooks_url)
    else:
        log.info("Rascunho assistido desligado (HAYHOOKS_URL vazio): /nova no modo manual")
    if settings.project:
        log.info("Issues novas vão para o Project %s/%s (Status: %s)", *settings.project, settings.project_status or "—")
    if settings.allow_everyone:
        log.warning("ALLOWED_USERS=*: qualquer pessoa que encontrar o bot no Telegram pode usá-lo.")
    elif settings.allowed_users or settings.allowed_usernames or settings.allowed_chats:
        log.info(
            "Liberados — usuários: %s · chats: %s",
            ", ".join([*map(str, sorted(settings.allowed_users)), *(f"@{u}" for u in sorted(settings.allowed_usernames))])
            or "—",
            ", ".join(map(str, sorted(settings.allowed_chats))) or "—",
        )
    else:
        log.warning("Ninguém liberado: preencha ALLOWED_USERS ou ALLOWED_CHATS. Mande /start ao bot para ver os IDs.")


async def _post_shutdown(app: Application) -> None:
    await app.bot_data["gh"].aclose()
    if drafter := app.bot_data.get("drafter"):
        await drafter.aclose()
