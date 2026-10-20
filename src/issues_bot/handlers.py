"""Comandos, botões e conversas do bot."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from functools import wraps
from html import escape
from typing import Any

from telegram import ForceReply, InlineKeyboardMarkup, Message, Update, User
from telegram.constants import ChatAction, ChatType
from telegram.error import BadRequest, Conflict, NetworkError
from telegram.ext import ContextTypes, ConversationHandler

from . import keyboards
from .config import Settings
from .formatting import (
    STATE_FILTERS,
    STATE_TITLES,
    issue_detail,
    issue_list,
    parse_ref,
    telegram_signature,
)
from .github import GitHubClient, GitHubError

log = logging.getLogger(__name__)

Ctx = ContextTypes.DEFAULT_TYPE
Handler = Callable[[Update, Ctx], Awaitable[Any]]

PAGE_SIZE = 8


# ---- utilitários ---------------------------------------------------------------


def gh(context: Ctx) -> GitHubClient:
    return context.bot_data["gh"]


def settings(context: Ctx) -> Settings:
    return context.bot_data["settings"]


def is_allowed(update: Update, context: Ctx) -> bool:
    user, chat = update.effective_user, update.effective_chat
    return settings(context).is_allowed(
        user.id if user else None, chat.id if chat else None, user.username if user else None
    )


def denied_text(update: Update, context: Ctx) -> str:
    user, chat = update.effective_user, update.effective_chat
    private = chat is None or chat.type == ChatType.PRIVATE
    if private and settings(context).allowed_chats:
        return "🔒 Este bot só funciona dentro do grupo do time."
    text = f"🔒 <b>Acesso restrito</b>\n\nSeu ID do Telegram é <code>{user.id if user else '?'}</code>"
    if not private:
        text += f" e o ID deste grupo é <code>{chat.id}</code>"
    return text + ".\nPeça para quem administra o bot liberar em <code>ALLOWED_USERS</code> ou <code>ALLOWED_CHATS</code>."


def restricted(func: Handler) -> Handler:
    """Só deixa passar quem está liberado (ALLOWED_USERS/ALLOWED_CHATS); os demais recebem o próprio ID."""

    @wraps(func)
    async def wrapper(update: Update, context: Ctx) -> Any:
        if is_allowed(update, context):
            return await func(update, context)
        user, chat = update.effective_user, update.effective_chat
        log.warning(
            "Acesso negado para %s (id=%s) no chat %s", user.full_name if user else "?", user.id if user else "?",
            chat.id if chat else "?",
        )
        if update.callback_query:
            await update.callback_query.answer("🔒 Você não tem acesso a este bot.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(denied_text(update, context))
        return ConversationHandler.END

    return wrapper


def command_payload(message: Message) -> str:
    """Texto depois do comando, preservando quebras de linha (context.args as perde)."""
    parts = re.split(r"\s", message.text or "", maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def signature(user: User) -> str:
    return telegram_signature(user.full_name, user.username)


async def typing(update: Update, context: Ctx) -> None:
    if update.effective_chat:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)


async def edit(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


def org_link(context: Ctx) -> str:
    org = gh(context).org
    return f'<a href="https://github.com/{org}">{escape(org)}</a>'


# ---- ajuda ---------------------------------------------------------------------

HELP = """\
<b>🔎 Consultar</b>
/issues — issues abertas  ·  <code>/issues fechadas</code>  ·  <code>/issues todas</code>
/issue <code>repo#42</code> — detalhes (ou mande só <code>repo#42</code>, <code>#42</code> ou o link)
/buscar <code>termo</code> — busca no título, descrição e comentários
<i>Dá para filtrar a busca: </i><code>/buscar repo:GovHub-br/gov-hub login</code>

<b>✍️ Criar e atualizar</b>
/nova — descreva do seu jeito e eu monto o rascunho
<code>/nova Título | descrição</code> — preenchido por você, só falta a label
/comentar <code>repo#42 texto</code> — comenta na issue

<i>Fechar, editar ou apagar issues e comentários é pelo GitHub.</i>

/cancelar — interrompe o que estiver em andamento"""


async def start(update: Update, context: Ctx) -> None:
    user = update.effective_user
    if not is_allowed(update, context):
        log.info("/start não autorizado: %s (id=%s) no chat %s", user.full_name, user.id, update.effective_chat.id)
        await update.effective_message.reply_text(denied_text(update, context))
        return
    await update.effective_message.reply_text(
        f"👋 Olá, <b>{escape(user.first_name)}</b>!\n\n"
        f"Eu cuido das issues dos repositórios de 🏢 <b>{org_link(context)}</b>.\n\n{HELP}"
    )


@restricted
async def help_command(update: Update, context: Ctx) -> None:
    await update.effective_message.reply_text(f"🏢 <b>{org_link(context)}</b>\n\n{HELP}")


# ---- listagem e busca ----------------------------------------------------------


@restricted
async def issues_command(update: Update, context: Ctx) -> None:
    arg = (context.args[0].lower() if context.args else "abertas").strip()
    state = STATE_FILTERS.get(arg) or (arg if arg in STATE_TITLES else None)
    if not state:
        await update.effective_message.reply_text("Use <code>/issues</code>, <code>/issues fechadas</code> ou <code>/issues todas</code>.")
        return
    await send_list(update, context, state, 1)


async def send_list(update: Update, context: Ctx, state: str, page: int, *, edit_in: Message | None = None) -> None:
    await typing(update, context)
    result = await gh(context).list_issues(state, page, PAGE_SIZE)  # type: ignore[arg-type]
    count = "1 issue" if result.total == 1 else f"{result.total} issues"
    text = issue_list(f"Issues {STATE_TITLES[state]} · {gh(context).org}", result.issues, page, subtitle=count)
    markup = keyboards.issue_list(result.issues, f"l:{state}", page, result.has_next, state)
    if edit_in:
        await edit(edit_in, text, markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


@restricted
async def search_command(update: Update, context: Ctx) -> None:
    terms = command_payload(update.effective_message)
    if not terms:
        await update.effective_message.reply_text("Use <code>/buscar termo</code>, por exemplo <code>/buscar login quebrado</code>.")
        return
    await send_search(update, context, terms, 1)


async def send_search(update: Update, context: Ctx, terms: str, page: int, *, edit_in: Message | None = None) -> None:
    await typing(update, context)
    result = await gh(context).search(terms, page, PAGE_SIZE)
    found = "1 resultado" if result.total == 1 else f"{result.total} resultados"
    text = issue_list(f"Busca: {terms}", result.issues, page, subtitle=found)
    markup = keyboards.issue_list(result.issues, "s", page, result.has_next)
    if edit_in:
        await edit(edit_in, text, markup)
        return
    sent = await update.effective_message.reply_text(text, reply_markup=markup)
    # Guardamos o termo por mensagem para a paginação (callback_data tem só 64 bytes).
    context.chat_data.setdefault("searches", {})[sent.message_id] = terms


# ---- detalhes de uma issue -----------------------------------------------------


async def render_issue(context: Ctx, repo: str, number: int) -> tuple[str, InlineKeyboardMarkup]:
    client = gh(context)
    issue, comments = await asyncio.gather(client.get_issue(repo, number), client.recent_comments(repo, number))
    return issue_detail(issue, comments), keyboards.issue_actions(issue)


async def send_issue(update: Update, context: Ctx, repo: str, number: int) -> None:
    await typing(update, context)
    text, markup = await render_issue(context, repo, number)
    await update.effective_message.reply_text(text, reply_markup=markup)


async def resolve_ref(update: Update, context: Ctx, repo: str | None, number: int) -> list[str]:
    """Repositórios onde a issue `number` existe. Com o repo informado, só ele."""
    if repo:
        return [await gh(context).resolve_repo(repo)]
    await typing(update, context)
    return [i.repo for i in await gh(context).find_by_number(number)]


async def open_ref(update: Update, context: Ctx, repo: str | None, number: int) -> None:
    """Abre 'repo#42'; para um '#42' sem repositório, procura em todos."""
    if repo:
        await send_issue(update, context, repo, number)
        return
    await typing(update, context)
    found = await gh(context).find_by_number(number)
    if not found:
        await update.effective_message.reply_text(f"Não achei nenhuma issue #{number} nos repositórios de {gh(context).org}.")
    elif len(found) == 1:
        await send_issue(update, context, found[0].repo, number)
    else:
        await update.effective_message.reply_text(
            f"A <b>#{number}</b> existe em {len(found)} repositórios. Qual delas?",
            reply_markup=keyboards.issue_choices(found),
        )


def ref_usage(example: str) -> str:
    return f"Use <code>{example}</code> (também vale <code>#42</code> ou o link da issue)."


@restricted
async def issue_command(update: Update, context: Ctx) -> None:
    ref = parse_ref(command_payload(update.effective_message))
    if ref is None:
        await update.effective_message.reply_text(ref_usage("/issue graphrag-tais#42"))
        return
    await open_ref(update, context, *ref)


# ---- comentar ------------------------------------------------------------------


@restricted
async def comment_command(update: Update, context: Ctx) -> None:
    first, _, text = command_payload(update.effective_message).partition(" ")
    ref = parse_ref(first) if first else None
    if ref is None:
        await update.effective_message.reply_text(ref_usage("/comentar graphrag-tais#42 texto do comentário"))
        return
    repos = await resolve_ref(update, context, *ref)
    number = ref[1]
    if not repos:
        await update.effective_message.reply_text(f"Não achei nenhuma issue #{number}.")
        return
    if len(repos) > 1:
        options = ", ".join(f"<code>{escape(r)}#{number}</code>" for r in repos)
        await update.effective_message.reply_text(f"A #{number} existe em mais de um repositório: {options}. Diga qual.")
        return
    if not text.strip():
        await ask_comment(update.effective_message, context, repos[0], number, update.effective_user)
        return
    await post_comment(update, context, repos[0], number, text.strip())


async def ask_comment(message: Message, context: Ctx, repo: str, number: int, user: User) -> None:
    # A menção faz o ForceReply (selective) achar a pessoa em grupos mesmo quando a
    # pergunta responde a uma mensagem do próprio bot (caso dos botões).
    prompt = await message.reply_text(
        f"{user.mention_html()} 💬 Escreva o comentário para <b>{escape(repo)}#{number}</b>:\n"
        "<i>/cancelar para desistir</i>",
        reply_markup=ForceReply(selective=True, input_field_placeholder=f"Comentário para {repo}#{number}"),
    )
    context.user_data["pending_comment"] = {
        "chat": message.chat_id, "prompt": prompt.message_id, "repo": repo, "number": number,
    }


async def post_comment(update: Update, context: Ctx, repo: str, number: int, text: str) -> None:
    await typing(update, context)
    url = await gh(context).add_comment(repo, number, text + signature(update.effective_user))
    await update.effective_message.reply_text(
        f'💬 Comentário publicado em <a href="{url}"><b>{escape(repo)}#{number}</b></a>.',
        reply_markup=keyboards.view_issue(repo, number),
    )


@restricted
async def cancel_command(update: Update, context: Ctx) -> int:
    pending = [context.user_data.pop(key, None) for key in ("pending_comment", "draft")]
    had_something = any(pending)
    await update.effective_message.reply_text("👌 Cancelado." if had_something else "Nada em andamento para cancelar.")
    return ConversationHandler.END


# ---- mensagens de texto livres -------------------------------------------------


async def on_text(update: Update, context: Ctx) -> None:
    """Respostas a pedidos de comentário e o atalho '#42'. Usuários não autorizados são ignorados
    em silêncio, para o bot não responder a toda conversa de um grupo."""
    if not is_allowed(update, context):
        return
    msg = update.effective_message
    pending = context.user_data.get("pending_comment")
    if pending and pending["chat"] == msg.chat_id:
        replied = msg.reply_to_message and msg.reply_to_message.message_id == pending["prompt"]
        if replied or msg.chat.type == ChatType.PRIVATE:
            context.user_data.pop("pending_comment")
            await post_comment(update, context, pending["repo"], pending["number"], msg.text)
            return

    # Atalho: uma mensagem que é só 'repo#42', '#42' ou o link de uma issue.
    looks_like_ref = "#" in msg.text or msg.text.strip().startswith("https://github.com/")
    if looks_like_ref and (ref := parse_ref(msg.text)) is not None:
        await open_ref(update, context, *ref)
    elif msg.chat.type == ChatType.PRIVATE:
        await msg.reply_text("🤔 Não entendi. Veja os comandos em /ajuda.")


# ---- botões --------------------------------------------------------------------


@restricted
async def on_button(update: Update, context: Ctx) -> None:
    query = update.callback_query
    kind, *args = query.data.split(":")
    message = query.message

    if kind == "l":
        await query.answer()
        await send_list(update, context, args[0], int(args[1]), edit_in=message)

    elif kind == "s":
        terms = context.chat_data.get("searches", {}).get(message.message_id)
        if not terms:
            await query.answer("Essa busca expirou. Faça /buscar de novo.", show_alert=True)
            return
        await query.answer()
        await send_search(update, context, terms, int(args[0]), edit_in=message)

    elif kind == "v":
        repo, number = args[0], int(args[1])
        await query.answer()
        if args[2:] == ["e"]:
            text, markup = await render_issue(context, repo, number)
            await edit(message, text, markup)
        else:
            await send_issue(update, context, repo, number)

    elif kind == "c":
        await query.answer()
        await ask_comment(message, context, args[0], int(args[1]), update.effective_user)


async def on_stale_button(update: Update, context: Ctx) -> None:
    await update.callback_query.answer("Este botão expirou ou não é para você.", show_alert=True)


# ---- entrada em grupos ---------------------------------------------------------


async def on_added_to_chat(update: Update, context: Ctx) -> None:
    """Se alguém puser o bot num grupo que não está em ALLOWED_CHATS, ele avisa e sai.
    Com ALLOWED_CHATS vazio não sai, para dar tempo de descobrir o ID do grupo novo."""
    change = update.my_chat_member
    chat = change.chat
    added = change.new_chat_member.status in ("member", "administrator")
    allowed = settings(context).allowed_chats
    log.info("Bot %s no chat %s (%s)", "adicionado" if added else "removido", chat.id, chat.title)
    if not added or chat.type == ChatType.PRIVATE or not allowed or chat.id in allowed:
        return
    log.warning("Saindo do grupo não autorizado %s (%s)", chat.id, chat.title)
    await context.bot.send_message(chat.id, "🔒 Este bot é restrito a outro grupo. Saindo 👋")
    await chat.leave()


# ---- erros ---------------------------------------------------------------------


async def on_error(update: object, context: Ctx) -> None:
    err = context.error
    if isinstance(err, (Conflict, NetworkError)) and not isinstance(update, Update):
        # Falhas do polling: o PTB tenta de novo sozinho. Conflict = outra instância
        # do bot rodando com o mesmo token.
        log.warning("Telegram: %s", err)
        return
    if isinstance(err, GitHubError):
        log.warning("Erro do GitHub (%s): %s", err.status, err.message)
        text = err.message
    else:
        log.error("Erro inesperado", exc_info=err)
        text = "Algo deu errado. O erro foi registrado no log do bot."

    if not isinstance(update, Update):
        return
    try:
        if update.callback_query:
            await update.callback_query.answer(f"⚠️ {text}"[:200], show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(f"⚠️ {escape(text)}")
    except Exception:  # noqa: BLE001 - não deixar o handler de erro falhar
        log.exception("Falha ao avisar o usuário sobre o erro")
