"""Conversa de criação de issue (/nova).

Primeiro a pessoa escolhe o repositório da organização (o último que ela usou vem
primeiro). Com o assistente (HAYHOOKS_URL) ligado, ela descreve o problema do jeito dela
e o Agent devolve título, descrição, labels sugeridas e issues parecidas. Sem assistente,
ou se ele falhar, o fluxo cai no modo manual: título → descrição.

Nos dois casos a pessoa passa pelas labels (pelo menos uma) e revisa tudo antes de criar,
podendo editar título, descrição e labels.
"""

from __future__ import annotations

import logging
import warnings
from html import escape
from typing import Any

from telegram import ForceReply, Message, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler, filters
from telegram.warnings import PTBUserWarning

from . import keyboards
from .assets import AssetStore, image_extension
from .drafter import DrafterClient, DraftUnavailable
from .formatting import split_title_body, telegram_signature, truncate
from .github import GitHubError, Issue
from .people import Person, find_people
from .handlers import Ctx, cancel_command, command_payload, edit, gh, org_link, restricted, settings, typing

log = logging.getLogger(__name__)

REPO, DESCRIBE, TITLE, BODY, LABELS, CONFIRM, WHO = range(7)
MAX_TITLE = 256
MAX_LABEL_OPTIONS = 24
PREVIEW_BODY = 2500
MAX_IMAGE_BYTES = 8 * 1024 * 1024
TELEGRAM_LIMIT = 4096


def drafter(context: Ctx) -> DrafterClient | None:
    return context.bot_data.get("drafter")


def draft_of(context: Ctx) -> dict[str, Any]:
    return context.user_data["draft"]


async def ask(update: Update, text: str, placeholder: str) -> Message:
    """Pergunta com ForceReply. Menciona a pessoa para o ForceReply (selective) achá-la em
    grupos, e para que a resposta chegue ao bot mesmo com o privacy mode ligado."""
    user = update.effective_user
    return await update.effective_message.reply_text(
        f"{user.mention_html()} {text}",
        reply_markup=ForceReply(selective=True, input_field_placeholder=placeholder),
    )


async def ask_title(update: Update, intro: str = "") -> int:
    await ask(update, f"{intro}📝 Qual o <b>título</b> da issue?\n<i>/cancelar para desistir</i>", "Título da issue")
    return TITLE


async def ask_body(update: Update, intro: str = "") -> int:
    await ask(
        update,
        f"{intro}Agora a <b>descrição</b>. Pode usar Markdown.\n<i>/pular para deixar em branco</i>",
        "Descrição da issue",
    )
    return BODY


# ---- início --------------------------------------------------------------------


@restricted
async def start(update: Update, context: Ctx) -> int:
    context.user_data.pop("pending_comment", None)
    payload = command_payload(update.effective_message)
    draft = context.user_data["draft"] = {
        "repo": "", "text": "", "title": "", "body": "", "labels": [], "options": [], "suggested": [],
        "similar": [], "duplicate_of": None, "model": "", "template": None, "editing": False, "prefilled": False,
        "assignee": None, "assignee_options": [], "images": [], "media_group": None,
    }

    if "|" in payload:
        title, body = split_title_body(payload)
        if title and len(title) <= MAX_TITLE:
            draft |= {"title": title, "body": body, "prefilled": True}
    elif payload and drafter(context):
        draft["text"] = payload
    elif payload and "\n" not in payload and len(payload) <= MAX_TITLE:
        draft["title"] = payload

    repos = await gh(context).list_repos()
    if len(repos) == 1:
        draft["repo"] = repos[0].name
        return await after_repo(update, context)
    return await ask_repo(update, context)


async def ask_repo(update: Update, context: Ctx, *, edit_in: Message | None = None, page: int = 1) -> int:
    repos = await gh(context).list_repos()
    text = f"📦 <b>Em qual repositório?</b>\n<i>{len(repos)} repositórios de {org_link(context)}, dos mais ativos para os menos.</i>"
    markup = keyboards.repo_picker(repos, page, context.user_data.get("last_repo"))
    if edit_in:
        await edit(edit_in, text, markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    return REPO


async def repo_page(update: Update, context: Ctx) -> int:
    query = update.callback_query
    await query.answer()
    return await ask_repo(update, context, edit_in=query.message, page=int(query.data.split(":")[2]))


async def repo_chosen(update: Update, context: Ctx) -> int:
    query = update.callback_query
    repo = await gh(context).resolve_repo(query.data.split(":", 2)[2])
    await query.answer()
    draft = draft_of(context)
    changed = draft["repo"] and draft["repo"] != repo
    draft["repo"] = repo
    context.user_data["last_repo"] = repo
    if draft["editing"]:
        # Trocou de repositório na revisão: as labels são de cada repositório, então
        # escolhe de novo (mantendo as que existirem no novo).
        draft["editing"] = False
        if changed:
            draft["options"] = []
            await drop_unassignable(context, draft)
        return await ask_labels(update, context, edit_in=query.message)
    await edit(query.message, f"📦 Repositório: <b>{escape(repo)}</b>")
    return await after_repo(update, context)


async def drop_unassignable(context: Ctx, draft: dict[str, Any]) -> None:
    """Ao trocar de repositório, tira o responsável (e os candidatos) que não podem ser
    atribuídos no novo."""
    people = {p.login.lower() for p in await gh(context).assignable_people(draft["repo"])}
    draft["assignee_options"] = [p for p in draft["assignee_options"] if p.login.lower() in people]
    if draft["assignee"] and draft["assignee"].login.lower() not in people:
        draft["assignee"] = None


async def after_repo(update: Update, context: Ctx) -> int:
    """Segue o fluxo depois de saber o repositório."""
    draft = draft_of(context)
    if draft["prefilled"]:  # /nova Título | descrição
        return await ask_labels(update, context)
    if draft["text"]:  # /nova texto livre, com assistente
        return await assisted(update, context, draft["text"])
    if drafter(context):
        await ask(
            update,
            "📝 <b>Nova issue</b>\n\nMe conta o que precisa virar issue, do seu jeito: o problema ou a ideia, "
            "onde acontece, o que você esperava. Pode mandar um <b>print</b> (foto com ou sem legenda). "
            "Eu monto o rascunho e procuro issues parecidas.\n\n"
            "<i>/manual para preencher você mesmo · /cancelar para desistir</i>",
            "Descreva o problema ou a ideia",
        )
        return DESCRIBE
    if draft["title"]:
        return await ask_body(update, f"<b>{escape(draft['title'])}</b>\n\n")
    return await ask_title(update)


async def describe(update: Update, context: Ctx) -> int:
    return await assisted(update, context, update.effective_message.text)


async def read_image(update: Update, context: Ctx) -> bytes | None:
    """Baixa a foto (ou imagem enviada como arquivo) da mensagem. Avisa e devolve None se não der."""
    msg = update.effective_message
    file = msg.photo[-1] if msg.photo else msg.document  # a última foto é a de maior resolução
    if file.file_size and file.file_size > MAX_IMAGE_BYTES:
        await msg.reply_text("Essa imagem passa de 8 MB. Mande uma menor ou um print.")
        return None
    data = bytes(await (await file.get_file()).download_as_bytearray())
    if not image_extension(data):
        await msg.reply_text("Não reconheci essa imagem. Mande em JPEG, PNG, WEBP ou GIF.")
        return None
    return data


async def describe_photo(update: Update, context: Ctx) -> int:
    draft = draft_of(context)
    msg = update.effective_message
    # Álbum: cada foto chega como uma mensagem; usamos só a primeira.
    if msg.media_group_id and msg.media_group_id == draft["media_group"]:
        return DESCRIBE
    draft["media_group"] = msg.media_group_id
    if (data := await read_image(update, context)) is None:
        return DESCRIBE
    draft["images"] = [data]
    if msg.media_group_id:
        await msg.reply_text("🖼️ Recebi um álbum: vou usar só a primeira imagem.")
    return await assisted(update, context, msg.caption or "")


async def manual(update: Update, context: Ctx) -> int:
    return await ask_title(update)


async def assisted(update: Update, context: Ctx, text: str) -> int:
    draft = draft_of(context)
    wait = "até 1 minuto, por causa da imagem" if draft["images"] else "leva uns 20 segundos"
    status = await update.effective_message.reply_text(
        f"🤖 Montando o rascunho e procurando issues parecidas… <i>{wait}</i>"
    )
    await typing(update, context)
    try:
        result = await drafter(context).draft(text, draft["repo"], draft["images"])
    except DraftUnavailable as exc:
        log.warning("Rascunho assistido indisponível: %s", exc)
        draft["body"] = text
        await edit(status, f"⚠️ <b>Assistente indisponível</b> — {escape(str(exc))}.\nVamos no manual: seu texto virou a descrição.")
        return await ask_title(update)

    draft |= {
        "title": result.title[:MAX_TITLE],
        "body": result.body,
        "suggested": result.labels,
        "similar": result.similar,
        "duplicate_of": result.duplicate_of,
        "model": result.model,
        "template": result.template,
        "assignee": result.assignee,
        "assignee_options": result.assignee_options,
    }
    await edit(status, f"🤖 Rascunho pronto: <b>{escape(result.title)}</b>")
    return await ask_labels(update, context)


# ---- título e descrição --------------------------------------------------------


async def got_title(update: Update, context: Ctx) -> int:
    title = update.effective_message.text.strip()
    if "\n" in title or len(title) > MAX_TITLE:
        return await ask_title(update, f"O título precisa ter uma linha só e até {MAX_TITLE} caracteres.\n")
    draft = draft_of(context)
    draft["title"] = title
    if draft["editing"]:
        return await show_preview(update, context)
    if draft["body"]:  # veio do fallback do assistente: a descrição já está preenchida
        return await ask_labels(update, context)
    return await ask_body(update)


async def got_body_photo(update: Update, context: Ctx) -> int:
    """No modo manual, a descrição pode vir como foto com legenda."""
    if (data := await read_image(update, context)) is None:
        return BODY
    draft = draft_of(context)
    draft["images"] = [data]
    draft["body"] = update.effective_message.caption or draft["body"]
    return await after_body(update, context)


async def got_body(update: Update, context: Ctx) -> int:
    draft_of(context)["body"] = update.effective_message.text
    return await after_body(update, context)


async def skip_body(update: Update, context: Ctx) -> int:
    draft = draft_of(context)
    if not draft["editing"]:
        draft["body"] = ""
    return await after_body(update, context)


async def after_body(update: Update, context: Ctx) -> int:
    if draft_of(context)["editing"]:
        return await show_preview(update, context)
    return await ask_labels(update, context)


# ---- labels --------------------------------------------------------------------


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


async def ask_labels(update: Update, context: Ctx, *, edit_in: Message | None = None) -> int:
    draft = draft_of(context)
    defaults = list(settings(context).default_labels)
    if not draft["options"]:
        await typing(update, context)
        repo_labels = [lb.name for lb in await gh(context).list_labels(draft["repo"])]
        # Padrão e sugeridas primeiro; as padrão que ainda não existem no repositório
        # são criadas pelo GitHub junto com a issue.
        draft["options"] = _unique(defaults + draft["suggested"] + repo_labels)[:MAX_LABEL_OPTIONS]
        keep = draft["labels"] or _unique(defaults + draft["suggested"])
        draft["labels"] = [lb for lb in keep if lb in draft["options"]]
    if not draft["options"]:
        return await show_preview(update, context, edit_in=edit_in)

    text = "🏷️ <b>Labels</b>\nMarque pelo menos uma e toque em <b>Continuar</b>."
    if draft["suggested"]:
        text += "\n<i>Já deixei marcadas as sugeridas pelo assistente.</i>"
    markup = keyboards.draft_labels(draft["options"], draft["labels"])
    if edit_in:
        await edit(edit_in, text, markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    return LABELS


async def toggle_label(update: Update, context: Ctx) -> int:
    query = update.callback_query
    draft = draft_of(context)
    name = draft["options"][int(query.data.split(":")[2])]
    if name in draft["labels"]:
        draft["labels"].remove(name)
    else:
        draft["labels"].append(name)
    await query.answer()
    await query.message.edit_reply_markup(keyboards.draft_labels(draft["options"], draft["labels"]))
    return LABELS


async def labels_done(update: Update, context: Ctx) -> int:
    query = update.callback_query
    if not draft_of(context)["labels"]:
        await query.answer("Escolha pelo menos uma label 🏷️", show_alert=True)
        return LABELS
    await query.answer()
    return await show_preview(update, context, edit_in=query.message)


# ---- revisão e criação ---------------------------------------------------------


def preview(draft: dict[str, Any], context: Ctx) -> str:
    # O escape de HTML pode inflar a descrição: encurta até caber numa mensagem.
    for limit in (PREVIEW_BODY, 1500, 700):
        text = _preview(draft, context, limit)
        if len(text) <= TELEGRAM_LIMIT:
            return text
    return _preview(draft, context, 200)


def _preview(draft: dict[str, Any], context: Ctx, body_limit: int) -> str:
    lines = ["📝 <b>Revise a nova issue</b>", ""]
    similar = {(s.repo, s.number): s for s in draft["similar"]}
    dup = similar.get(draft["duplicate_of"]) if draft["duplicate_of"] else None
    if dup:
        lines += [f'⚠️ <b>Parece duplicada de <a href="{dup.url}">{escape(dup.ref)}</a></b> · {escape(dup.title)}', ""]
    lines.append(f'<b>Repositório:</b> <a href="https://github.com/{gh(context).org}/{draft["repo"]}">{escape(draft["repo"])}</a>')
    if draft["template"]:
        lines.append(f"<b>Modelo:</b> 📄 {escape(draft['template'])}")
    lines.append(f"<b>Título:</b> {escape(draft['title'])}")
    if draft["labels"]:
        lines.append("<b>Labels:</b> " + " ".join(f"<code>{escape(lb)}</code>" for lb in draft["labels"]))
    if draft["assignee"]:
        lines.append(f"<b>Responsável:</b> 👤 {escape(draft['assignee'].label)}")
    if draft["images"]:
        where = "vai anexada na issue" if context.bot_data.get("assets") else "só ajudou no rascunho (anexo desligado)"
        lines.append(f"<b>Imagem:</b> 🖼️ {where}")
    body = draft["body"].strip()
    lines += ["", f"<blockquote expandable>{escape(truncate(body, body_limit))}</blockquote>" if body else "<i>Sem descrição.</i>"]
    others = [s for s in draft["similar"] if (s.repo, s.number) != draft["duplicate_of"]]
    if others:
        links = [
            f'<a href="{s.url}">{escape(s.ref)}</a> {escape(truncate(s.title, 50))} ({"aberta" if s.state == "open" else "fechada"})'
            for s in others[:3]
        ]
        lines += ["", "🔁 <b>Parecidas:</b>\n" + "\n".join(links)]
    if draft["model"]:
        lines += ["", f"<i>🤖 Rascunho gerado com {escape(draft['model'])}. Confira antes de criar.</i>"]
    return "\n".join(lines)


async def show_preview(update: Update, context: Ctx, *, edit_in: Message | None = None) -> int:
    draft = draft_of(context)
    draft["editing"] = False
    text, markup = preview(draft, context), keyboards.draft_confirm()
    if edit_in:
        await edit(edit_in, text, markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    return CONFIRM


async def confirm(update: Update, context: Ctx) -> int | None:
    query = update.callback_query
    action = query.data.removeprefix("n:")
    draft = draft_of(context)

    if action == "cancel":
        await query.answer("Cancelado")
        await edit(query.message, "❌ <i>Criação de issue cancelada.</i>")
        context.user_data.pop("draft", None)
        return ConversationHandler.END

    if action == "e:labels":
        await query.answer()
        return await ask_labels(update, context, edit_in=query.message)

    if action == "e:repo":
        await query.answer()
        draft["editing"] = True
        return await ask_repo(update, context, edit_in=query.message)

    if action == "e:who":
        await query.answer()
        return await show_assignee_picker(update, context, edit_in=query.message)

    if action in ("e:title", "e:body"):
        await query.answer()
        draft["editing"] = True
        await query.message.edit_reply_markup(None)
        if action == "e:title":
            return await ask_title(update, f"Título atual: <code>{escape(draft['title'])}</code>\n")
        return await ask_body(update, "✏️ Mande a nova descrição (/pular mantém a atual).\n")

    # action == "create"
    await query.answer("Criando…")
    await create(update, context, query.message)
    context.user_data.pop("draft", None)
    return ConversationHandler.END


# ---- responsável ---------------------------------------------------------------


async def show_assignee_picker(
    update: Update, context: Ctx, *, edit_in: Message | None = None, intro: str = ""
) -> int:
    draft = draft_of(context)
    text = intro + "👤 <b>Responsável pela issue</b>"
    if draft["assignee_options"]:
        text += "\nOs mais parecidos com o nome pedido:"
    markup = keyboards.assignee_picker(draft["assignee_options"], draft["assignee"])
    if edit_in:
        await edit(edit_in, text, markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    return CONFIRM


async def pick_assignee(update: Update, context: Ctx) -> int:
    query = update.callback_query
    choice = query.data.split(":", 2)[2]
    draft = draft_of(context)

    if choice == "?":
        await query.answer()
        await query.message.edit_reply_markup(None)
        await ask(update, "🔎 Qual o nome da pessoa? Pode ser aproximado.", "Nome ou login do GitHub")
        return WHO
    if choice == "-":
        draft["assignee"] = None
    elif choice != "<":
        people = {p.login.lower(): p for p in await gh(context).assignable_people(draft["repo"])}
        person = people.get(choice.lower())
        if not person:
            await query.answer("Essa pessoa não pode ser responsável neste repositório.", show_alert=True)
            return CONFIRM
        draft["assignee"] = person
    await query.answer()
    return await show_preview(update, context, edit_in=query.message)


async def got_who(update: Update, context: Ctx) -> int:
    draft = draft_of(context)
    name = update.effective_message.text.strip()
    await typing(update, context)
    matches = find_people(name, await gh(context).assignable_people(draft["repo"]), limit=5)
    draft["assignee_options"] = [m.person for m in matches]
    intro = "" if matches else f"Não achei ninguém parecido com <b>{escape(name)}</b> em {escape(draft['repo'])}.\n\n"
    return await show_assignee_picker(update, context, intro=intro)


async def create(update: Update, context: Ctx, message: Message) -> None:
    # O rascunho só sai do user_data depois de criado: se o GitHub falhar, dá para tentar de novo.
    draft = draft_of(context)
    await typing(update, context)
    user = update.effective_user
    note = f"rascunho gerado com {draft['model']}" if draft["model"] else None
    images_md, images_warning = await upload_images(context, draft)
    body = (draft["body"].strip() + images_md + telegram_signature(user.full_name, user.username, note)).lstrip()
    assignees = [draft["assignee"].login] if draft["assignee"] else None
    issue = await gh(context).create_issue(draft["repo"], draft["title"], body, draft["labels"], assignees)
    log.info("Issue %s criada por %s", issue.ref, user.id)
    text = f'✅ Issue <a href="{issue.url}"><b>{escape(issue.ref)}</b></a> criada!\n<i>{escape(issue.title)}</i>'
    if issue.labels:
        text += "\n" + " ".join(f"<code>{escape(lb)}</code>" for lb in issue.labels)
    if issue.assignees:
        text += "\n👤 " + ", ".join(f"@{escape(a)}" for a in issue.assignees)
    project_line, project_url = await put_in_project(context, issue)
    await edit(message, text + images_warning + project_line, keyboards.open_issue(issue, project_url))


async def upload_images(context: Ctx, draft: dict[str, Any]) -> tuple[str, str]:
    """Sobe as imagens para o repositório de anexos. Devolve o Markdown para a issue e, se
    algo falhar, um aviso para a mensagem (a issue é criada mesmo assim)."""
    store: AssetStore | None = context.bot_data.get("assets")
    if not draft["images"] or not store:
        return "", ""
    try:
        urls = [await store.upload_image(img, draft["repo"]) for img in draft["images"]]
    except GitHubError as exc:
        log.warning("Não consegui anexar a imagem: %s", exc.message)
        return "", f"\n⚠️ <i>A imagem não foi anexada: {escape(exc.message)}</i>"
    return "\n\n## Imagens\n\n" + "\n\n".join(f"![Imagem enviada pelo Telegram]({u})" for u in urls), ""


async def put_in_project(context: Ctx, issue: Issue) -> tuple[str, str | None]:
    """Coloca a issue no GitHub Project (GITHUB_PROJECT). Uma falha aqui não desfaz a issue:
    vira um aviso na mensagem."""
    cfg = settings(context)
    if not cfg.project:
        return "", None
    try:
        project = context.bot_data.get("project") or await gh(context).get_project(*cfg.project)
        context.bot_data["project"] = project
        status = await gh(context).add_to_project(project, issue, cfg.project_status)
    except GitHubError as exc:
        log.warning("Issue %s não entrou no Project: %s", issue.ref, exc.message)
        return f"\n⚠️ <i>Não consegui colocar no Project: {escape(exc.message)}</i>", None
    column = f" · {escape(status)}" if status else ""
    return f'\n📋 No Project <a href="{project.url}">{escape(project.title)}</a>{column}', project.url


async def unexpected(update: Update, context: Ctx) -> None:
    await update.effective_message.reply_text("Estou no meio da criação de uma issue. Use os botões acima ou /cancelar.")


def conversation() -> ConversationHandler:
    text = filters.TEXT & ~filters.COMMAND
    image = filters.PHOTO | filters.Document.IMAGE
    # Misturar CallbackQueryHandler com per_message=False gera um aviso informativo do PTB;
    # é exatamente o comportamento que queremos (uma conversa por pessoa em cada chat).
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PTBUserWarning)
        return ConversationHandler(
            entry_points=[CommandHandler("nova", start)],
            states={
                REPO: [
                    CallbackQueryHandler(repo_chosen, pattern=r"^n:r:[\w.-]+$"),
                    CallbackQueryHandler(repo_page, pattern=r"^n:rp:\d+$"),
                    MessageHandler(text, unexpected),
                ],
                DESCRIBE: [
                    MessageHandler(text, describe),
                    MessageHandler(image, describe_photo),
                    CommandHandler("manual", manual),
                ],
                TITLE: [MessageHandler(text, got_title)],
                BODY: [
                    MessageHandler(text, got_body),
                    MessageHandler(image, got_body_photo),
                    CommandHandler("pular", skip_body),
                ],
                LABELS: [
                    CallbackQueryHandler(toggle_label, pattern=r"^n:l:\d+$"),
                    CallbackQueryHandler(labels_done, pattern=r"^n:ok$"),
                    MessageHandler(text, unexpected),
                ],
                CONFIRM: [
                    CallbackQueryHandler(
                        confirm, pattern=r"^n:(create|cancel|e:title|e:body|e:labels|e:repo|e:who)$"
                    ),
                    CallbackQueryHandler(pick_assignee, pattern=r"^n:a:"),
                    MessageHandler(text, unexpected),
                ],
                WHO: [MessageHandler(text, got_who)],
            },
            fallbacks=[CommandHandler("cancelar", cancel_command)],
            allow_reentry=True,
            name="nova_issue",
        )
