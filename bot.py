#!/usr/bin/env python3
"""
BOTNET SIMULATION BOT — FOR EDUCATIONAL PURPOSES ONLY
This bot SIMULATES a botnet mass-reporting interface.
It does NOTHING real — no actual reports, no network attacks, no real botnet.
All IPs, bot IDs, and delays are randomly generated fakery.
"""

import os
import json
import asyncio
import random
import string
import logging
from datetime import datetime, timedelta
import httpx

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
import time
import atexit

dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path)
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
REQUIRED_CHANNEL_ID = os.getenv("REQUIRED_CHANNEL_ID", "")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "")
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "")
CRYPTO_PAY_ASSET = os.getenv("CRYPTO_PAY_ASSET", "USDT")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.json")

TOTAL_SESSIONS = 368
USERS_PER_PAGE = 10

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FAKE_IPS = [
    "10.{}.{}.{}", "172.16.{}.{}", "192.168.{}.{}",
    "45.33.{}.{}", "89.45.{}.{}", "103.{}.{}.{}",
]
FAKE_COUNTRIES = [
    "RU", "UA", "CN", "US", "BR", "IN", "NG", "DE", "KR", "IR",
    "VN", "RO", "PL", "IL", "HK", "SG", "ZA", "AR", "MY", "TH",
]
FAKE_SESSION_NAMES = [
    "NanoCore", "DarkGate", "RedLine", "Vidar", "AgentTesla",
    "FormBook", "LokiBot", "QakBot", "Emotet", "TrickBot",
    "AsyncRAT", "DcRAT", "Warzone", "njRAT", "Orcus",
    "BlackRock", "Ermac", "Coper", "Xenomorph", "Octo",
]

REASONS = {
    "spam": "Спам",
    "abuse": "Оскорбления",
    "tos": "Нарушение правил",
    "impersonation": "Выдача себя за другого",
    "harassment": "Преследование",
    "scam": "Мошенничество",
    "illegal": "Незаконный контент",
    "copyright": "Нарушение авторских прав",
}

TARGET, REASON = range(2)
SUBSCRIPTION_DURATIONS = [1, 7, 30, 0]  # 0 = навсегда



# ============================================================
# OPTIMIZATION: In-memory DB cache + async I/O + write debouncing
# ============================================================

_db_cache = None
_db_cache_time = 0.0
_db_cache_lock = None
_db_write_lock = None
_db_dirty = False
_db_write_task = None
DB_CACHE_TTL = 0.5
DB_DEBOUNCE_DELAY = 1.0


def _load_db_sync() -> dict:
    if not os.path.exists(DB_PATH):
        default = {"admins": list(set(ADMIN_IDS)), "users": {}}
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2, ensure_ascii=False)
        return default
    with open(DB_PATH, "r", encoding="utf-8") as f:
        db = json.load(f)
    db.setdefault("admins", [])
    db["admins"] = list(set(db["admins"] + ADMIN_IDS))
    db.setdefault("users", {})
    db.setdefault("keys", {})
    db.setdefault("pending_payments", {})
    db["prices"] = db.get("prices", {"1 день": 1, "7 дней": 5, "30 дней": 15, "Навсегда": 30})
    return db


def _save_db_sync(db: dict) -> None:
    tmp_path = DB_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp_path, DB_PATH)


async def load_db() -> dict:
    global _db_cache, _db_cache_time
    async with _db_cache_lock:
        now = time.monotonic()
        if _db_cache is not None and (now - _db_cache_time) < DB_CACHE_TTL:
            return _db_cache
        _db_cache = await asyncio.to_thread(_load_db_sync)
        _db_cache_time = now
        return _db_cache


async def _debounced_write_task() -> None:
    global _db_dirty, _db_write_task
    try:
        await asyncio.sleep(DB_DEBOUNCE_DELAY)
        async with _db_write_lock:
            if _db_dirty and _db_cache is not None:
                await asyncio.to_thread(_save_db_sync, _db_cache)
                _db_dirty = False
    except asyncio.CancelledError:
        pass
    finally:
        _db_write_task = None


async def save_db(db: dict) -> None:
    global _db_cache, _db_cache_time, _db_dirty, _db_write_task
    async with _db_cache_lock:
        _db_cache = db
        _db_cache_time = time.monotonic()
        _db_dirty = True
        if _db_write_task is None:
            _db_write_task = asyncio.create_task(_debounced_write_task())


async def flush_db() -> None:
    global _db_dirty, _db_write_task
    if _db_write_task is not None:
        _db_write_task.cancel()
        _db_write_task = None
    async with _db_write_lock:
        if _db_dirty and _db_cache is not None:
            await asyncio.to_thread(_save_db_sync, _db_cache)
            _db_dirty = False


def _sync_flush_db() -> None:
    global _db_dirty, _db_cache
    if _db_dirty and _db_cache is not None:
        _save_db_sync(_db_cache)
        _db_dirty = False






async def register_user(db: dict, user) -> dict:
    uid = str(user.id)
    if uid not in db["users"]:
        db["users"][uid] = {
            "id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "registered_at": datetime.now().isoformat(),
            "banned": False,
            "ban_reason": None,
            "subscription": None,
            "reports_used": 0,
        }
        await save_db(db)
    return db["users"][uid]


def has_subscription(user_data: dict) -> bool:
    sub = user_data.get("subscription")
    if not sub:
        return False
    if sub.get("active") is not True:
        return False
    expires = sub.get("expires")
    if expires:
        try:
            if datetime.fromisoformat(expires) < datetime.now():
                sub["active"] = False
                return False
        except (ValueError, TypeError):
            return False
    return True


def is_admin(db: dict, user_id: int) -> bool:
    return user_id in db["admins"]


def is_banned(user_data: dict) -> bool:
    return user_data.get("banned", False)


async def is_subscribed(bot, user_id: int) -> bool:
    if not REQUIRED_CHANNEL_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL_ID, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


def generate_key() -> str:
    chars = string.ascii_uppercase + string.digits
    return f"{''.join(random.choices(chars, k=4))}-{''.join(random.choices(chars, k=4))}-{''.join(random.choices(chars, k=4))}"


def progress_bar(current: int, total: int, width: int = 12) -> str:
    filled = int(current / total * width) if total else 0
    empty = width - filled
    return "▓" * filled + "░" * empty


def user_display_name(udata: dict) -> str:
    return udata.get("username") or udata.get("first_name") or str(udata.get("id", "?"))



# Initialize async locks for DB cache
_db_cache_lock = asyncio.Lock()
_db_write_lock = asyncio.Lock()
atexit.register(_sync_flush_db)
# ─────────────────────── KEYBOARDS ───────────────────────

def main_menu_keyboard(is_admin_user: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🚀 Атаковать", callback_data="attack")],
        [InlineKeyboardButton("👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")],
    ]
    if is_admin_user:
        buttons.append([InlineKeyboardButton("🛠 Админ панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)


def back_keyboard(dest: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=dest)]])


def reason_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for i, (key, label) in enumerate(REASONS.items(), 1):
        row.append(InlineKeyboardButton(label, callback_data=f"reason_{key}"))
        if i % 2 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("◀️ Отмена", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Пользователи", callback_data="admin_users_page_0"),
         InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("🔑 Ключи", callback_data="admin_keys"),
         InlineKeyboardButton("💲 Прайс", callback_data="admin_prices")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu")],
    ])


def attack_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Новая атака", callback_data="attack")],
        [InlineKeyboardButton("◀️ В меню", callback_data="menu")],
    ])


def users_page_keyboard(db_users: dict, page: int) -> InlineKeyboardMarkup:
    buttons = []
    user_ids = sorted(db_users.keys(), key=lambda uid: db_users[uid].get("registered_at", ""), reverse=True)
    total = len(user_ids)
    total_pages = max(1, (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    start = page * USERS_PER_PAGE
    end = min(start + USERS_PER_PAGE, total)
    page_ids = user_ids[start:end]

    for uid in page_ids:
        udata = db_users[uid]
        name = user_display_name(udata)
        label = f"@{name}" if udata.get("username") else name
        flags = ""
        if is_banned(udata):
            flags += "🚫"
        if has_subscription(udata):
            flags += "✅"
        buttons.append([InlineKeyboardButton(f"{label} {flags}",
                                             callback_data=f"admin_user_{uid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"admin_users_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if end < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"admin_users_page_{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("◀️ В админ панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)


def user_management_keyboard(uid_str: str, udata: dict, user_is_admin: bool) -> InlineKeyboardMarkup:
    buttons = []

    if has_subscription(udata):
        buttons.append([InlineKeyboardButton("🎫 Забрать подписку",
                                             callback_data=f"admin_user_{uid_str}_removesub")])
    else:
        dur_row = []
        for d in SUBSCRIPTION_DURATIONS[:2]:
            label = "∞" if d == 0 else f"+{d}д"
            dur_row.append(InlineKeyboardButton(f"🎫 {label}",
                                                callback_data=f"admin_user_{uid_str}_givesub_{d}"))
        buttons.append(dur_row)
        dur_row2 = []
        for d in SUBSCRIPTION_DURATIONS[2:]:
            label = "∞" if d == 0 else f"+{d}д"
            dur_row2.append(InlineKeyboardButton(f"🎫 {label}",
                                                 callback_data=f"admin_user_{uid_str}_givesub_{d}"))
        buttons.append(dur_row2)

    if is_banned(udata):
        buttons.append([InlineKeyboardButton("✅ Разбанить",
                                             callback_data=f"admin_user_{uid_str}_unban")])
    else:
        buttons.append([InlineKeyboardButton("🚫 Забанить",
                                             callback_data=f"admin_user_{uid_str}_ban")])

    if user_is_admin:
        if int(uid_str) in ADMIN_IDS:
            buttons.append([InlineKeyboardButton("👑 Из .env", callback_data="noop")])
        else:
            buttons.append([InlineKeyboardButton("❌ Убрать админа",
                                                 callback_data=f"admin_user_{uid_str}_removeadmin")])
    else:
        buttons.append([InlineKeyboardButton("👑 Сделать админом",
                                             callback_data=f"admin_user_{uid_str}_makeadmin")])

    buttons.append([InlineKeyboardButton("◀️ Назад к списку", callback_data="admin_users_page_0")])
    return InlineKeyboardMarkup(buttons)


def key_duration_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for i, d in enumerate(SUBSCRIPTION_DURATIONS, 1):
        label = "∞ Навсегда" if d == 0 else f"{d} дн."
        row.append(InlineKeyboardButton(label, callback_data=f"admin_key_create_{d}"))
        if i % 2 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_keys")])
    return InlineKeyboardMarkup(buttons)


def key_count_keyboard(days: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1", callback_data=f"admin_key_create_{days}_1"),
         InlineKeyboardButton("5", callback_data=f"admin_key_create_{days}_5"),
         InlineKeyboardButton("10", callback_data=f"admin_key_create_{days}_10")],
        [InlineKeyboardButton("25", callback_data=f"admin_key_create_{days}_25"),
         InlineKeyboardButton("50", callback_data=f"admin_key_create_{days}_50")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_keys")],
    ])


# ─────────────────────── START & MENU ───────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db = await load_db()
    await register_user(db, user)

    if update.message:
        msg = update.message
    elif update.callback_query:
        msg = update.callback_query.message
    else:
        return

    admin_flag = is_admin(db, user.id)
    text = (
        f"❄️ *Frosty Warnings* ❄️\n"
        f"👋 Привет, {user.first_name}!\n\n"
        f"⚡️ 368 сессий готовы к атаке.\n"
    )
    await msg.reply_html(text, reply_markup=main_menu_keyboard(admin_flag))


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    db = await load_db()
    admin_flag = is_admin(db, user.id)

    text = (
        f"❄️ *Frosty Warnings* ❄️\n"
        f"👋 Привет, {user.first_name}!\n\n"
        f"⚡️ 368 сессий готовы к атаке.\n"
    )
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=main_menu_keyboard(admin_flag))


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db = await load_db()
    prices_text = "\n".join(f"`{k}` — `${v}`" for k, v in db.get("prices", {}).items())
    text = (
        "❓ *Помощь*\n\n"
        "❄️ *Frosty Warnings*\n"
        "⚡️ Использует `368` сессий для атаки.\n\n"
        "💲 *Прайс:*\n"
        f"{prices_text}\n\n"
        "🚀 *Атаковать* — запустить атаку\n"
        "  1. Выбери причину жалобы\n"
        "  2. Наблюдай за процессом\n\n"
        "👤 *Профиль* — информация об аккаунте\n"
        "💳 Оплата / 🛠 Тех.поддержка: @unnacy\n"
    )
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=back_keyboard("menu"))


# ─────────────────────── PROFILE ───────────────────────

async def profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db = await load_db()
    user_data = await register_user(db, update.effective_user)
    uid = update.effective_user.id

    sub_status = "✅ Активна" if has_subscription(user_data) else "❌ Неактивна"
    sub_info = ""
    if has_subscription(user_data):
        sub = user_data["subscription"]
        if sub.get("type") == "lifetime" or not sub.get("expires"):
            sub_info = "\n📅 Навсегда"
        else:
            exp = sub.get("expires", "Н/Д")[:10]
            sub_info = f"\n📅 Истекает: `{exp}`"

    keyboard = [
        [InlineKeyboardButton("🎫 Ввести ключ", callback_data="activate_key")],
        [InlineKeyboardButton("💳 Купить подписку", callback_data="buy_sub")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu")],
    ]

    text = (
        f"👤 *Профиль*\n\n"
        f"ID: `{uid}`\n"
        f"Username: @{update.effective_user.username or 'не указан'}\n"
        f"Имя: {user_data.get('first_name', '')}\n"
        f"Дата регистрации: {user_data.get('registered_at', 'Н/Д')[:10]}\n"
        f"Проведено атак: `{user_data.get('reports_used', 0)}`\n"
        f"Статус: {'🚫 Забанен' if is_banned(user_data) else '✅ Активен'}\n\n"
        f"🎫 *Подписка:* {sub_status}{sub_info}\n"
    )
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(keyboard))


# ─────────────────────── KEY ACTIVATION ───────────────────────

async def activate_key_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting_key"] = True

    text = (
        "🎫 *Активация ключа*\n\n"
        "Отправьте ключ активации в формате:\n"
        "`XXXX-XXXX-XXXX`\n\n"
        "Пример: `AB12-CD34-EF56`\n\n"
        "Или нажмите Отмена."
    )
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=back_keyboard("profile"))


async def handle_key_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = update.message.text.strip().upper()
    db = await load_db()
    uid = str(update.effective_user.id)

    if key not in db["keys"]:
        await update.message.reply_text(
            "❌ Неверный ключ. Проверьте правильность ввода.",
            reply_markup=back_keyboard("profile"),
        )
        context.user_data["awaiting_key"] = False
        return

    key_data = db["keys"][key]
    if key_data.get("used_by"):
        await update.message.reply_text(
            "❌ Этот ключ уже был использован.",
            reply_markup=back_keyboard("profile"),
        )
        context.user_data["awaiting_key"] = False
        return

    days = key_data["days"]
    key_data["used_by"] = int(uid)
    key_data["used_at"] = datetime.now().isoformat()

    is_lifetime = days == 0
    sub = {
        "active": True,
        "type": "lifetime" if is_lifetime else f"{days}d",
        "activated_by_key": key,
        "granted_at": datetime.now().isoformat(),
    }
    if not is_lifetime:
        sub["expires"] = (datetime.now() + timedelta(days=days)).isoformat()
    db["users"][uid]["subscription"] = sub
    await save_db(db)
    context.user_data["awaiting_key"] = False

    dur_text = "навсегда" if is_lifetime else f"на {days} дн."
    await update.message.reply_html(
        f"✅ *Подписка активирована!*\n\n"
        f"📅 {dur_text}\n\n"
        f"Теперь вы можете использовать 🚀 Атаковать.",
        reply_markup=back_keyboard("menu"),
    )


# ─────────────────────── TEXT DISPATCHER ───────────────────────

async def text_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("awaiting_key"):
        await handle_key_input(update, context)


# ─────────────────────── ATTACK ───────────────────────

async def attack_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    db = await load_db()
    user_data = await register_user(db, update.effective_user)

    if is_banned(user_data):
        reason = user_data.get("ban_reason") or "Не указана"
        await query.edit_message_text(
            f"🚫 Вы забанены.\nПричина: {reason}",
            reply_markup=back_keyboard("menu"),
        )
        return ConversationHandler.END

    if not await is_subscribed(context.bot, update.effective_user.id):
        buttons = [[InlineKeyboardButton("📢 Подписаться", url=CHANNEL_LINK)]] if CHANNEL_LINK else []
        buttons.append([InlineKeyboardButton("🔄 Я подписался", callback_data="check_sub")])
        buttons.append([InlineKeyboardButton("◀️ В меню", callback_data="menu")])
        await query.edit_message_text(
            "🚫 *Доступ запрещён*\n\n"
            "Чтобы использовать бота, подпишитесь на канал:\n"
            f"`{REQUIRED_CHANNEL_ID}`\n\n"
            "После подписки нажмите 'Я подписался'.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return ConversationHandler.END

    if not has_subscription(user_data):
        await query.edit_message_text(
            "❌ У вас нет активной подписки.\n"
            "Обратитесь к администратору для получения доступа.",
            reply_markup=back_keyboard("menu"),
        )
        return ConversationHandler.END

    text = (
        "🎯 *ШАГ 1: Укажите цель*\n\n"
        "Отправьте ссылку на сообщение:\n"
        "• `https://t.me/username/1234`\n"
    )
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=back_keyboard("menu"))
    return TARGET


async def attack_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target = update.message.text.strip()
    context.user_data["target"] = target

    text = (
        f"🎯 *Цель:* `{target}`\n\n"
        f"📋 *ШАГ 2: Выберите причину жалобы*"
    )
    await update.message.reply_html(text, reply_markup=reason_keyboard())
    return REASON


async def attack_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    reason_key = query.data.replace("reason_", "")
    reason_label = REASONS.get(reason_key, "Спам")
    target = context.user_data.get("target", "не указана")

    session_id = random.randint(10000, 99999)
    success_rate = random.uniform(0.85, 1.0)
    success_count = int(TOTAL_SESSIONS * success_rate)
    fail_count = TOTAL_SESSIONS - success_count
    total_batches = (TOTAL_SESSIONS + 14) // 15

    header_clean = (
        f"☠️ *АТАКА ЗАПУЩЕНА* ☠️\n"
        f"{'▔' * 28}\n"
        f"🎯 Цель: `{target}`\n"
        f"📋 Причина: `{reason_label}`\n"
        f"🔑 ID: `#ATK-{session_id}`\n"
        f"⚙️ Сессий: `{TOTAL_SESSIONS}`\n"
    )
    msg = await query.edit_message_text(header_clean, parse_mode="Markdown")

    batch_size = 15
    total_done = 0

    for batch_num, batch_start in enumerate(range(1, TOTAL_SESSIONS + 1, batch_size), 1):
        batch_end = min(batch_start + batch_size - 1, TOTAL_SESSIONS)
        lines = []
        for i in range(batch_start, batch_end + 1):
            sid = random.randint(1000000, 9999999)
            is_success = i > fail_count or random.random() < 0.95
            status = "✅" if is_success else "❌"
            lines.append(f"`{i:03d}` │ session-{sid} {status}")
            total_done = i

        bar = progress_bar(total_done, TOTAL_SESSIONS)
        progress_block = (
            f"\n{'▔' * 28}\n"
            f"📊 Прогресс: `{total_done}/{TOTAL_SESSIONS}`\n"
            f"`{bar}`\n"
        )
        body = "\n".join(lines)
        new_text = header_clean + body + progress_block

        try:
            if len(new_text) > 4096:
                short_body = "\n".join(lines[-12:])
                new_text = header_clean + short_body + progress_block
            await msg.edit_text(new_text, parse_mode="Markdown")
        except Exception:
            pass

        await asyncio.sleep(random.uniform(0.8, 1.5))

    total_reports = success_count * random.randint(2, 5)

    db = await load_db()
    uid = str(update.effective_user.id)
    db["users"][uid]["reports_used"] = db["users"][uid].get("reports_used", 0) + 1
    await save_db(db)

    summary = (
        f"\n{'▃' * 28}\n"
        f"☠️ *АТАКА ЗАВЕРШЕНА* ☠️\n"
        f"{'▔' * 28}\n"
        f"🎯 Цель: `{target}`\n"
        f"📋 Причина: `{reason_label}`\n"
        f"🔑 ID атаки: `#ATK-{session_id}`\n"
        f"{'─' * 28}\n"
        f"✅ Успешно:    `{success_count}` / `{TOTAL_SESSIONS}`\n"
        f"❌ Провалено:  `{fail_count}` / `{TOTAL_SESSIONS}`\n"
        f"📬 Всего жалоб: `{total_reports}`\n"
        f"📈 Успешность: `{success_rate*100:.1f}%`\n"
        f"{'─' * 28}\n"
        f"🕒 `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
        f"{'▃' * 28}"
    )

    try:
        await msg.edit_text(header_clean + summary, parse_mode="Markdown",
                            reply_markup=attack_result_keyboard())
    except Exception:
        await query.message.reply_html(summary,
                                       reply_markup=attack_result_keyboard())

    context.user_data.clear()
    return ConversationHandler.END


async def check_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if await is_subscribed(context.bot, update.effective_user.id):
        await query.edit_message_text(
            "✅ Спасибо! Теперь вы можете использовать бота.",
            reply_markup=back_keyboard("menu"),
        )
    else:
        buttons = [[InlineKeyboardButton("📢 Подписаться", url=CHANNEL_LINK)]] if CHANNEL_LINK else []
        buttons.append([InlineKeyboardButton("🔄 Я подписался", callback_data="check_sub")])
        buttons.append([InlineKeyboardButton("◀️ В меню", callback_data="menu")])
        await query.edit_message_text(
            "❌ Вы ещё не подписались.\n\n"
            "Подпишитесь на канал и нажмите 'Я подписался'.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def attack_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    db = await load_db()
    admin_flag = is_admin(db, user.id)
    text = (
        f"❄️ *Frosty Warnings* ❄️\n"
        f"👋 Привет, {user.first_name}!\n\n"
        f"⚡️ 368 сессий готовы к атаке.\n"
    )
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=main_menu_keyboard(admin_flag))
    return ConversationHandler.END


# ─────────────────────── ADMIN PANEL ───────────────────────

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db = await load_db()
    if not is_admin(db, update.effective_user.id):
        await query.edit_message_text("🚫 Доступ запрещён.", reply_markup=back_keyboard("menu"))
        return

    text = "🛠 *АДМИН ПАНЕЛЬ*\n\nВыберите раздел:"
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=admin_keyboard())


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db = await load_db()
    users = db["users"]
    total = len(users)
    banned = sum(1 for u in users.values() if u.get("banned"))
    with_sub = sum(1 for u in users.values() if has_subscription(u))
    total_reports = sum(u.get("reports_used", 0) for u in users.values())

    text = (
        f"📊 *Статистика бота*\n\n"
        f"👥 Всего пользователей: `{total}`\n"
        f"✅ С подпиской: `{with_sub}`\n"
        f"🚫 Забанено: `{banned}`\n"
        f"🛡 Админов: `{len(db['admins'])}`\n"
        f"⚔️ Проведено атак: `{total_reports}`\n"
    )
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=back_keyboard("admin_panel"))


async def admin_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db = await load_db()
    data = query.data

    page = 0
    if data.startswith("admin_users_page_"):
        page = int(data.split("_")[-1])

    users = db["users"]
    if not users:
        await query.edit_message_text("Нет зарегистрированных пользователей.",
                                      reply_markup=back_keyboard("admin_panel"))
        return

    text = f"👥 *Пользователи*\n\nВсего: `{len(users)}`\n"
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=users_page_keyboard(users, page))


async def admin_user_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db = await load_db()
    data = query.data

    parts = data.split("_")
    uid_str = parts[2]

    if uid_str not in db["users"]:
        await query.edit_message_text("❌ Пользователь не найден.",
                                      reply_markup=back_keyboard("admin_users_page_0"))
        return

    udata = db["users"][uid_str]
    name_display = f"@{udata['username']}" if udata.get("username") else (udata.get("first_name") or uid_str)
    sub_status = "✅ Активна" if has_subscription(udata) else "❌ Нет"
    sub_exp = ""
    if has_subscription(udata):
        sub = udata["subscription"]
        if sub.get("type") == "lifetime" or not sub.get("expires"):
            sub_exp = "навсегда"
        else:
            sub_exp = f"до {sub.get('expires', 'Н/Д')[:10]}"

    user_is_admin = int(uid_str) in db["admins"]

    text = (
        f"👤 *Пользователь:* {name_display}\n"
        f"ID: `{uid_str}`\n"
        f"Имя: {udata.get('first_name', '')} {udata.get('last_name', '')}\n"
        f"🎫 Подписка: {sub_status} {sub_exp}\n"
        f"{'🚫 Забанен' if is_banned(udata) else '✅ Активен'}\n"
        f"{'🛡 Админ' if user_is_admin else ''}\n"
        f"⚔️ Атак: `{udata.get('reports_used', 0)}`\n"
    )
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=user_management_keyboard(uid_str, udata, user_is_admin))


async def admin_user_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db = await load_db()

    if not is_admin(db, update.effective_user.id):
        return

    data = query.data
    parts = data.split("_")
    uid_str = parts[2]

    if uid_str not in db["users"]:
        await query.edit_message_text("❌ Пользователь не найден.",
                                      reply_markup=back_keyboard("admin_users_page_0"))
        return

    udata = db["users"][uid_str]
    action = parts[3]

    if action == "givesub" and len(parts) == 5:
        days = int(parts[4])
        is_lifetime = days == 0
        sub = {
            "active": True,
            "type": "lifetime" if is_lifetime else f"{days}d",
            "granted_by": update.effective_user.id,
            "granted_at": datetime.now().isoformat(),
        }
        if not is_lifetime:
            sub["expires"] = (datetime.now() + timedelta(days=days)).isoformat()
        db["users"][uid_str]["subscription"] = sub
        await save_db(db)
        label = "навсегда" if is_lifetime else f"на `{days}` дн."
        msg = f"✅ Подписка выдана `{uid_str}` {label}."
        await query.edit_message_text(
            msg, parse_mode="Markdown",
            reply_markup=back_keyboard(f"admin_user_{uid_str}"),
        )
        notify_text = "🎫 Ваша подписка активирована навсегда!" if is_lifetime else (
            f"🎫 Ваша подписка активирована на {days} дней!\n"
            f"📅 Истекает: {sub.get('expires', '')[:10]}"
        )
        try:
            await context.bot.send_message(int(uid_str), notify_text + "\nИспользуйте 🚀 Атаковать в меню.")
        except Exception:
            pass
        return

    if action == "removesub":
        db["users"][uid_str]["subscription"] = None
        await save_db(db)
        await query.edit_message_text(
            f"❌ Подписка отключена у `{uid_str}`.",
            parse_mode="Markdown",
            reply_markup=back_keyboard(f"admin_user_{uid_str}"),
        )
        try:
            await context.bot.send_message(int(uid_str), "❌ Ваша подписка отключена администратором.")
        except Exception:
            pass
        return

    # Ban
    if action == "ban":
        db["users"][uid_str]["banned"] = True
        db["users"][uid_str]["ban_reason"] = "Нарушение правил"
        if db["users"][uid_str].get("subscription"):
            db["users"][uid_str]["subscription"]["active"] = False
        await save_db(db)
        await query.edit_message_text(
            f"🚫 `{uid_str}` забанен.",
            parse_mode="Markdown",
            reply_markup=back_keyboard(f"admin_user_{uid_str}"),
        )
        try:
            await context.bot.send_message(int(uid_str), "🚫 Вы забанены администратором.")
        except Exception:
            pass
        return

    # Unban
    if action == "unban":
        db["users"][uid_str]["banned"] = False
        db["users"][uid_str]["ban_reason"] = None
        await save_db(db)
        await query.edit_message_text(
            f"✅ `{uid_str}` разбанен.",
            parse_mode="Markdown",
            reply_markup=back_keyboard(f"admin_user_{uid_str}"),
        )
        try:
            await context.bot.send_message(int(uid_str), "✅ Вы разбанены.")
        except Exception:
            pass
        return

    # Make admin
    if action == "makeadmin":
        uid = int(uid_str)
        if uid in db["admins"]:
            await query.answer("Уже админ")
            return
        db["admins"].append(uid)
        await save_db(db)
        await query.edit_message_text(
            f"✅ `{uid_str}` назначен администратором.",
            parse_mode="Markdown",
            reply_markup=back_keyboard(f"admin_user_{uid_str}"),
        )
        return

    # Remove admin
    if action == "removeadmin":
        uid = int(uid_str)
        if uid in ADMIN_IDS:
            await query.edit_message_text(
                "❌ Нельзя удалить админа из .env через бота.",
                reply_markup=back_keyboard(f"admin_user_{uid_str}"),
            )
            return
        if uid not in db["admins"]:
            await query.answer("Не админ")
            return
        db["admins"].remove(uid)
        await save_db(db)
        await query.edit_message_text(
            f"❌ `{uid_str}` удалён из администраторов.",
            parse_mode="Markdown",
            reply_markup=back_keyboard(f"admin_user_{uid_str}"),
        )
        return


# ─────────────────────── ADMIN KEYS ───────────────────────

async def admin_keys_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db = await load_db()
    if not is_admin(db, update.effective_user.id):
        return

    text = "🔑 *Управление ключами*\n\nВыберите действие:"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Создать ключи", callback_data="admin_key_create")],
        [InlineKeyboardButton("📋 Список ключей", callback_data="admin_keylist")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")],
    ])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def admin_key_create_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    text = "🔑 *Выберите длительность ключа:*"
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=key_duration_keyboard())


async def admin_key_create_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    days = int(query.data.split("_")[3])
    text = f"🔑 *Сколько ключей по {days} дн. создать?*"
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=key_count_keyboard(days))


async def admin_key_create_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db = await load_db()
    if not is_admin(db, update.effective_user.id):
        return

    parts = query.data.split("_")
    days = int(parts[3])
    count = int(parts[4])
    is_lifetime = days == 0
    dur_label = "навсегда" if is_lifetime else f"{days} дн."

    keys = []
    for _ in range(count):
        key = generate_key()
        while key in db["keys"]:
            key = generate_key()
        db["keys"][key] = {
            "days": days,
            "created_by": update.effective_user.id,
            "created_at": datetime.now().isoformat(),
            "used_by": None,
            "used_at": None,
        }
        keys.append(key)
    await save_db(db)

    if count == 1:
        text = (
            f"🔑 *Ключ создан:*\n\n"
            f"`{keys[0]}`\n\n"
            f"📅 {dur_label}\n"
        )
    else:
        keys_text = "\n".join(f"`{k}`" for k in keys)
        text = (
            f"🔑 *Создано ключей: {count}*\n\n"
            f"{keys_text}\n\n"
            f"📅 {dur_label}\n"
        )
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=back_keyboard("admin_keys"))


async def admin_keylist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db = await load_db()
    keys = db.get("keys", {})
    if not keys:
        await query.edit_message_text("Нет созданных ключей.",
                                      reply_markup=back_keyboard("admin_keys"))
        return

    lines = ["🔑 *Все ключи:*\n"]
    used_count = 0
    unused_count = 0
    for key, kdata in sorted(keys.items(), key=lambda x: x[1].get("created_at", ""), reverse=True):
        used = kdata.get("used_by")
        days_raw = kdata.get("days", 0)
        dur_str = "∞ навсегда" if days_raw == 0 else f"{days_raw}дн"
        status = f"✅ Использован {kdata.get('used_at', '')[:10]}" if used else "❌ Свободен"
        if used:
            used_count += 1
        else:
            unused_count += 1
        lines.append(f"`{key}` — {dur_str} — {status}")
    lines.append(f"\n📊 Всего: `{len(keys)}` | Свободно: `{unused_count}` | Использовано: `{used_count}`")
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                  reply_markup=back_keyboard("admin_keys"))


# ─────────────────────── ADMIN PRICES ───────────────────────

async def admin_prices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db = await load_db()
    prices = db.get("prices", {})
    text = "💲 *Прайс:*\n\n"
    for k, v in prices.items():
        text += f"`{k}` — `${v}`\n"
    text += "\n💳 Оплата: @unnacy"
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=back_keyboard("admin_panel"))


# ─────────────────────── CRYPTO PAY ───────────────────────

PAYLOAD_SEP = "::"


class CryptoPay:
    BASE_URL = "https://pay.crypt.bot/api"

    def __init__(self, token: str):
        self.token = token
        self.headers = {"Crypto-Pay-API-Token": token}
        self._client = httpx.AsyncClient(timeout=30)
        self._closed = False

    async def _request(self, method: str, endpoint: str, data: dict = None) -> dict:
        if self._closed:
            raise RuntimeError("CryptoPay client is closed")
        url = f"{self.BASE_URL}/{endpoint}"
        if method == "GET":
            r = await self._client.get(url, headers=self.headers, params=data)
        else:
            r = await self._client.post(url, headers=self.headers, json=data)
        r.raise_for_status()
        result = r.json()
        if not result.get("ok"):
            raise Exception(f"CryptoPay error: {result.get('error')}")
        return result["result"]

    async def close(self):
        self._closed = True
        await self._client.aclose()

    async def get_me(self) -> dict:
        return await self._request("GET", "getMe")

    async def create_invoice(
        self, asset: str, amount: str, payload: str = None,
        description: str = None, expires_in: int = 3600,
    ) -> dict:
        data = {"asset": asset, "amount": amount}
        if payload:
            data["payload"] = payload
        if description:
            data["description"] = description
        if expires_in:
            data["expires_in"] = expires_in
        return await self._request("POST", "createInvoice", data)

    async def get_invoices(self, invoice_ids: str = None, status: str = None) -> list:
        data = {}
        if invoice_ids:
            data["invoice_ids"] = invoice_ids
        if status:
            data["status"] = status
        return await self._request("GET", "getInvoices", data)


cp = None
if CRYPTO_PAY_TOKEN:
    cp = CryptoPay(CRYPTO_PAY_TOKEN)


PLAN_KEYS = {"1d": 1, "7d": 7, "30d": 30, "forever": 0}


def plan_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for idx, (key, days) in enumerate(PLAN_KEYS.items()):
        label = "∞ Навсегда" if days == 0 else f"{days} дн."
        row.append(InlineKeyboardButton(label, callback_data=f"pay_{key}"))
        if idx % 2 == 1:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="profile")])
    return InlineKeyboardMarkup(buttons)


async def buy_subscription_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    db = await load_db()
    prices = db.get("prices", {})
    price_lines = []
    for key, days in PLAN_KEYS.items():
        label = "Навсегда" if days == 0 else f"{days} день" if days == 1 else f"{days} дней"
        price = prices.get(label, 0)
        price_lines.append(f"`{label}` — `${price}`")
    text = (
        "💳 *Выберите тариф:*\n\n"
        + "\n".join(price_lines)
        + f"\n\n⛓ Оплата в `{CRYPTO_PAY_ASSET}`"
    )
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=plan_keyboard())


async def create_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not cp:
        await query.edit_message_text("❌ Платежи временно недоступны.", reply_markup=back_keyboard("profile"))
        return

    plan_key = query.data.split("_", 1)[1]
    days = PLAN_KEYS.get(plan_key)
    if days is None:
        return

    db = await load_db()
    label = "Навсегда" if days == 0 else f"{days} день" if days == 1 else f"{days} дней"
    price = db["prices"].get(label)
    if not price:
        await query.edit_message_text("❌ Цена не найдена.", reply_markup=back_keyboard("profile"))
        return

    user_id = update.effective_user.id
    payload_str = f"{user_id}{PAYLOAD_SEP}{days}"

    try:
        invoice = await cp.create_invoice(
            asset=CRYPTO_PAY_ASSET,
            amount=str(price),
            payload=payload_str,
            description=f"Frosty Warnings — {label}",
            expires_in=3600,
        )
    except Exception as e:
        logger.error(f"CryptoPay createInvoice error: {e}")
        await query.edit_message_text("❌ Ошибка создания счёта. Попробуйте позже.", reply_markup=back_keyboard("profile"))
        return

    inv_id = invoice["invoice_id"]
    pay_url = invoice.get("bot_invoice_url") or invoice.get("pay_url", "")

    db["pending_payments"][str(inv_id)] = {
        "user_id": user_id,
        "days": days,
        "amount": str(price),
        "asset": CRYPTO_PAY_ASSET,
        "status": "active",
        "created_at": datetime.now().isoformat(),
        "paid_at": None,
    }
    await save_db(db)

    buttons = [
        [InlineKeyboardButton("💳 Оплатить", url=pay_url)],
        [InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"checkpay_{inv_id}")],
        [InlineKeyboardButton("◀️ Назад", callback_data="profile")],
    ]
    await query.edit_message_text(
        f"💳 *Счёт на оплату*\n\n"
        f"📋 Тариф: `{label}`\n"
        f"💰 Сумма: `${price}` (`{CRYPTO_PAY_ASSET}`)\n"
        f"⏳ Счёт действителен 1 час\n\n"
        f"Нажмите *Оплатить*, чтобы перейти к платежу.\n"
        f"После оплаты нажмите *Проверить*.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not cp:
        return

    inv_id = query.data.split("_", 1)[1]
    db = await load_db()
    payment = db["pending_payments"].get(inv_id)
    if not payment:
        await query.edit_message_text("❌ Счёт не найден.", reply_markup=back_keyboard("profile"))
        return
    if payment.get("status") == "paid":
        await query.edit_message_text("✅ Платёж уже обработан! Подписка активна.", reply_markup=back_keyboard("profile"))
        return

    try:
        invoices = await cp.get_invoices(invoice_ids=inv_id)
    except Exception as e:
        logger.error(f"CryptoPay getInvoices error: {e}")
        await query.edit_message_text("❌ Ошибка проверки. Попробуйте позже.", reply_markup=back_keyboard("profile"))
        return

    if not invoices:
        await query.edit_message_text("⏳ Счёт ещё не оплачен.", reply_markup=back_keyboard("profile"))
        return

    invoice = invoices[0]
    if invoice["status"] == "paid":
        await _grant_subscription(db, inv_id, payment)
        await query.edit_message_text(
            "✅ *Оплата получена!*\n\n🎫 Подписка активирована!",
            parse_mode="Markdown",
            reply_markup=back_keyboard("menu"),
        )
    elif invoice["status"] == "expired":
        db["pending_payments"][inv_id]["status"] = "expired"
        await save_db(db)
        await query.edit_message_text("❌ Счёт просрочен. Создайте новый.", reply_markup=back_keyboard("profile"))
    else:
        await query.edit_message_text("⏳ Ожидание оплаты...", reply_markup=back_keyboard("profile"))


async def _grant_subscription(db: dict, inv_id: str, payment: dict) -> None:
    uid = str(payment["user_id"])
    days = payment["days"]
    is_lifetime = days == 0
    sub = {
        "active": True,
        "type": "lifetime" if is_lifetime else f"{days}d",
        "granted_by": "crypto_pay",
        "granted_at": datetime.now().isoformat(),
        "invoice_id": inv_id,
    }
    if not is_lifetime:
        sub["expires"] = (datetime.now() + timedelta(days=days)).isoformat()
    db["users"][uid]["subscription"] = sub
    db["pending_payments"][inv_id]["status"] = "paid"
    db["pending_payments"][inv_id]["paid_at"] = datetime.now().isoformat()
    await save_db(db)


async def background_check_payments(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not cp:
        return
    db = await load_db()
    pending = db.get("pending_payments", {})
    active_ids = [inv_id for inv_id, p in pending.items() if p.get("status") == "active"]
    if not active_ids:
        return

    for inv_id in active_ids:
        payment = pending[inv_id]
        try:
            invoices = await cp.get_invoices(invoice_ids=inv_id)
        except Exception:
            continue
        if not invoices:
            continue
        invoice = invoices[0]
        if invoice["status"] == "paid":
            await _grant_subscription(db, inv_id, payment)
            uid = str(payment["user_id"])
            logger.info(f"Auto-granted subscription (invoice {inv_id}) to user {uid}")
            try:
                await context.bot.send_message(
                    int(uid),
                    "✅ *Оплата получена!*\n🎫 Подписка активирована!\nИспользуйте 🚀 Атаковать в меню.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass


# ─────────────────────── NOOP ───────────────────────

async def noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()


# ─────────────────────── MAIN ───────────────────────



# ─────────────────────── PERIODIC DB FLUSH ───────────────────────

async def periodic_db_flush(context: ContextTypes.DEFAULT_TYPE) -> None:
    await flush_db()
def main() -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не найден в .env файле!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # User handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu$"))
    app.add_handler(CallbackQueryHandler(help_callback, pattern="^help$"))
    app.add_handler(CallbackQueryHandler(profile_callback, pattern="^profile$"))
    app.add_handler(CallbackQueryHandler(activate_key_start, pattern="^activate_key$"))
    app.add_handler(CallbackQueryHandler(check_sub, pattern="^check_sub$"))

    # Attack conversation (MUST be before generic text handler)
    attack_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(attack_start, pattern="^attack$")],
        states={
            TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, attack_target)],
            REASON: [CallbackQueryHandler(attack_reason, pattern="^reason_")],
        },
        fallbacks=[
            CallbackQueryHandler(attack_cancel, pattern="^menu$"),
            CallbackQueryHandler(attack_start, pattern="^attack$"),
        ],
    )
    app.add_handler(attack_conv)

    # Text dispatcher (key activation — must be AFTER ConversationHandler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_dispatcher))

    # Admin panel
    app.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_panel$"))
    app.add_handler(CallbackQueryHandler(admin_stats, pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_users_list, pattern="^admin_users_page_"))
    app.add_handler(CallbackQueryHandler(admin_user_panel, pattern=r"^admin_user_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_user_action, pattern=r"^admin_user_\d+_"))
    app.add_handler(CallbackQueryHandler(admin_keys_menu, pattern="^admin_keys$"))
    app.add_handler(CallbackQueryHandler(admin_keylist, pattern="^admin_keylist$"))
    app.add_handler(CallbackQueryHandler(admin_key_create_duration, pattern="^admin_key_create$"))
    app.add_handler(CallbackQueryHandler(admin_key_create_count, pattern=r"^admin_key_create_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_key_create_execute, pattern=r"^admin_key_create_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_prices, pattern="^admin_prices$"))
    app.add_handler(CallbackQueryHandler(noop, pattern="^noop$"))

    # Crypto Pay handlers
    app.add_handler(CallbackQueryHandler(buy_subscription_menu, pattern="^buy_sub$"))
    app.add_handler(CallbackQueryHandler(create_payment, pattern=r"^pay_"))
    app.add_handler(CallbackQueryHandler(check_payment, pattern=r"^checkpay_"))

    # Background jobs
    job_queue = app.job_queue
    if job_queue:
        # Payment checker (every 30 seconds)
        if cp:
            job_queue.run_repeating(background_check_payments, interval=30, first=10)
        # Periodic DB flush (every 30 seconds as safety net)
        job_queue.run_repeating(periodic_db_flush, interval=30, first=30)

    logger.info("Botnet Simulation Bot запущен. Нажмите Ctrl+C для остановки.")
    logger.info(f"Админы из .env: {ADMIN_IDS}")
    logger.info(f"Crypto Pay: {'включён' if cp else 'не настроен (CRYPTO_PAY_TOKEN пуст)'}")
    # Register shutdown handler to flush DB and close HTTP client
    async def _shutdown(app: Application) -> None:
        await flush_db()
        if cp:
            await cp.close()
    app.post_shutdown = _shutdown

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
