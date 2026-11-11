import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import ErrorEvent, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.markdown import html_decoration as hd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("anonbot")


# ================= CONFIG =================

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise SystemExit("Переменная окружения TOKEN не задана")


def _parse_admins(raw: str) -> set:
    return {int(p) for p in raw.replace(" ", "").split(",") if p}


# Можно переопределить переменными окружения ADMINS="id1,id2" и CHANNEL_ID="-100..."
ADMINS = _parse_admins(os.getenv("ADMINS", "731423086,7955144889"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003682143536"))

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Бот работает только в личных сообщениях — в группах/каналах игнорирует всё
dp.message.filter(F.chat.type == "private")

SPAM_DELAY = 10
FOREVER_SECONDS = 100 * 365 * 24 * 3600
MODE_TTL = 180  # сколько секунд админский "режим ввода" (причина бана/мута, ответ) живёт

# Режимы ввода: admin_id -> (значение, время_истечения)
reply_mode = {}        # admin_id -> (user_id, expires)
ban_reason_mode = {}   # admin_id -> (user_id, expires)
mute_reason_mode = {}  # admin_id -> ((user_id, seconds), expires)
_ALL_MODES = (reply_mode, ban_reason_mode, mute_reason_mode)

# Состояние альбомов (несколько фото одним сообщением): user_id -> (media_group_id, принят_ли)
last_album = {}


def clear_modes(admin_id: int):
    for m in _ALL_MODES:
        m.pop(admin_id, None)


def set_mode(mode: dict, admin_id: int, value):
    """Включает режим для админа, сбрасывая все остальные его режимы."""
    clear_modes(admin_id)
    mode[admin_id] = (value, time.time() + MODE_TTL)


def peek_mode(mode: dict, admin_id: int):
    """Возвращает значение режима, если он ещё не истёк, иначе None."""
    item = mode.get(admin_id)
    if item is None:
        return None
    value, expires = item
    if time.time() > expires:
        mode.pop(admin_id, None)
        return None
    return value


# ================= DATABASE =================

_volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
DB_PATH = os.getenv("DB_PATH") or os.path.join(_volume or "/data", "bot.db")
if os.getenv("RAILWAY_ENVIRONMENT") and not _volume and not os.getenv("DB_PATH"):
    log.warning(
        "Railway Volume не подключён — база будет стираться при каждом деплое! "
        "Создайте Volume и смонтируйте его в /data."
    )
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    joined_at REAL DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS messages(
    user_id INTEGER PRIMARY KEY,
    count INTEGER DEFAULT 0,
    last_message_time REAL DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS daily_activity(
    date TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (date, user_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS banned_users(
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    banned_at REAL,
    reason TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS muted_users(
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    muted_until REAL,
    reason TEXT
)
""")

# Посты, пересланные админам — общее состояние публикации на канал
cursor.execute("""
CREATE TABLE IF NOT EXISTS posts(
    post_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    published INTEGER DEFAULT 0,
    channel_msg_id INTEGER,
    published_by INTEGER,
    published_at REAL,
    deleted INTEGER DEFAULT 0,
    deleted_by INTEGER,
    deleted_at REAL
)
""")

# Связь: у каждого админа своя копия сообщения (свой msg_id в своей ЛС с ботом).
# Нужна, чтобы синхронизировать клавиатуры у всех админов сразу.
cursor.execute("""
CREATE TABLE IF NOT EXISTS post_admin_messages(
    post_id INTEGER NOT NULL,
    admin_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    PRIMARY KEY (post_id, admin_id)
)
""")

# Миграции для старых БД
for migration in [
    "ALTER TABLE users ADD COLUMN joined_at REAL DEFAULT 0",
    "ALTER TABLE banned_users ADD COLUMN reason TEXT",
    "ALTER TABLE muted_users ADD COLUMN reason TEXT",
]:
    try:
        cursor.execute(migration)
        db.commit()
    except Exception:
        pass  # колонка уже существует

db.commit()


# ================= DB FUNCTIONS =================

def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def add_user(user_id, username, first_name):
    cursor.execute(
        "INSERT OR IGNORE INTO users (id, username, first_name, joined_at) VALUES (?, ?, ?, ?)",
        (user_id, username, first_name, time.time())
    )
    cursor.execute(
        "INSERT OR IGNORE INTO messages (user_id, count) VALUES (?, 0)",
        (user_id,)
    )
    db.commit()


def add_message(user_id):
    cursor.execute("UPDATE messages SET count = count + 1 WHERE user_id = ?", (user_id,))
    cursor.execute(
        "INSERT OR IGNORE INTO daily_activity (date, user_id) VALUES (?, ?)",
        (utc_today(), user_id)
    )
    db.commit()


def get_stats():
    cursor.execute("SELECT COUNT(*) FROM users")
    users = cursor.fetchone()[0]
    cursor.execute("SELECT SUM(count) FROM messages")
    msgs = cursor.fetchone()[0] or 0
    return users, msgs


def get_top():
    cursor.execute("""
        SELECT users.username, users.first_name, messages.user_id, messages.count
        FROM messages JOIN users ON users.id = messages.user_id
        ORDER BY messages.count DESC LIMIT 10
    """)
    return cursor.fetchall()


def get_activity_last_days(days: int = 7):
    result = []
    now = datetime.now(timezone.utc)
    for i in range(days - 1, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        cursor.execute("SELECT COUNT(*) FROM daily_activity WHERE date = ?", (d,))
        result.append((d, cursor.fetchone()[0]))
    return result


def get_user_info(target: str):
    if target.isdigit():
        cursor.execute(
            "SELECT u.id, u.username, u.first_name, u.joined_at, COALESCE(m.count,0) "
            "FROM users u LEFT JOIN messages m ON m.user_id = u.id WHERE u.id = ?",
            (int(target),)
        )
    else:
        cursor.execute(
            "SELECT u.id, u.username, u.first_name, u.joined_at, COALESCE(m.count,0) "
            "FROM users u LEFT JOIN messages m ON m.user_id = u.id WHERE u.username = ?",
            (target,)
        )
    return cursor.fetchone()


# ===== БАН =====

def ban_user(user_id, username, reason=None):
    cursor.execute(
        "INSERT OR REPLACE INTO banned_users (user_id, username, banned_at, reason) VALUES (?, ?, ?, ?)",
        (user_id, username, time.time(), reason)
    )
    db.commit()


def unban_user(user_id):
    cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
    db.commit()


def is_banned(user_id):
    cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None


def get_ban_list():
    cursor.execute("SELECT user_id, username, reason FROM banned_users")
    return cursor.fetchall()


def get_ban_reason(user_id):
    cursor.execute("SELECT reason FROM banned_users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else None


# ===== МУТ =====

def mute_user(user_id, username, seconds, reason=None):
    cursor.execute(
        "INSERT OR REPLACE INTO muted_users (user_id, username, muted_until, reason) VALUES (?, ?, ?, ?)",
        (user_id, username, time.time() + seconds, reason)
    )
    db.commit()


def unmute_user(user_id):
    cursor.execute("DELETE FROM muted_users WHERE user_id = ?", (user_id,))
    db.commit()


def get_mute_info(user_id):
    cursor.execute("SELECT muted_until, reason FROM muted_users WHERE user_id = ?", (user_id,))
    return cursor.fetchone()


def get_mute_list():
    now = time.time()
    cursor.execute("SELECT user_id, username, muted_until, reason FROM muted_users WHERE muted_until > ?", (now,))
    return cursor.fetchall()


# ===== ПОСТЫ (публикация / удаление на канале) =====

def create_post(user_id: int) -> int:
    """Создаёт новый пост для входящего сообщения пользователя, возвращает post_id."""
    cursor.execute("INSERT INTO posts (user_id, published) VALUES (?, 0)", (user_id,))
    db.commit()
    return cursor.lastrowid


def save_post_admin_message(post_id: int, admin_id: int, msg_id: int):
    """Запоминает msg_id копии этого поста в личке конкретного админа."""
    cursor.execute(
        "INSERT OR REPLACE INTO post_admin_messages (post_id, admin_id, msg_id) VALUES (?, ?, ?)",
        (post_id, admin_id, msg_id)
    )
    db.commit()


def get_post_admin_message(post_id: int, admin_id: int):
    cursor.execute(
        "SELECT msg_id FROM post_admin_messages WHERE post_id = ? AND admin_id = ?",
        (post_id, admin_id)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def get_post_admin_messages(post_id: int):
    """Все копии поста у всех админов: [(admin_id, msg_id), ...]"""
    cursor.execute(
        "SELECT admin_id, msg_id FROM post_admin_messages WHERE post_id = ?",
        (post_id,)
    )
    return cursor.fetchall()


def get_post(post_id: int):
    cursor.execute(
        "SELECT post_id, user_id, published, channel_msg_id, published_by, deleted "
        "FROM posts WHERE post_id = ?",
        (post_id,)
    )
    return cursor.fetchone()


def try_mark_published(post_id: int, admin_id: int, channel_msg_id: int) -> bool:
    """
    Атомарно помечает пост опубликованным, только если он ещё не опубликован.
    Между execute() и commit() нет await, поэтому это безопасно даже если оба
    админа жмут кнопку почти одновременно — сработает только у одного из них.
    Флаги удаления сбрасываются — пост может быть опубликован повторно.
    """
    cursor.execute(
        "UPDATE posts SET published = 1, deleted = 0, deleted_by = NULL, deleted_at = NULL, "
        "channel_msg_id = ?, published_by = ?, published_at = ? "
        "WHERE post_id = ? AND published = 0",
        (channel_msg_id, admin_id, time.time(), post_id)
    )
    db.commit()
    return cursor.rowcount > 0


def try_mark_deleted(post_id: int, admin_id: int) -> bool:
    """Атомарно помечает пост удалённым, только если он был опубликован."""
    cursor.execute(
        "UPDATE posts SET published = 0, deleted = 1, deleted_by = ?, deleted_at = ? "
        "WHERE post_id = ? AND published = 1",
        (admin_id, time.time(), post_id)
    )
    db.commit()
    return cursor.rowcount > 0


def revert_publish_status(post_id: int):
    """Откат, если пост пометили удалённым, но реальное удаление из канала не удалось."""
    cursor.execute(
        "UPDATE posts SET published = 1, deleted = 0, deleted_by = NULL, deleted_at = NULL "
        "WHERE post_id = ?",
        (post_id,)
    )
    db.commit()


# ===== СПАМ =====

def is_spam(user_id: int) -> bool:
    cursor.execute("SELECT last_message_time FROM messages WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    now = time.time()
    if row and now - row[0] < SPAM_DELAY:
        return True
    cursor.execute("UPDATE messages SET last_message_time = ? WHERE user_id = ?", (now, user_id))
    db.commit()
    return False


# ===== УТИЛИТЫ =====

def parse_mute_duration(arg: str):
    """Парсит '30m', '2h', '1d' -> секунды. Возвращает None если не распознано."""
    if not arg:
        return None
    units = {"m": 60, "h": 3600, "d": 86400}
    unit = arg[-1].lower()
    try:
        if unit in units:
            value = int(arg[:-1]) * units[unit]
        else:
            value = int(arg) * 60
    except ValueError:
        return None
    return value if value > 0 else None


def format_duration(seconds: int) -> str:
    if seconds >= FOREVER_SECONDS - 86400:
        return "навсегда"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч"
    return f"{seconds // 86400} д"


def get_user_display(user_id, username) -> str:
    return f"@{username}" if username else f"ID:{user_id}"


def get_user_count() -> int:
    cursor.execute("SELECT COUNT(*) FROM users")
    return cursor.fetchone()[0]


def build_activity_chart(data: list) -> str:
    if not data:
        return "Нет данных"
    max_val = max(v for _, v in data) or 1
    bar_width = 10
    lines = []
    for date_str, count in data:
        day = date_str[5:]  # MM-DD
        filled = round(count / max_val * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        lines.append(f"{day} {bar} {count}")
    return "\n".join(lines)


# ================= KEYBOARDS =================

def mute_time_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="10 мин",   callback_data=f"mutetime:{user_id}:600"),
                InlineKeyboardButton(text="30 мин",   callback_data=f"mutetime:{user_id}:1800"),
                InlineKeyboardButton(text="1 час",    callback_data=f"mutetime:{user_id}:3600"),
            ],
            [
                InlineKeyboardButton(text="3 часа",   callback_data=f"mutetime:{user_id}:10800"),
                InlineKeyboardButton(text="1 день",   callback_data=f"mutetime:{user_id}:86400"),
                InlineKeyboardButton(text="Навсегда", callback_data=f"mutetime:{user_id}:0"),
            ],
            [
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"mutecancel:{user_id}"),
            ]
        ]
    )


def publish_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(
            text="📢 Опубликовать в канал",
            callback_data=f"publish:{post_id}"
        )]]
    )


def publish_confirm_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, опубликовать", callback_data=f"pubconfirm:{post_id}"),
            InlineKeyboardButton(text="❌ Отмена",           callback_data=f"pubcancel:{post_id}"),
        ]]
    )


def published_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(
            text="🗑 Удалить из канала",
            callback_data=f"delpost:{post_id}"
        )]]
    )


def delete_confirm_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"delconfirm:{post_id}"),
            InlineKeyboardButton(text="❌ Отмена",      callback_data=f"delcancel:{post_id}"),
        ]]
    )


def user_action_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✉️ Ответить", callback_data=f"do_reply:{user_id}"),
                InlineKeyboardButton(text="🚫 Бан",      callback_data=f"do_ban:{user_id}"),
            ],
            [
                InlineKeyboardButton(text="🔇 Мут",      callback_data=f"do_mute:{user_id}"),
                InlineKeyboardButton(text="✅ Разбан",    callback_data=f"do_unban:{user_id}"),
            ]
        ]
    )


def admin_main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
                InlineKeyboardButton(text="🏆 Топ",        callback_data="admin_top"),
            ],
            [
                InlineKeyboardButton(text="📈 График",      callback_data="admin_chart"),
                InlineKeyboardButton(text="🔍 Поиск юзера", callback_data="admin_search"),
            ],
            [
                InlineKeyboardButton(text="🚫 Бан-лист",    callback_data="admin_bans"),
                InlineKeyboardButton(text="🔇 Мут-лист",    callback_data="admin_mutes"),
            ]
        ]
    )


def back_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]]
    )


# ================= ERRORS =================

@dp.errors()
async def on_error(event: ErrorEvent):
    exc = event.exception
    # Двойной клик по кнопке и т.п. — не ошибка
    if isinstance(exc, TelegramBadRequest) and "message is not modified" in str(exc):
        return True
    log.error("Необработанная ошибка в хендлере: %r", exc, exc_info=exc)
    cb = event.update.callback_query
    if cb:
        try:
            await cb.answer("⚠️ Произошла ошибка", show_alert=True)
        except Exception:
            pass
    return True


# ================= HELPERS =================

async def _do_ban(user_id: int, reason, reply_to: Message):
    if user_id in ADMINS:
        await reply_to.answer("❌ Нельзя забанить администратора.")
        return
    cursor.execute("SELECT username FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    username = row[0] if row else None
    ban_user(user_id, username, reason)
    label = get_user_display(user_id, username)
    r_text = f"\nПричина: {hd.quote(reason)}" if reason else ""
    await reply_to.answer(f"🚫 {label} забанен.{r_text}", parse_mode="HTML")
    try:
        notify = "🚫 Вы были <b>забанены</b>."
        if reason:
            notify += f"\nПричина: <i>{hd.quote(reason)}</i>"
        await bot.send_message(user_id, notify, parse_mode="HTML")
    except Exception as e:
        log.info("Не удалось уведомить %s о бане: %s", user_id, e)


async def _do_mute(user_id: int, seconds: int, reason, reply_to: Message):
    if user_id in ADMINS:
        await reply_to.answer("❌ Нельзя замутить администратора.")
        return
    cursor.execute("SELECT username FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    username = row[0] if row else None
    forever = seconds == 0
    actual_seconds = FOREVER_SECONDS if forever else seconds
    mute_user(user_id, username, actual_seconds, reason)
    label_time = "навсегда" if forever else format_duration(seconds)
    label = get_user_display(user_id, username)
    r_text = f"\nПричина: {hd.quote(reason)}" if reason else ""
    await reply_to.answer(f"🔇 {label} замучен на {label_time}.{r_text}", parse_mode="HTML")
    try:
        notify = f"🔇 Вы были <b>замучены</b> на <b>{label_time}</b>."
        if reason:
            notify += f"\nПричина: <i>{hd.quote(reason)}</i>"
        await bot.send_message(user_id, notify, parse_mode="HTML")
    except Exception as e:
        log.info("Не удалось уведомить %s о муте: %s", user_id, e)


# ================= CALLBACKS: ОТВЕТ =================

@dp.callback_query(F.data.startswith("do_reply:"))
async def reply_button(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    user_id = int(callback.data.split(":")[1])
    set_mode(reply_mode, callback.from_user.id, user_id)
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить ответ", callback_data="reply_cancel")]]
    )
    await callback.message.answer(
        f"✏️ Напишите ответ пользователю (режим активен {MODE_TTL // 60} мин):",
        reply_markup=cancel_kb
    )
    await callback.answer()


@dp.callback_query(F.data == "reply_cancel")
async def reply_cancel(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    reply_mode.pop(callback.from_user.id, None)
    await callback.message.edit_text("↩️ Ответ отменён.")
    await callback.answer()


# ================= CALLBACKS: БАН =================

@dp.callback_query(F.data.startswith("do_ban:"))
async def ban_button(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    user_id = int(callback.data.split(":")[1])
    if user_id in ADMINS:
        return await callback.answer("❌ Нельзя забанить администратора", show_alert=True)
    set_mode(ban_reason_mode, callback.from_user.id, user_id)
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(
            text="❌ Забанить без причины", callback_data=f"banconfirm:{user_id}:"
        )]]
    )
    await callback.message.answer(
        f"✍️ Напишите причину бана (режим активен {MODE_TTL // 60} мин) или нажмите кнопку:",
        reply_markup=cancel_kb
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("banconfirm:"))
async def ban_confirm_no_reason(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    # формат: banconfirm:{user_id}:{reason}
    parts = callback.data.split(":", 2)
    user_id = int(parts[1])
    reason = parts[2] if len(parts) > 2 and parts[2] else None
    clear_modes(callback.from_user.id)
    await _do_ban(user_id, reason, callback.message)
    await callback.answer()


@dp.callback_query(F.data.startswith("do_unban:"))
async def unban_button(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    user_id = int(callback.data.split(":")[1])
    unban_user(user_id)
    await callback.answer("✅ Пользователь разбанен")
    try:
        await bot.send_message(user_id, "✅ Вы были <b>разбанены</b>. Можете снова писать.", parse_mode="HTML")
    except Exception as e:
        log.info("Не удалось уведомить %s о разбане: %s", user_id, e)


# ================= CALLBACKS: МУТ =================

@dp.callback_query(F.data.startswith("do_mute:"))
async def mute_button(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    user_id = int(callback.data.split(":")[1])
    if user_id in ADMINS:
        return await callback.answer("❌ Нельзя замутить администратора", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=mute_time_keyboard(user_id))
    await callback.answer("Выберите время мута")


@dp.callback_query(F.data.startswith("mutetime:"))
async def mute_time_selected(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    # формат: mutetime:{user_id}:{seconds}
    parts = callback.data.split(":")
    user_id = int(parts[1])
    seconds = int(parts[2])
    if user_id in ADMINS:
        return await callback.answer("❌ Нельзя замутить администратора", show_alert=True)
    set_mode(mute_reason_mode, callback.from_user.id, (user_id, seconds))
    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(
            text="❌ Замутить без причины",
            callback_data=f"muteconfirm:{user_id}:{seconds}:"
        )]]
    )
    # Сначала восстанавливаем кнопки карточки, потом отправляем запрос причины
    await callback.message.edit_reply_markup(reply_markup=user_action_keyboard(user_id))
    await callback.message.answer(
        f"✍️ Напишите причину мута (режим активен {MODE_TTL // 60} мин) или нажмите кнопку:",
        reply_markup=cancel_kb
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("muteconfirm:"))
async def mute_confirm_no_reason(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    # формат: muteconfirm:{user_id}:{seconds}:{reason}
    parts = callback.data.split(":", 3)
    user_id = int(parts[1])
    seconds = int(parts[2])
    reason = parts[3] if len(parts) > 3 and parts[3] else None
    clear_modes(callback.from_user.id)
    await _do_mute(user_id, seconds, reason, callback.message)
    await callback.answer()


@dp.callback_query(F.data.startswith("mutecancel:"))
async def mute_cancel(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    user_id = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=user_action_keyboard(user_id))
    await callback.answer("Отменено")


# ================= CALLBACKS: ПУБЛИКАЦИЯ / УДАЛЕНИЕ =================
# Состояние поста (опубликован/удалён) хранится в БД по post_id и общее для
# всех админов. sync_post_keyboards обновляет клавиатуру сразу у всех копий
# сообщения, поэтому если один админ опубликовал (или удалил) пост — у второго
# кнопка меняется автоматически и повторно нажать "Опубликовать" нельзя.

async def sync_post_keyboards(post_id: int, keyboard: InlineKeyboardMarkup):
    for admin_id, msg_id in get_post_admin_messages(post_id):
        try:
            await bot.edit_message_reply_markup(
                chat_id=admin_id, message_id=msg_id, reply_markup=keyboard
            )
        except Exception as e:
            log.debug("sync_post_keyboards %s/%s: %s", admin_id, msg_id, e)


@dp.callback_query(F.data.startswith("publish:"))
async def publish_button(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    post_id = int(callback.data.split(":")[1])
    post = get_post(post_id)
    if not post:
        return await callback.answer("❌ Пост не найден", show_alert=True)
    if post[2]:  # published
        await callback.answer("⚠️ Уже опубликовано другим админом", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=published_keyboard(post_id))
        return
    await callback.message.edit_reply_markup(reply_markup=publish_confirm_keyboard(post_id))
    await callback.answer("Подтвердите публикацию")


@dp.callback_query(F.data.startswith("pubconfirm:"))
async def publish_confirm(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    post_id = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id

    msg_id = get_post_admin_message(post_id, admin_id)
    if msg_id is None:
        return await callback.answer("❌ Не найдена копия сообщения", show_alert=True)

    try:
        sent = await bot.copy_message(chat_id=CHANNEL_ID, from_chat_id=admin_id, message_id=msg_id)
    except Exception as e:
        log.warning("Публикация поста %s не удалась: %s", post_id, e)
        return await callback.answer(f"❌ Ошибка: {e}", show_alert=True)

    if not try_mark_published(post_id, admin_id, sent.message_id):
        # Второй админ успел опубликовать раньше — убираем свой дубль из канала
        try:
            await bot.delete_message(CHANNEL_ID, sent.message_id)
        except Exception as e:
            log.warning("Не удалось убрать дубль поста %s из канала: %s", post_id, e)
        await callback.answer("⚠️ Уже опубликовано другим админом", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=published_keyboard(post_id))
        return

    await callback.answer("✅ Опубликовано в канал!", show_alert=True)
    await sync_post_keyboards(post_id, published_keyboard(post_id))


@dp.callback_query(F.data.startswith("pubcancel:"))
async def publish_cancel(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    post_id = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=publish_keyboard(post_id))
    await callback.answer("Отменено")


@dp.callback_query(F.data.startswith("delpost:"))
async def delete_post_button(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    post_id = int(callback.data.split(":")[1])
    post = get_post(post_id)
    if not post or not post[2]:  # не опубликован (уже удалили / не публиковали)
        await callback.answer("⚠️ Уже удалено (или ещё не публиковалось)", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=publish_keyboard(post_id))
        return
    await callback.message.edit_reply_markup(reply_markup=delete_confirm_keyboard(post_id))
    await callback.answer("Подтвердите удаление")


@dp.callback_query(F.data.startswith("delconfirm:"))
async def delete_confirm(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    post_id = int(callback.data.split(":")[1])
    admin_id = callback.from_user.id

    post = get_post(post_id)
    if not post or not post[2]:
        await callback.answer("⚠️ Уже удалено другим админом", show_alert=True)
        await sync_post_keyboards(post_id, publish_keyboard(post_id))
        return

    channel_msg_id = post[3]
    if not try_mark_deleted(post_id, admin_id):
        await callback.answer("⚠️ Уже удалено другим админом", show_alert=True)
        await sync_post_keyboards(post_id, publish_keyboard(post_id))
        return

    try:
        await bot.delete_message(CHANNEL_ID, channel_msg_id)
    except Exception as e:
        # Реально удалить не получилось — откатываем статус, чтобы кнопка не "залипла"
        log.warning("Удаление поста %s из канала не удалось: %s", post_id, e)
        revert_publish_status(post_id)
        await callback.answer(f"❌ Ошибка удаления: {e}", show_alert=True)
        await sync_post_keyboards(post_id, published_keyboard(post_id))
        return

    await callback.answer("🗑 Удалено из канала", show_alert=True)
    await sync_post_keyboards(post_id, publish_keyboard(post_id))


@dp.callback_query(F.data.startswith("delcancel:"))
async def delete_cancel(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    post_id = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=published_keyboard(post_id))
    await callback.answer("Отменено")


# ================= ADMIN PANEL =================

@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id not in ADMINS:
        return
    await message.answer("⚙️ Админ панель", reply_markup=admin_main_keyboard())


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    users, msgs = get_stats()
    text = f"📊 <b>Статистика</b>\n\n👤 Пользователей: {users}\n✉️ Сообщений: {msgs}"
    await callback.message.edit_text(text, reply_markup=back_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_top")
async def admin_top(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    data = get_top()
    text = "🏆 <b>Топ пользователей:</b>\n\n"
    for i, (username, first_name, user_id, count) in enumerate(data, 1):
        name = f"@{username}" if username else (first_name or f"ID:{user_id}")
        text += f"{i}. {hd.quote(name)} — {count} сообщений\n"
    if not data:
        text += "Пока никого нет."
    await callback.message.edit_text(text, reply_markup=back_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_chart")
async def admin_chart(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    data = get_activity_last_days(7)
    chart = build_activity_chart(data)
    total = sum(v for _, v in data)
    text = (
        f"📈 <b>Активность за 7 дней</b>\n"
        f"(уникальных юзеров в день)\n\n"
        f"<code>{chart}</code>\n\n"
        f"Итого за неделю: <b>{total}</b> чел."
    )
    await callback.message.edit_text(text, reply_markup=back_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_search")
async def admin_search_prompt(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    await callback.message.edit_text(
        "🔍 <b>Поиск пользователя</b>\n\n"
        "Введите команду:\n"
        "<code>/user @username</code>\n"
        "<code>/user 123456789</code>",
        reply_markup=back_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_bans")
async def admin_bans(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    bans = get_ban_list()
    if not bans:
        text = "🚫 Бан-лист пуст."
    else:
        text = "🚫 <b>Бан-лист:</b>\n\n"
        for user_id, username, reason in bans:
            r = f" — <i>{hd.quote(reason)}</i>" if reason else ""
            text += f"• {get_user_display(user_id, username)}{r}\n"
    await callback.message.edit_text(text, reply_markup=back_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_mutes")
async def admin_mutes(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    mutes = get_mute_list()
    if not mutes:
        text = "🔇 Мут-лист пуст."
    else:
        text = "🔇 <b>Пользователи в муте:</b>\n\n"
        for user_id, username, muted_until, reason in mutes:
            remaining = int(muted_until - time.time())
            r = f" — <i>{hd.quote(reason)}</i>" if reason else ""
            text += f"• {get_user_display(user_id, username)} — ещё {format_duration(remaining)}{r}\n"
    await callback.message.edit_text(text, reply_markup=back_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    await callback.message.edit_text("⚙️ Админ панель", reply_markup=admin_main_keyboard())
    await callback.answer()


# ================= /user — ПОИСК =================

@dp.message(Command("user"))
async def cmd_user(message: Message):
    if message.from_user.id not in ADMINS:
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Использование: /user @username или /user <user_id>")
    target = args[1].strip().lstrip("@")
    row = get_user_info(target)
    if not row:
        return await message.answer("❌ Пользователь не найден в базе.")

    user_id, username, first_name, joined_at, msg_count = row
    profile_link = f'<a href="tg://user?id={user_id}">{user_id}</a>'
    uname = f"@{username}" if username else "—"
    joined = (
        datetime.fromtimestamp(joined_at, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        if joined_at else "неизвестно"
    )

    if is_banned(user_id):
        reason = get_ban_reason(user_id)
        status = "🚫 Забанен" + (f" (<i>{hd.quote(reason)}</i>)" if reason else "")
    else:
        mute_row = get_mute_info(user_id)
        if mute_row and time.time() < mute_row[0]:
            remaining = format_duration(int(mute_row[0] - time.time()))
            r = mute_row[1]
            status = f"🔇 В муте ещё {remaining}" + (f" (<i>{hd.quote(r)}</i>)" if r else "")
        else:
            status = "✅ Активен"

    data = get_activity_last_days(7)
    chart = build_activity_chart(data)

    text = (
        f"🔍 <b>Профиль</b>\n\n"
        f"ID: {profile_link}\n"
        f"Username: {uname}\n"
        f"Имя: {hd.quote(first_name or '—')}\n"
        f"Зарегистрирован: {joined}\n"
        f"Сообщений: {msg_count}\n"
        f"Статус: {status}\n\n"
        f"📈 <b>Активность бота (7 дней):</b>\n"
        f"<code>{chart}</code>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🚫 Бан",      callback_data=f"do_ban:{user_id}"),
                InlineKeyboardButton(text="🔇 Мут",      callback_data=f"do_mute:{user_id}"),
            ],
            [
                InlineKeyboardButton(text="✅ Разбан",    callback_data=f"do_unban:{user_id}"),
                InlineKeyboardButton(text="✉️ Ответить",  callback_data=f"do_reply:{user_id}"),
            ]
        ]
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ================= START =================

@dp.message(Command("start"))
async def start(message: Message):
    uid = message.from_user.id
    if is_banned(uid):
        reason = get_ban_reason(uid)
        text = "🚫 Вы забанены."
        if reason:
            text += f"\nПричина: <i>{hd.quote(reason)}</i>"
        return await message.answer(text, parse_mode="HTML")

    mute_row = get_mute_info(uid)
    if mute_row and time.time() < mute_row[0]:
        remaining = format_duration(int(mute_row[0] - time.time()))
        reason = mute_row[1]
        text = f"🔇 Вы в муте. Осталось: <b>{remaining}</b>."
        if reason:
            text += f"\nПричина: <i>{hd.quote(reason)}</i>"
        return await message.answer(text, parse_mode="HTML")

    user_count = get_user_count()
    await message.answer(
        f"👋 Привет!\n\n"
        f"Отправь своё сообщение — мы ответим в ближайшее время.\n"
        f"Это полностью анонимно 🎭\n\n"
        f"👥 Нас уже <b>{user_count}</b> пользователей",
        parse_mode="HTML"
    )


# ================= ADMIN КОМАНДЫ =================

@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    if message.from_user.id not in ADMINS:
        return
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        return await message.answer("❌ Использование: /ban @username [причина]")
    target = args[1].strip().lstrip("@")
    reason = args[2].strip() if len(args) > 2 else None
    if target.isdigit():
        cursor.execute("SELECT id, username FROM users WHERE id = ?", (int(target),))
    else:
        cursor.execute("SELECT id, username FROM users WHERE username = ?", (target,))
    row = cursor.fetchone()
    if not row:
        return await message.answer("❌ Пользователь не найден в базе.")
    user_id, username = row
    await _do_ban(user_id, reason, message)  # проверка на админа внутри


@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    if message.from_user.id not in ADMINS:
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Использование: /unban @username или /unban <user_id>")
    target = args[1].strip().lstrip("@")
    if target.isdigit():
        cursor.execute("SELECT user_id, username FROM banned_users WHERE user_id = ?", (int(target),))
    else:
        cursor.execute("SELECT user_id, username FROM banned_users WHERE username = ?", (target,))
    row = cursor.fetchone()
    if not row:
        return await message.answer("❌ Пользователь не найден в бан-листе.")
    user_id, username = row
    unban_user(user_id)
    await message.answer(f"✅ {get_user_display(user_id, username)} разбанен.")
    try:
        await bot.send_message(user_id, "✅ Вы были <b>разбанены</b>. Можете снова писать.", parse_mode="HTML")
    except Exception as e:
        log.info("Не удалось уведомить %s о разбане: %s", user_id, e)


@dp.message(Command("mute"))
async def cmd_mute(message: Message):
    if message.from_user.id not in ADMINS:
        return
    args = message.text.split(maxsplit=3)
    if len(args) < 2:
        return await message.answer(
            "❌ Использование: /mute @username [время] [причина]\n"
            "Примеры: /mute @user 30m спам | /mute @user 2h | /mute @user спам"
        )
    target = args[1].strip().lstrip("@")

    # Если второй аргумент не является временем — считаем его причиной, мут на 1ч
    duration_arg = args[2].strip() if len(args) > 2 else None
    seconds = parse_mute_duration(duration_arg) if duration_arg else 3600
    if seconds is None:
        reason = " ".join(args[2:]).strip() if len(args) > 2 else None
        seconds = 3600
    else:
        reason = args[3].strip() if len(args) > 3 else None

    if target.isdigit():
        cursor.execute("SELECT id, username FROM users WHERE id = ?", (int(target),))
    else:
        cursor.execute("SELECT id, username FROM users WHERE username = ?", (target,))
    row = cursor.fetchone()
    if not row:
        return await message.answer("❌ Пользователь не найден в базе.")
    user_id, username = row
    await _do_mute(user_id, seconds, reason, message)  # проверка на админа внутри


@dp.message(Command("unmute"))
async def cmd_unmute(message: Message):
    if message.from_user.id not in ADMINS:
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("❌ Использование: /unmute @username или /unmute <user_id>")
    target = args[1].strip().lstrip("@")
    if target.isdigit():
        cursor.execute("SELECT user_id, username FROM muted_users WHERE user_id = ?", (int(target),))
    else:
        cursor.execute("SELECT user_id, username FROM muted_users WHERE username = ?", (target,))
    row = cursor.fetchone()
    if not row:
        return await message.answer("❌ Пользователь не найден в мут-листе.")
    user_id, username = row
    unmute_user(user_id)
    await message.answer(f"✅ {get_user_display(user_id, username)} размучен.")
    try:
        await bot.send_message(user_id, "✅ Ваш мут снят. Можете снова писать.", parse_mode="HTML")
    except Exception as e:
        log.info("Не удалось уведомить %s о снятии мута: %s", user_id, e)


# ================= COPY HELPER =================
# copy_message сохраняет форматирование (entities), подписи и поддерживает ВСЕ типы
# сообщений (фото, видео, кружки, опросы, геолокацию, стикеры и т.д.).

async def copy_content(message: Message, target_id: int, reply_markup=None):
    """Копирует сообщение в чат target_id. Возвращает MessageId или None при ошибке."""
    try:
        return await bot.copy_message(
            chat_id=target_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=reply_markup,
        )
    except Exception as e:
        log.warning("copy_message -> %s не удалось: %s", target_id, e)
        return None


async def notify_admins_about_reply(sender_admin_id: int, target_user_id: int, message: Message):
    """Показывает остальным админам, что кто-то уже ответил пользователю — и что именно."""
    cursor.execute("SELECT username FROM users WHERE id = ?", (target_user_id,))
    row = cursor.fetchone()
    target_label = get_user_display(target_user_id, row[0] if row else None)

    admin_user = message.from_user
    admin_label = f"@{admin_user.username}" if admin_user.username else (admin_user.first_name or f"ID:{sender_admin_id}")

    note = f"✉️ <b>{hd.quote(admin_label)}</b> ответил(а) пользователю {target_label}:"

    for other_admin in ADMINS:
        if other_admin == sender_admin_id:
            continue
        try:
            await bot.send_message(other_admin, note, parse_mode="HTML")
            await copy_content(message, other_admin)
        except Exception as e:
            log.warning("Ошибка уведомления админа %s об ответе: %s", other_admin, e)


# ================= ALL MESSAGES =================

@dp.message()
async def all_messages(message: Message):
    user = message.from_user
    if user is None:
        return

    if user.id in ADMINS:
        # ===== ADMIN: ввод причины бана =====
        target_id = peek_mode(ban_reason_mode, user.id)
        if target_id is not None:
            if not message.text:
                return await message.answer("✍️ Причину нужно написать текстом (или нажмите кнопку).")
            ban_reason_mode.pop(user.id, None)
            await _do_ban(target_id, message.text.strip() or None, message)
            return

        # ===== ADMIN: ввод причины мута =====
        mute_target = peek_mode(mute_reason_mode, user.id)
        if mute_target is not None:
            if not message.text:
                return await message.answer("✍️ Причину нужно написать текстом (или нажмите кнопку).")
            mute_reason_mode.pop(user.id, None)
            target_id, seconds = mute_target
            await _do_mute(target_id, seconds, message.text.strip() or None, message)
            return

        # ===== ADMIN: режим ответа =====
        target = peek_mode(reply_mode, user.id)
        if target is None:
            return
        reply_mode.pop(user.id, None)
        sent = await copy_content(message, target)
        if sent is None:
            await message.answer("❌ Не удалось отправить (возможно, пользователь заблокировал бота).")
            return
        await message.answer("✅ Ответ отправлен")
        await notify_admins_about_reply(user.id, target, message)
        return

    # ===== USER =====
    if is_banned(user.id):
        reason = get_ban_reason(user.id)
        text = "🚫 Вы забанены."
        if reason:
            text += f"\nПричина: <i>{hd.quote(reason)}</i>"
        return await message.answer(text, parse_mode="HTML")

    mute_row = get_mute_info(user.id)
    if mute_row and time.time() < mute_row[0]:
        remaining = format_duration(int(mute_row[0] - time.time()))
        reason = mute_row[1]
        text = f"🔇 Вы в муте. Осталось: <b>{remaining}</b>."
        if reason:
            text += f"\nПричина: <i>{hd.quote(reason)}</i>"
        return await message.answer(text, parse_mode="HTML")
    elif mute_row:
        unmute_user(user.id)

    add_user(user.id, user.username, user.first_name)

    # --- Антиспам с поддержкой альбомов ---
    # Все фото одного альбома приходят отдельными сообщениями с одним media_group_id.
    # Первое проверяем на спам как обычно, остальные пропускаем (или молча игнорируем,
    # если первое было отклонено).
    group_id = message.media_group_id
    album_state = last_album.get(user.id) if group_id else None
    is_followup = album_state is not None and album_state[0] == group_id

    if is_followup:
        if not album_state[1]:
            return  # альбом уже отклонён как спам — не засоряем чат повторными ответами
    else:
        if is_spam(user.id):
            if group_id:
                last_album[user.id] = (group_id, False)
            return await message.answer(f"⏱ Не спамь. Подожди {SPAM_DELAY} секунд.")
        if group_id:
            last_album[user.id] = (group_id, True)
        add_message(user.id)

    profile_link = f'<a href="tg://user?id={user.id}">{user.id}</a>'
    username_line = f"@{user.username}" if user.username else "—"
    name_line = hd.quote(user.first_name or "—")

    user_card = (
        f"👤 <b>Новое сообщение</b>\n\n"
        f"ID: {profile_link}\n"
        f"Username: {username_line}\n"
        f"Имя: {name_line}"
    )

    keyboard = user_action_keyboard(user.id)

    post_id = create_post(user.id)
    delivered = False

    for admin in ADMINS:
        sent = await copy_content(message, admin, reply_markup=publish_keyboard(post_id))
        if sent:
            save_post_admin_message(post_id, admin, sent.message_id)
            delivered = True
        # Карточку с кнопками бан/мут/ответ шлём один раз на альбом
        if not is_followup:
            try:
                await bot.send_message(admin, user_card, reply_markup=keyboard, parse_mode="HTML")
                delivered = True
            except Exception as e:
                log.warning("Не удалось отправить карточку админу %s: %s", admin, e)

    if is_followup:
        return  # на каждое фото из альбома не отвечаем

    if delivered:
        await message.answer("✅ Сообщение отправлено анонимно.")
    else:
        await message.answer("❌ Не удалось отправить сообщение. Попробуйте позже.")


# ================= RUN =================

async def main():
    # На случай, если раньше был выставлен вебхук — иначе polling не заработает.
    # Накопившиеся апдейты НЕ сбрасываем, чтобы не потерять сообщения при редеплое.
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Бот запущен. Админы: %s, БД: %s", sorted(ADMINS), DB_PATH)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
