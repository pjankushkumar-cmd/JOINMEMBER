"""
JANEMAN Telegram admin bot — join-request welcome messages with custom emoji.

Environment:
  BOT_TOKEN  = fresh Telegram bot token from @BotFather
  ADMIN_ID   = your numeric Telegram user ID
  DB_PATH    = optional SQLite path (default: janeman_pro.db)
Install: pip install -U python-telegram-bot
Run:     python janemanrequest_webservice.py

Join-request DMs use ChatJoinRequest.user_chat_id. Telegram allows contacting
that private chat for a limited period (5 minutes) after a join request, provided
the bot is an admin with can_invite_users. Send saved messages BEFORE approving
the request. copy_message is used to preserve Telegram-supported message entities,
including custom emoji, when Telegram permits copying that message.
"""
import logging
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ChatJoinRequestHandler, ContextTypes, MessageHandler, filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
log = logging.getLogger("janeman")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0
DB_PATH = os.getenv("DB_PATH", "janeman_pro.db")
DB_LOCK = threading.RLock()
CACHED_MESSAGES = []


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Render Web Service health endpoint; also supports /health checks.
        body = b"JANEMAN service is running\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.debug("Health endpoint: " + fmt, *args)


def run_health_server():
    port = int(os.getenv("PORT", "8080"))
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
        log.info("Health endpoint listening on port %s", port)
        server.serve_forever()
    except OSError:
        log.exception("Health endpoint could not start")


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    global CACHED_MESSAGES
    with DB_LOCK, db_connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")
        conn.execute("""CREATE TABLE IF NOT EXISTS messages_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            msg_id TEXT NOT NULL
        )""")
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('auto_accept','OFF')")
        conn.execute("INSERT OR IGNORE INTO stats(key,count) VALUES('total_requests',0)")
        conn.execute("INSERT OR IGNORE INTO stats(key,count) VALUES('accepted',0)")
        CACHED_MESSAGES = conn.execute(
            "SELECT chat_id,msg_id FROM messages_list ORDER BY id ASC"
        ).fetchall()


def get_setting(key, default="OFF"):
    with DB_LOCK, db_connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
        )


def add_saved_message(chat_id, msg_id):
    global CACHED_MESSAGES
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "INSERT INTO messages_list(chat_id,msg_id) VALUES(?,?)",
            (str(chat_id), str(msg_id))
        )
        CACHED_MESSAGES = conn.execute(
            "SELECT chat_id,msg_id FROM messages_list ORDER BY id ASC"
        ).fetchall()


def clear_saved_messages():
    global CACHED_MESSAGES
    with DB_LOCK, db_connect() as conn:
        conn.execute("DELETE FROM messages_list")
        CACHED_MESSAGES = []


def add_user(user_id):
    with DB_LOCK, db_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (int(user_id),))


def get_all_users():
    with DB_LOCK, db_connect() as conn:
        return [r[0] for r in conn.execute("SELECT user_id FROM users ORDER BY user_id")]


def get_stats():
    with DB_LOCK, db_connect() as conn:
        return dict(conn.execute("SELECT key,count FROM stats").fetchall())


def bump_stat(key, amount=1):
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "INSERT INTO stats(key,count) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET count=count+excluded.count",
            (key, amount)
        )


def is_admin(update):
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


def main_menu():
    s = get_stats()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📊 Join requests: {s.get('total_requests', 0)}", callback_data="noop")],
        [InlineKeyboardButton(f"✅ Auto-approved: {s.get('accepted', 0)}", callback_data="noop")],
        [InlineKeyboardButton(f"👥 Users who started bot: {len(get_all_users())}", callback_data="noop")],
        [InlineKeyboardButton("⚙️ Welcome settings", callback_data="welcome")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="broadcast")],
        [InlineKeyboardButton("🔄 Refresh panel", callback_data="home")],
    ])


def welcome_menu():
    status = get_setting("auto_accept")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Auto-approve: {'🟢 ON' if status == 'ON' else '🔴 OFF'}", callback_data="toggle_auto")],
        [InlineKeyboardButton(f"➕ Add message/media ({len(CACHED_MESSAGES)} saved)", callback_data="add_welcome")],
        [InlineKeyboardButton("🗑️ Clear saved messages", callback_data="clear_welcome")],
        [InlineKeyboardButton("👁️ Test sequence to admin", callback_data="test")],
        [InlineKeyboardButton("⬅️ Main menu", callback_data="home")],
    ])


async def edit_panel(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            log.warning("Panel edit failed: %s", exc)
            await query.message.reply_text(text, reply_markup=markup)
    except TelegramError:
        log.exception("Panel edit failed")
        await query.message.reply_text(text, reply_markup=markup)


async def copy_sequence(bot, destination_chat_id):
    sent = failed = 0
    for source_chat_id, source_message_id in list(CACHED_MESSAGES):
        try:
            await bot.copy_message(
                chat_id=int(destination_chat_id),
                from_chat_id=int(source_chat_id),
                message_id=int(source_message_id),
            )
            sent += 1
        except TelegramError as exc:
            failed += 1
            log.warning("Copy message %s from %s failed to %s: %s",
                        source_message_id, source_chat_id, destination_chat_id, exc)
    return sent, failed


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user:
        return
    add_user(user.id)
    if not is_admin(update):
        await msg.reply_text("Welcome! Bot active hai.")
        return
    context.user_data.pop("state", None)
    await msg.reply_text(
        "👑 JANEMAN ADMIN PANEL\n\n"
        "Welcome Settings → Add message/media mein custom-emoji messages set karein.\n"
        "Commands: /help /stats /id /done /cancel",
        reply_markup=main_menu()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text(
            "/start — open admin panel\n/help — help\n/stats — statistics\n"
            "/id — show numeric ID\n/done — finish welcome sequence\n/cancel — cancel action\n\n"
            "Saved messages are copied to the join request's user_chat_id before "
            "auto-approval, so the user does not need to start the bot first."
        )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_message:
        await update.effective_message.reply_text(f"Your Telegram ID: {update.effective_user.id}")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not update.effective_message:
        return
    s = get_stats()
    await update.effective_message.reply_text(
        f"📊 Statistics\nJoin requests: {s.get('total_requests', 0)}\n"
        f"Auto-approved: {s.get('accepted', 0)}\nUsers who started bot: {len(get_all_users())}\n"
        f"Saved welcome items: {len(CACHED_MESSAGES)}\nAuto-approve: {get_setting('auto_accept')}"
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return
    if is_admin(update):
        context.user_data.pop("state", None)
        await update.effective_message.reply_text("Cancelled. Use /start to reopen panel.")


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not update.effective_message:
        return
    if context.user_data.get("state") == "waiting_welcome":
        context.user_data.pop("state", None)
        await update.effective_message.reply_text(
            f"✅ Saved sequence finished. Total: {len(CACHED_MESSAGES)}",
            reply_markup=welcome_menu()
        )
    else:
        await update.effective_message.reply_text("Nothing to finish. Open /start → Welcome settings.")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    if q.from_user.id != ADMIN_ID:
        await q.answer("Access denied", show_alert=True)
        return
    await q.answer()
    action = q.data or ""

    if action == "noop":
        return
    if action == "home":
        context.user_data.pop("state", None)
        await edit_panel(q, "👑 JANEMAN ADMIN PANEL\nChoose a feature:", main_menu())
    elif action == "welcome":
        await edit_panel(q,
            f"⚙️ Welcome settings\nAuto-approve: {get_setting('auto_accept')}\n"
            f"Saved messages: {len(CACHED_MESSAGES)}\n"
            "Saved messages are copied as Telegram messages, preserving supported custom emoji.",
            welcome_menu())
    elif action == "toggle_auto":
        value = "OFF" if get_setting("auto_accept") == "ON" else "ON"
        set_setting("auto_accept", value)
        await edit_panel(q, f"⚙️ Welcome settings\nAuto-approve: {value}\nSaved: {len(CACHED_MESSAGES)}",
                         welcome_menu())
    elif action == "add_welcome":
        context.user_data["state"] = "waiting_welcome"
        await edit_panel(q,
            "📝 Ab apna text/custom emoji, photo, video, voice, sticker ya media bhejo.\n"
            "Har message sequence mein save hoga. Complete hone par /done; cancel ke liye /cancel.")
    elif action == "clear_welcome":
        clear_saved_messages()
        await edit_panel(q, "🗑️ Welcome sequence cleared.", welcome_menu())
    elif action == "test":
        sent, failed = await copy_sequence(context.bot, ADMIN_ID)
        await q.message.reply_text(
            f"Test result — sent: {sent}, failed: {failed}." if sent or failed
            else "Sequence empty hai. Add message/media first.")
    elif action == "broadcast":
        context.user_data["state"] = "waiting_broadcast"
        await edit_panel(q, "📣 Ab broadcast ke liye ek message/media bhejo.\nCancel: /cancel")


async def content_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not is_admin(update):
        return
    state = context.user_data.get("state")
    if state == "waiting_welcome":
        add_saved_message(msg.chat_id, msg.message_id)
        await msg.reply_text(f"✅ Saved item #{len(CACHED_MESSAGES)}. Send another item or /done.")
    elif state == "waiting_broadcast":
        context.user_data.pop("state", None)
        users = get_all_users()
        await msg.reply_text(f"📣 Broadcast started for {len(users)} users.")
        sent = failed = 0
        for user_id in users:
            try:
                await context.bot.copy_message(
                    chat_id=user_id, from_chat_id=msg.chat_id, message_id=msg.message_id
                )
                sent += 1
            except TelegramError as exc:
                failed += 1
                log.info("Broadcast failed to %s: %s", user_id, exc)
        await msg.reply_text(f"🏁 Broadcast complete\n✅ Sent: {sent}\n❌ Failed: {failed}")


async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    request = update.chat_join_request
    if not request:
        return
    bump_stat("total_requests")
    # CRITICAL: use user_chat_id, not from_user.id, to contact join requester.
    # Telegram grants this chat ID a limited 5-minute window after the request.
    destination = request.user_chat_id
    if CACHED_MESSAGES:
        sent, failed = await copy_sequence(context.bot, destination)
        log.info("Welcome sequence for request %s: sent=%s failed=%s",
                 request.from_user.id, sent, failed)

    # Send welcome items before approving, so the temporary contact window remains valid.
    if get_setting("auto_accept") == "ON":
        try:
            await context.bot.approve_chat_join_request(
                chat_id=request.chat.id, user_id=request.from_user.id
            )
            bump_stat("accepted")
        except TelegramError:
            log.exception("Could not approve join request from %s", request.from_user.id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled Telegram error: %s", context.error, exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing. Set a NEW token from @BotFather in environment variables.")
    if ADMIN_ID <= 0:
        raise SystemExit("ADMIN_ID missing/invalid. Set your numeric Telegram user ID.")

    init_db()
    threading.Thread(target=run_health_server, name="health-server", daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).bootstrap_retries(-1).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, content_handler))
    app.add_error_handler(error_handler)
    log.info("JANEMAN bot starting")
    # Retry Telegram bootstrap/network failures instead of exiting immediately.
    # Can run as a Render Web Service because the HTTP health server binds PORT.
    # This endpoint does not itself prevent free-tier sleep; use an external monitor
    # only if permitted by your hosting plan/policies, or choose an always-on plan.
    app.run_polling(
        poll_interval=1.0,
        timeout=30,
        bootstrap_retries=-1,
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
