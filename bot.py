import logging
import os
import re

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import database
import scheduler
from uni import GroupNotFound, fetch_groups, fetch_timetables

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHECK_EVERY_MINUTES = int(os.getenv("CHECK_EVERY_MINUTES", "30"))

PAGE_SIZE = 12                 # groups per page in the keyboard
GROUPS_MAX_AGE = 24 * 3600     # refresh the group list once a day
ON_DEMAND_CACHE = 120          # /schedule reuses a result younger than 2 min
VALID_GROUP = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,59}")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("bot")


# ------------------------------------------------------------------ helpers
def short_error(e: Exception) -> str:
    lines = str(e).splitlines() or [type(e).__name__]
    return lines[0][:200]


def remember_user(update: Update):
    """Save the Telegram username / name of whoever is talking to the bot."""
    user = update.effective_user
    if user and update.effective_chat:
        database.save_user_info(update.effective_chat.id, user.username, user.full_name)


def chunk(items: list, n: int) -> list[list]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def prefix_of(name: str) -> str:
    return name.split("_")[0] if "_" in name else "Other"


def normalize(text: str) -> str:
    return re.sub(r"[\s_]+", "_", text.strip().lower())


def find_matches(query: str, groups: list[str]) -> list[str]:
    q = normalize(query)
    exact = [g for g in groups if normalize(g) == q]
    if exact:
        return exact[:1]
    tokens = [t for t in q.split("_") if t]
    if not tokens:
        return []
    return [g for g in groups if all(t in g.lower() for t in tokens)]


async def get_groups() -> list[str]:
    """Cached list of groups; refreshed from the website once a day."""
    groups = database.get_groups()
    if groups and database.groups_age() < GROUPS_MAX_AGE:
        return groups
    try:
        fresh = await fetch_groups()
        if fresh:
            database.save_groups(fresh)
            return fresh
    except Exception:
        log.exception("Could not refresh group list")
    return groups


def prefix_keyboard(groups: list[str]) -> InlineKeyboardMarkup:
    prefixes = sorted({prefix_of(g) for g in groups})
    buttons = [InlineKeyboardButton(p, callback_data=f"p:{p}:0") for p in prefixes]
    return InlineKeyboardMarkup(chunk(buttons, 3))


def groups_keyboard(groups: list[str], prefix: str, page: int) -> InlineKeyboardMarkup:
    items = [g for g in groups if prefix_of(g) == prefix]
    start = page * PAGE_SIZE
    shown = [g for g in items[start:start + PAGE_SIZE] if len(f"g:{g}".encode()) <= 64]
    rows = chunk([InlineKeyboardButton(g, callback_data=f"g:{g}") for g in shown], 2)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"p:{prefix}:{page - 1}"))
    if start + PAGE_SIZE < len(items):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"p:{prefix}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def matches_keyboard(matches: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(g, callback_data=f"g:{g}")
        for g in matches[:20] if len(f"g:{g}".encode()) <= 64
    ]
    return InlineKeyboardMarkup(chunk(buttons, 2))


async def send_chooser(message, groups: list[str]):
    if not groups:
        await message.reply_text(
            "I couldn't load the list of groups right now.\n"
            "Type your group name instead, for example:\n/group L3_INFO_TD3"
        )
        return
    await message.reply_text("Choose your level:", reply_markup=prefix_keyboard(groups))


async def send_timetable(message, group: str):
    results = await fetch_timetables([group], max_age=ON_DEMAND_CACHE)
    res = results[group]
    if isinstance(res, Exception):
        raise res
    path, h, title = res
    if database.get_hash(group) is None:
        database.set_hash(group, h)
    with open(path, "rb") as f:
        await message.reply_photo(f, caption=title or f"📅 {group}")


async def apply_group(message, chat_id: int, group: str):
    previous = database.get_group(chat_id)
    database.set_group(chat_id, group)
    status = await message.reply_text(f"✅ Group set to {group}\n⏳ Fetching your timetable...")
    try:
        await send_timetable(message, group)
        await status.delete()
    except GroupNotFound:
        database.set_group(chat_id, previous)
        await status.edit_text(f"❌ No timetable found for {group}. Your group was not changed.")
    except Exception as e:
        log.exception("apply_group failed")
        await status.edit_text(
            f"⚠️ Group saved, but fetching failed: {short_error(e)}\nTry /schedule again in a moment."
        )


# ------------------------------------------------------------------ commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    remember_user(update)
    group = database.get_group(chat_id)

    text = (
        "Hi! 👋\n\n"
        "/group - choose your group\n"
        "/schedule - get your timetable now\n"
        "/schedule L2_INFO_TD1 - peek at any other group\n"
        "/mygroup - show your group\n"
        "/stop - stop automatic updates\n\n"
        f"I check every {CHECK_EVERY_MINUTES} min and message you when your timetable changes."
    )
    await update.message.reply_text(text)

    if group:
        await update.message.reply_text(f"Your current group: {group}")
    else:
        await send_chooser(update.message, await get_groups())


async def group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    chat_id = update.effective_chat.id
    groups = await get_groups()

    if not context.args:
        await send_chooser(update.message, groups)
        return

    query = " ".join(context.args)
    matches = find_matches(query, groups)

    if len(matches) == 1:
        await apply_group(update.message, chat_id, matches[0])
    elif matches:
        await update.message.reply_text(
            f"{len(matches)} groups match, pick one:", reply_markup=matches_keyboard(matches)
        )
    elif not groups and VALID_GROUP.fullmatch(query.strip()):
        # list unavailable: trust the name, apply_group() will verify it
        await apply_group(update.message, chat_id, query.strip())
    else:
        await update.message.reply_text("No group matches that. Try /group to browse the list.")


async def mygroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    group = database.get_group(update.effective_chat.id)
    await update.message.reply_text(
        f"Your group: {group}" if group else "You haven't chosen a group yet. Use /group"
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    database.remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("Unsubscribed. Send /start to subscribe again.")


async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    chat_id = update.effective_chat.id

    if context.args:
        query = " ".join(context.args)
        groups = await get_groups()
        matches = find_matches(query, groups)
        if len(matches) == 1:
            group = matches[0]
        elif matches:
            await update.message.reply_text(
                "Be more specific, for example:\n" + "\n".join(matches[:10])
            )
            return
        elif not groups and VALID_GROUP.fullmatch(query.strip()):
            group = query.strip()
        else:
            await update.message.reply_text("Unknown group. Use /group to browse the list.")
            return
    else:
        group = database.get_group(chat_id)
        if not group:
            await update.message.reply_text("Choose your group first: /group")
            return

    msg = await update.message.reply_text(f"⏳ Fetching {group}...")
    try:
        await send_timetable(update.message, group)
        await msg.delete()
    except GroupNotFound:
        await msg.edit_text(f"❌ No timetable found for {group}")
    except Exception as e:
        log.exception("schedule failed")
        await msg.edit_text(f"❌ Failed: {short_error(e)}")


# ------------------------------------------------------------------ buttons
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "back":
        groups = await get_groups()
        await q.edit_message_text("Choose your level:", reply_markup=prefix_keyboard(groups))

    elif data.startswith("p:"):
        _, prefix, page = data.split(":", 2)
        groups = await get_groups()
        await q.edit_message_text(
            f"{prefix} - choose your group:",
            reply_markup=groups_keyboard(groups, prefix, int(page)),
        )

    elif data.startswith("g:"):
        group = data[2:]
        await q.edit_message_text(f"Selected: {group}")
        await apply_group(q.message, q.message.chat_id, group)


# ------------------------------------------------------------------ main
async def backfill_names(app: Application):
    """Fill in username/name for users saved before we stored them."""
    for chat_id in database.users_missing_info():
        try:
            chat = await app.bot.get_chat(chat_id)
            full_name = " ".join(filter(None, [chat.first_name, chat.last_name]))
            database.save_user_info(chat_id, chat.username, full_name or "(unknown)")
        except Exception as e:
            log.warning("Could not fetch info for %s: %s", chat_id, e)


async def post_init(app: Application):
    await backfill_names(app)
    await app.bot.set_my_commands([
        BotCommand("group", "Choose your group"),
        BotCommand("schedule", "Get your timetable"),
        BotCommand("mygroup", "Show your group"),
        BotCommand("stop", "Stop automatic updates"),
    ])


def main():
    database.init()
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("group", group_cmd))
    app.add_handler(CommandHandler("mygroup", mygroup))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("schedule", schedule))
    app.add_handler(CallbackQueryHandler(on_button))

    scheduler.setup(app, CHECK_EVERY_MINUTES)
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()