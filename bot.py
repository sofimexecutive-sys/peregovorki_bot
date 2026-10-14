#!/usr/bin/env python3
import logging
import os
import sqlite3
import time
import csv
import io
from datetime import datetime, date, timedelta

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Chat,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------- ЛОГИ ----------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# HTTP-логи Telegram API содержат BOT_TOKEN в URL; не пишем их на уровне INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------- КОНСТАНТЫ ----------------------

# Состояния диалога бронирования
(
    BOOK_ROOM,
    BOOK_DATE,
    BOOK_START,
    BOOK_END,
    BOOK_TOPIC,
    BOOK_NAME,
    BOOK_CONTACT,
    BOOK_CONFIRM,
) = range(8)

# Состояния диалога просмотра занятости
(BUSY_ROOM, BUSY_DATE) = range(8, 10)

ROOMS = {
    "ROOM3": "3 этаж",
    "ROOM4": "4 этаж",
}

WORK_START_HOUR = 6
WORK_END_HOUR = 24  # условно до полуночи
MIN_DURATION_MINUTES = 10
PLANNING_DAYS = 120

# Админ-блокировка
(
    ADMIN_BLOCK_ROOM,
    ADMIN_BLOCK_DATE,
    ADMIN_BLOCK_START,
    ADMIN_BLOCK_END,
    ADMIN_BLOCK_REASON,
) = range(20, 25)

BACK_WORDS = {"назад", "⬅️ назад", "назад ⬅️", "/back"}

# Глобальные объекты
# Глобальные объекты
ADMIN_IDS = set()
GROUP_CHAT_ID = None

# Путь к базе брони
# По умолчанию — файл рядом с ботом, но в проде задаём через переменную окружения DB_PATH
DB_PATH = os.getenv("DB_PATH", "/_data/bookings.sqlite3")

DB = None

BACK_HINT = (
    "\n\nНа любом шаге бронирования можно написать «Назад», "
    "чтобы вернуться на предыдущий шаг."
)
# ---------------------- ХЕЛПЕРЫ ПО ВРЕМЕНИ ----------------------
def now() -> datetime:
    """Текущие дата/время (на сервере)."""
    return datetime.now()


def parse_date(text: str) -> date | None:
    t = text.strip().lower()
    if t in ("сегодня", "today"):
        return date.today()
    if t in ("завтра", "tomorrow"):
        return date.today() + timedelta(days=1)
    try:
       return datetime.strptime(text.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def parse_time(text: str) -> tuple[int, int] | None:
    try:
        dt = datetime.strptime(text.strip(), "%H:%M")
        return dt.hour, dt.minute
    except ValueError:
        return None


def combine_date_time(d: date, h: int, m: int) -> datetime:
    return datetime(d.year, d.month, d.day, h, m)


def dt_to_ts(dt: datetime) -> int:
    return int(dt.timestamp())


def ts_to_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts)


def format_dt_range(start_ts: int, end_ts: int) -> str:
    s = ts_to_dt(start_ts)
    e = ts_to_dt(end_ts)
    return f"{s.strftime('%d.%m.%Y %H:%M')}–{e.strftime('%H:%M')}"


def format_time_range(start_ts: int, end_ts: int) -> str:
    s = ts_to_dt(start_ts)
    e = ts_to_dt(end_ts)
    return f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}"


# ---------------------- РАБОТА С БД ----------------------
class BookingStorage:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_db()

    def init_db(self):
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room TEXT NOT NULL,
                start_ts INTEGER NOT NULL,
                end_ts INTEGER NOT NULL,
                user_id INTEGER,
                user_full_name TEXT,
                user_contact TEXT,
                topic TEXT,
                is_block INTEGER DEFAULT 0,
                block_reason TEXT,
                canceled INTEGER DEFAULT 0,
                canceled_at INTEGER,
                created_at INTEGER NOT NULL
            )
        """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_room_start ON bookings(room, start_ts)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_start ON bookings(user_id, start_ts)"
        )
        self.conn.commit()

    # ---- CRUD ----
    def create_booking(
        self,
        room: str,
        start_dt: datetime,
        end_dt: datetime,
        user_id: int | None,
        user_full_name: str | None,
        user_contact: str | None,
        topic: str | None,
        is_block: bool = False,
        block_reason: str | None = None,
    ) -> int:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO bookings
            (room, start_ts, end_ts, user_id, user_full_name, user_contact,
             topic, is_block, block_reason, canceled, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
            (
                room,
                dt_to_ts(start_dt),
                dt_to_ts(end_dt),
                user_id,
                user_full_name,
                user_contact,
                topic,
                1 if is_block else 0,
                block_reason,
                int(time.time()),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def cancel_booking(self, booking_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE bookings SET canceled = 1, canceled_at = ? WHERE id = ?",
            (int(time.time()), booking_id),
        )
        self.conn.commit()

    def get_booking(self, booking_id: int):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        return cur.fetchone()

    def get_user_future_bookings(self, user_id: int):
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT * FROM bookings
            WHERE user_id = ? AND canceled = 0 AND is_block = 0 AND start_ts >= ?
            ORDER BY start_ts
        """,
            (user_id, int(time.time())),
        )
        return cur.fetchall()

    def get_future_bookings(self):
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT * FROM bookings
            WHERE canceled = 0 AND start_ts >= ?
        """,
            (int(time.time()),),
        )
        return cur.fetchall()

    def get_bookings_for_day(self, room: str | None, d: date):
        start = dt_to_ts(datetime(d.year, d.month, d.day, 0, 0))
        end = dt_to_ts(datetime(d.year, d.month, d.day, 23, 59))
        cur = self.conn.cursor()
        if room:
            cur.execute(
                """
                SELECT * FROM bookings
                WHERE room = ? AND canceled = 0
                  AND start_ts <= ? AND end_ts >= ?
                ORDER BY start_ts
            """,
                (room, end, start),
            )
        else:
            cur.execute(
                """
                SELECT * FROM bookings
                WHERE canceled = 0
                  AND start_ts <= ? AND end_ts >= ?
                ORDER BY room, start_ts
            """,
                (end, start),
            )
        return cur.fetchall()

    def get_bookings_for_day_all(self, d: date):
        """Для админа — все брони за день."""
        return self.get_bookings_for_day(None, d)

    def get_bookings_for_range(self, start_ts: int, end_ts: int):
        """Все брони и блокировки в заданном диапазоне [start_ts, end_ts]."""
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT * FROM bookings
            WHERE canceled = 0
              AND start_ts >= ?
              AND start_ts <= ?
            ORDER BY start_ts, room
            """,
            (start_ts, end_ts),
        )
        return cur.fetchall()

    def get_all_bookings(self):
        """Все записи из таблицы bookings, как есть."""
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM bookings ORDER BY start_ts")
        return cur.fetchall()

    def check_conflicts(
        self,
        room: str,
        start_dt: datetime,
        end_dt: datetime,
        exclude_booking_id: int | None = None,
    ):
        s_ts = dt_to_ts(start_dt)
        e_ts = dt_to_ts(end_dt)
        cur = self.conn.cursor()

        if exclude_booking_id:
            cur.execute(
                """
                SELECT * FROM bookings
                WHERE room = ?
                  AND canceled = 0
                  AND id != ?
                  AND NOT (end_ts <= ? OR start_ts >= ?)
            """,
                (room, exclude_booking_id, s_ts, e_ts),
            )
        else:
            cur.execute(
                """
                SELECT * FROM bookings
                WHERE room = ?
                  AND canceled = 0
                  AND NOT (end_ts <= ? OR start_ts >= ?)
            """,
                (room, s_ts, e_ts),
            )
        return cur.fetchall()


# ---------------------- УТИЛИТЫ БОТА ----------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["Забронировать переговорку"],
        ["Мои брони", "Занятость на сегодня"],
        ["Занятость на ближайший месяц"],
        ["Помощь"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def ensure_private_chat(update: Update, reason: str) -> bool:
    """
    True, если чат приватный.
    Если нет – пишет понятное сообщение с указанием, зачем нужно перейти в личку.
    """
    chat = update.effective_chat
    if chat.type != Chat.PRIVATE:
        await update.effective_message.reply_text(
            f"Для {reason} напишите мне, пожалуйста, в личные сообщения 🙂",
            reply_markup=ReplyKeyboardRemove(),
        )
        return False
    return True


# ---------------------- ХЕНДЛЕРЫ /start и /help ----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    # В группе / супергруппе — тихий режим и убираем клавиатуру
    if chat.type != Chat.PRIVATE:
        await update.effective_message.reply_text(
            "Привет! Я бот для бронирования переговорок.\n"
            "Чтобы работать со мной, напишите мне, пожалуйста, в личные сообщения 🙂",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # В личке — нормальное приветствие с меню
    text = (
        f"Привет, {user.first_name}!\n\n"
        "Я бот для бронирования переговорок «3 этаж» и «4 этаж».\n\n"
        "Я умею:\n"
        "• бронировать переговорки\n"
        "• показывать ваши активные брони\n"
        "• показывать занятость переговорок\n\n"
        "Выберите действие в меню ниже 👇"
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    # В группе — только подсказка перейти в личку и убрать клавиатуру
    if chat.type != Chat.PRIVATE:
        await update.effective_message.reply_text(
            "Для справки и работы с ботом напишите мне, пожалуйста, в личные сообщения 🙂",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # В личке — подробная помощь + меню
    text = (
        "Как пользоваться ботом:\n\n"
        "• Команда /book или кнопка «Забронировать переговорку» — создать бронь.\n"
        "• «Мои брони» — список ваших активных встреч.\n"
        "• «Занятость на сегодня» — кто и когда занял переговорки сегодня.\n"
        "• «Занятость на ближайший месяц» — все брони на ближайшие 30 дней.\n\n"
        "• Удалить бронь — команда /del <ID>. ID видно в списке «Мои брони».\n"
        "• На любом шаге бронирования можно написать «Назад», чтобы вернуться на предыдущий шаг.\n"
        "Все шаги бронирования проходят в личном чате, чтобы не спамить общий чат 🙂"
    )
    await update.effective_message.reply_text(text, reply_markup=main_menu_keyboard())


# ---------------------- ДИАЛОГ БРОНИРОВАНИЯ ----------------------

def is_back_message(update: Update) -> bool:
    """
    Возвращает True, если пользователь хочет вернуться на предыдущий шаг.
    """
    if not getattr(update, "message", None) or not update.message.text:
        return False
    return update.message.text.strip().lower() in BACK_WORDS


async def book_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_private_chat(update, "бронирования переговорки"):
        return ConversationHandler.END

    context.user_data["booking"] = {}
    keyboard = [
        [
            InlineKeyboardButton("3 этаж", callback_data="ROOM_ROOM3"),
            InlineKeyboardButton("4 этаж", callback_data="ROOM_ROOM4"),
        ],
        [InlineKeyboardButton("Отмена", callback_data="ROOM_CANCEL")],
    ]
    await update.effective_message.reply_text(
        "Шаг 1/8. Выберите переговорку:", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return BOOK_ROOM


async def book_choose_room(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "ROOM_CANCEL":
        await query.edit_message_text("Бронирование отменено.", reply_markup=None)
        context.user_data.pop("booking", None)
        return ConversationHandler.END

    _, room_key = query.data.split("_", maxsplit=1)
    room = ROOMS.get(room_key)
    if not room:
        await query.edit_message_text("Не удалось определить переговорку.")
        return ConversationHandler.END

    context.user_data["booking"]["room"] = room
    await query.edit_message_text(
        f"Шаг 2/8. Вы выбрали: {room}\n\n"
        "Введите дату в формате ДД.ММ.ГГГГ или отправьте «Сегодня» / «Завтра»."
        + BACK_HINT,
        reply_markup=None,
    )
    return BOOK_DATE


async def book_choose_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Пользователь может вернуться к выбору переговорки
    if is_back_message(update):
        booking = context.user_data.get("booking", {})
        booking.pop("room", None)
        keyboard = [
            [
                InlineKeyboardButton("3 этаж", callback_data="ROOM_ROOM3"),
                InlineKeyboardButton("4 этаж", callback_data="ROOM_ROOM4"),
            ],
            [
                InlineKeyboardButton("Отмена", callback_data="ROOM_CANCEL"),
            ],
        ]
        await update.message.reply_text(
            "Шаг 1/8. Выберите переговорку (можно выбрать другую):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return BOOK_ROOM

    d = parse_date(update.message.text)
    if not d:
        await update.message.reply_text(
            "Не получается распознать дату 😕\n"
            "Введите в формате ДД.ММ.ГГГГ или напишите «Сегодня» / «Завтра»."
            + BACK_HINT
        )
        return BOOK_DATE

    today = date.today()
    if d < today:
        await update.message.reply_text("Нельзя бронировать дату в прошлом 🙈")
        return BOOK_DATE

    if d > today + timedelta(days=PLANNING_DAYS):
        await update.message.reply_text(
            f"Нельзя бронировать больше чем на {PLANNING_DAYS} дней вперёд."
        )
        return BOOK_DATE

    context.user_data["booking"]["date"] = d

    # Покажем занятость на этот день по выбранной переговорке
    room = context.user_data["booking"]["room"]
    busy_rows = DB.get_bookings_for_day(room, d)
    if busy_rows:
        lines = [f"Занятость {room} на {d.strftime('%d.%m.%Y')}:"]

        for row in busy_rows:
            interval = format_time_range(row["start_ts"], row["end_ts"])
            if row["is_block"]:
                reason = row["block_reason"] or "блокировка"
                lines.append(f"• {interval} — блокировка ({reason})")
            else:
                who = row["user_full_name"] or "Неизвестно"
                contact = row["user_contact"] or ""
                if contact:
                    lines.append(f"• {interval} — бронь | {who} ({contact})")
                else:
                    lines.append(f"• {interval} — бронь | {who}")
    else:
        lines = [f"На {d.strftime('%d.%m.%Y')} переговорка {room} свободна целый день ✅"]

    lines.append(
        "\nШаг 3/8. Введите время начала встречи в формате ЧЧ:ММ (например, 15:00)."
        + BACK_HINT
    )

    await update.message.reply_text("\n".join(lines))
    return BOOK_START


async def book_choose_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Пользователь может вернуться к вводу даты
    if is_back_message(update):
        booking = context.user_data.get("booking", {})
        booking.pop("start_ts", None)
        await update.message.reply_text(
            "Шаг 2/8. Введите дату встречи в формате ДД.ММ.ГГГ или словом «сегодня»/«завтра»."
            + BACK_HINT
        )
        return BOOK_DATE

    parsed = parse_time(update.message.text)
    if not parsed:
        await update.message.reply_text(
            "Не получилось распознать время 😕\nВведите в формате ЧЧ:ММ (например, 10:30)."
        )
        return BOOK_START

    h, m = parsed
    if h < WORK_START_HOUR or h >= WORK_END_HOUR:
        await update.message.reply_text(
            f"Бронировать можно только с {WORK_START_HOUR:02d}:00 до {WORK_END_HOUR:02d}:00."
        )
        return BOOK_START

    d = context.user_data["booking"]["date"]
    start_dt = combine_date_time(d, h, m)
    context.user_data["booking"]["start_dt"] = start_dt

    await update.message.reply_text(
        "Шаг 4/8. Введите время окончания встречи в формате ЧЧ:ММ (например, 16:00)."
        + BACK_HINT
    )
    return BOOK_END


async def book_choose_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Пользователь может вернуться к вводу времени начала
    if is_back_message(update):
        booking = context.user_data.get("booking", {})
        booking.pop("end_ts", None)
        await update.message.reply_text(
            "Шаг 3/8. Введите время начала встречи (по Москве), например 10:00."
            + BACK_HINT
        )
        return BOOK_START

    parsed = parse_time(update.message.text)
    if not parsed:
        await update.message.reply_text(
            "Не получилось распознать время 😕\nВведите в формате ЧЧ:ММ (например, 16:00)."
        )
        return BOOK_END

    h, m = parsed
    if h <= WORK_START_HOUR and not (h == 0 and m == 0):
        await update.message.reply_text(
            f"Бронировать можно только с {WORK_START_HOUR:02d}:00 до {WORK_END_HOUR:02d}:00."
        )
        return BOOK_END

    d = context.user_data["booking"]["date"]
    start_dt: datetime = context.user_data["booking"]["start_dt"]
    end_dt = combine_date_time(d, h, m)

    if end_dt <= start_dt:
        await update.message.reply_text(
            "Время окончания должно быть позже времени начала 😅\nПопробуйте ещё раз."
        )
        return BOOK_END

    if (end_dt - start_dt) < timedelta(minutes=MIN_DURATION_MINUTES):
        await update.message.reply_text(
            f"Минимальная длительность встречи — {MIN_DURATION_MINUTES} минут."
        )
        return BOOK_END

    if end_dt.hour > WORK_END_HOUR or (end_dt.hour == WORK_END_HOUR and end_dt.minute > 0):
        await update.message.reply_text(
            f"Встреча должна закончиться до {WORK_END_HOUR:02d}:00."
        )
        return BOOK_END

    # Проверка конфликтов
    room = context.user_data["booking"]["room"]
    conflicts = DB.check_conflicts(room, start_dt, end_dt)
    if conflicts:
        lines = ["К сожалению, в это время переговорка уже занята:"]
        for row in conflicts:
            interval = format_time_range(row["start_ts"], row["end_ts"])
            if row["is_block"]:
                reason = row["block_reason"] or "блокировка"
                lines.append(f"• {interval} — блокировка ({reason})")
            else:
                who = row["user_full_name"] or "Неизвестно"
                contact = row["user_contact"] or ""
                if contact:
                    lines.append(f"• {interval} — бронь | {who} ({contact})")
                else:
                    lines.append(f"• {interval} — бронь | {who}")
        lines.append("\nПожалуйста, введите другое время начала (ЧЧ:ММ).")
        await update.message.reply_text("\n".join(lines))
        return BOOK_START

    context.user_data["booking"]["end_dt"] = end_dt

    await update.message.reply_text(
        "Шаг 5/8. Введите тему встречи (например, «Интервью», «Планёрка отдела»)\n"
        "Или отправьте «-», чтобы пропустить."
        + BACK_HINT
    )
    return BOOK_TOPIC


async def book_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Пользователь может вернуться к вводу времени окончания
    if is_back_message(update):
        booking = context.user_data.get("booking", {})
        booking.pop("topic", None)
        await update.message.reply_text(
            "Шаг 4/8. Введите время окончания встречи (по Москве), например 12:00."
            + BACK_HINT
        )
        return BOOK_END

    text = update.message.text.strip()
    topic = None if text in ("-", "—", "") else text
    context.user_data["booking"]["topic"] = topic

    await update.message.reply_text(
        "Шаг 6/8. Введите вашу фамилию и имя (например, «Иванов Иван»)."
        + BACK_HINT
    )
    return BOOK_NAME


async def book_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Пользователь может вернуться к вводу темы встречи
    if is_back_message(update):
        booking = context.user_data.get("booking", {})
        booking.pop("user_full_name", None)
        await update.message.reply_text(
            "Шаг 5/8. Кратко опишите тему встречи. "
            "Если темы нет, можно оставить дефис «-»."
            + BACK_HINT
        )
        return BOOK_TOPIC

    full_name = update.message.text.strip()
    if not full_name:
        await update.message.reply_text("Пожалуйста, введите фамилию и имя.")
        return BOOK_NAME

    context.user_data["booking"]["user_full_name"] = full_name

    user = update.effective_user
    username_hint = f"@{user.username}" if user.username else "ник в Telegram"
    await update.message.reply_text(
        "Шаг 7/8. Введите ваш ник в Telegram (без @) или телефон.\n"
        f"Если хотите использовать {username_hint}, отправьте «-»."
        + BACK_HINT
    )
    return BOOK_CONTACT


async def book_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Пользователь может вернуться к вводу имени
    if is_back_message(update):
        booking = context.user_data.get("booking", {})
        booking.pop("user_contact", None)
        await update.message.reply_text(
            "Шаг 6/8. Введите фамилию и имя участника встречи "
            "(или того, кто отвечает за бронь)."
            + BACK_HINT
        )
        return BOOK_NAME

    text = update.message.text.strip()
    user = update.effective_user

    if text in ("-", "—", "") and user.username:
        contact = f"@{user.username}"
    else:
        contact = text

    context.user_data["booking"]["user_contact"] = contact

    # Показываем резюме
    b = context.user_data["booking"]
    room = b["room"]
    start_dt: datetime = b["start_dt"]
    end_dt: datetime = b["end_dt"]
    topic = b["topic"] or "—"
    full_name = b["user_full_name"]
    contact_str = b["user_contact"]

    summary = (
        "Проверьте, всё ли верно:\n\n"
        f"Переговорка: {room}\n"
        f"Дата: {start_dt.strftime('%d.%m.%Y')}\n"
        f"Время: {start_dt.strftime('%H:%M')}–{end_dt.strftime('%H:%M')}\n"
        f"Тема: {topic}\n"
        f"Забронировал: {full_name} ({contact_str})\n\n"
        "Шаг 8/8. Подтвердить бронь?"
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Подтвердить", callback_data="CONFIRM_OK"),
            InlineKeyboardButton("❌ Отменить", callback_data="CONFIRM_CANCEL"),
        ]
    ]
    await update.message.reply_text(summary, reply_markup=InlineKeyboardMarkup(keyboard))
    return BOOK_CONFIRM


async def book_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "CONFIRM_CANCEL":
        await query.edit_message_text("Бронирование отменено.", reply_markup=None)
        context.user_data.pop("booking", None)
        return ConversationHandler.END

    # Подтверждение
    b = context.user_data.get("booking")
    if not b:
        await query.edit_message_text("Данные бронирования не найдены. Попробуйте ещё раз.")
        return ConversationHandler.END

    room = b["room"]
    start_dt: datetime = b["start_dt"]
    end_dt: datetime = b["end_dt"]
    topic = b["topic"]
    full_name = b["user_full_name"]
    contact = b["user_contact"]

    user = query.from_user

    # Повторная проверка конфликтов (на случай гонок и двойных кликов)
    conflicts = DB.check_conflicts(room, start_dt, end_dt)
    if conflicts:
        await query.edit_message_text(
            "К сожалению, пока вы подтверждали, время успели занять.\n"
            "Попробуйте создать бронь ещё раз.",
            reply_markup=None,
        )
        context.user_data.pop("booking", None)
        return ConversationHandler.END

    booking_id = DB.create_booking(
        room=room,
        start_dt=start_dt,
        end_dt=end_dt,
        user_id=user.id,
        user_full_name=full_name,
        user_contact=contact,
        topic=topic,
    )

    # Запланируем напоминание за 1 день (если JobQueue есть)
    schedule_reminder_for_booking(context.application, booking_id)

    # Сообщение пользователю
    text_user = (
        "Бронь создана ✅\n\n"
        f"{room}, {start_dt.strftime('%d.%m.%Y')}, "
        f"{start_dt.strftime('%H:%M')}–{end_dt.strftime('%H:%M')}\n"
        f"Тема: {topic or '—'}\n"
        f"Забронировал: {full_name} ({contact})"
    )
    await query.edit_message_text(text_user, reply_markup=None)

    # Сообщение в общий чат (минимум спама)
    if GROUP_CHAT_ID is not None:
        text_group = (
            "Новая бронь переговорки:\n\n"
            f"{room}, {start_dt.strftime('%d.%m.%Y')}, "
            f"{start_dt.strftime('%H:%M')}–{end_dt.strftime('%H:%M')}\n"
            f"Тема: {topic or '—'}\n"
            f"Забронировал: {full_name} ({contact})"
        )
        try:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text_group)
        except Exception as e:
            logger.warning("Не удалось отправить сообщение в общий чат: %s", e)

    context.user_data.pop("booking", None)
    return ConversationHandler.END


async def book_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("booking", None)
    await update.effective_message.reply_text(
        "Диалог бронирования прерван.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# ---------------- Мои брони ----------------

async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = DB.get_user_future_bookings(user.id)

    if not rows:
        await update.message.reply_text("У тебя нет активных броней.")
        return

    lines = []
    for row in rows:
        booking_id = row["id"]
        room = row["room"]
        start = ts_to_dt(row["start_ts"])
        end = ts_to_dt(row["end_ts"])
        topic = row["topic"] or "—"

        date_str = start.strftime("%d.%m.%Y")
        time_str = f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
        lines.append(
            f"#{booking_id}: {date_str}, {time_str}, {room} — тема: {topic}"
        )

    text = (
        "Твои активные брони:\n\n"
        + "\n".join(lines)
        + "\n\nЧтобы отменить бронь, отправь команду:\n"
        "/del <ID_брони> (без скобок)."
    )
    await update.message.reply_text(text)

async def cancel_booking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    if not args:
        await update.effective_message.reply_text(
            "Укажите ID брони: /cancel_booking <ID>\n"
            "ID можно посмотреть в разделе «Мои брони»."
        )
        return

    try:
        booking_id = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("ID должен быть числом.")
        return

    row = DB.get_booking(booking_id)
    if not row or row["canceled"]:
        await update.effective_message.reply_text("Бронь с таким ID не найдена.")
        return

    # Проверка прав: владелец или админ
    if row["user_id"] != user.id and not is_admin(user.id):
        await update.effective_message.reply_text(
            "Вы не можете отменить эту бронь — она принадлежит другому пользователю."
        )
        return

    # Нельзя отменять после начала встречи
    start_dt = ts_to_dt(row["start_ts"])
    if now() >= start_dt:
        await update.effective_message.reply_text(
            "Встреча уже началась или завершилась, отменять нельзя."
        )
        return

    DB.cancel_booking(booking_id)
    await update.effective_message.reply_text("Бронь отменена ✅")

    # Уведомление в общий чат
    if GROUP_CHAT_ID is not None:
        who = row["user_full_name"] or "Неизвестно"
        contact = row["user_contact"] or ""
        dt_str = format_dt_range(row["start_ts"], row["end_ts"])
        text = (
            "Бронь отменена:\n"
            f"ID {booking_id}, {row['room']}, {dt_str}\n"
            f"{who} ({contact})"
        )
        try:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text)
        except Exception as e:
            logger.warning("Не удалось отправить уведомление об отмене в общий чат: %s", e)


# ---------------------- ЗАНЯТОСТЬ ----------------------
async def today_occupancy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Теперь тоже только в личке, чтобы не засорять общий чат
    if not await ensure_private_chat(update, "просмотра занятости переговорок"):
        return

    d = date.today()
    rows = DB.get_bookings_for_day(None, d)

    if not rows:
        await update.effective_message.reply_text(
            f"На сегодня ({d.strftime('%d.%m.%Y')}) переговорки свободны 🎉",
            reply_markup=main_menu_keyboard(),
        )
        return

    lines = [f"Занятость на сегодня ({d.strftime('%d.%m.%Y')}):\n"]
    for row in rows:
        room = row["room"]
        interval = format_time_range(row["start_ts"], row["end_ts"])
        if row["is_block"]:
            reason = row["block_reason"] or "блокировка"
            lines.append(f"{room}: {interval} — блокировка ({reason})")
        else:
            who = row["user_full_name"] or "Неизвестно"
            contact = row["user_contact"] or ""
            if contact:
                lines.append(f"{room}: {interval} — бронь | {who} ({contact})")
            else:
                lines.append(f"{room}: {interval} — бронь | {who}")

    await update.effective_message.reply_text("\n".join(lines), reply_markup=main_menu_keyboard())

async def month_occupancy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Только в личке, чтобы не засорять общий чат
    if not await ensure_private_chat(update, "просмотра занятости на месяц"):
        return

    today = date.today()
    start_dt = datetime(today.year, today.month, today.day, 0, 0)
    end_dt = start_dt + timedelta(days=30)  # ближайшие 30 дней

    start_ts = dt_to_ts(start_dt)
    end_ts = dt_to_ts(end_dt)

    rows = DB.get_bookings_for_range(start_ts, end_ts)

    if not rows:
        await update.effective_message.reply_text(
            "На ближайший месяц переговорки свободны 🎉",
            reply_markup=main_menu_keyboard(),
        )
        return

    period_text = f"{start_dt.strftime('%d.%m.%Y')}–{end_dt.strftime('%d.%m.%Y')}"
    header = f"Занятость на ближайший месяц ({period_text}):\n"

    # Следим за длиной сообщения, чтобы не вылезти за лимит Телеги
    max_len = 3500
    text = header
    current_date_str = None

    for row in rows:
        start_dt_row = ts_to_dt(row["start_ts"])
        end_dt_row = ts_to_dt(row["end_ts"])
        date_str = start_dt_row.strftime("%d.%m.%Y")

        if date_str != current_date_str:
            current_date_str = date_str
            line = f"\n{date_str}:\n"
            if len(text) + len(line) > max_len:
                await update.effective_message.reply_text(text)
                text = ""
            text += line

        room = row["room"]
        interval = f"{start_dt_row.strftime('%H:%M')}–{end_dt_row.strftime('%H:%M')}"

        if row["is_block"]:
            reason = row["block_reason"] or "блокировка"
            line = f"{room}: {interval} — блокировка ({reason})\n"
        else:
            who = row["user_full_name"] or "Неизвестно"
            contact = row["user_contact"] or ""
            topic = row["topic"] or "—"
            if contact:
                line = (
                    f"{room}: {interval} — бронь | {who} ({contact}), тема: {topic}\n"
                )
            else:
                line = f"{room}: {interval} — бронь | {who}, тема: {topic}\n"

        if len(text) + len(line) > max_len:
            await update.effective_message.reply_text(text)
            text = ""
        text += line

    if text:
        await update.effective_message.reply_text(
            text, reply_markup=main_menu_keyboard()
        )

async def busy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_private_chat(update, "просмотра занятости переговорок"):
        return ConversationHandler.END

    keyboard = [
        [
            InlineKeyboardButton("3 этаж", callback_data="BUSY_ROOM3"),
            InlineKeyboardButton("4 этаж", callback_data="BUSY_ROOM4"),
        ],
        [InlineKeyboardButton("Показать обе", callback_data="BUSY_BOTH")],
        [InlineKeyboardButton("Отмена", callback_data="BUSY_CANCEL")],
    ]
    await update.effective_message.reply_text(
        "Выберите переговорку для просмотра занятости:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return BUSY_ROOM


async def busy_choose_room(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "BUSY_CANCEL":
        await query.edit_message_text("Отмена просмотра занятости.", reply_markup=None)
        return ConversationHandler.END

    if query.data == "BUSY_BOTH":
        context.user_data["busy_room"] = None
    else:
        key = query.data.replace("BUSY_", "")
        room = ROOMS.get(key)
        context.user_data["busy_room"] = room

    await query.edit_message_text(
        "Введите дату в формате ДД.ММ.ГГГГ или отправьте «Сегодня» / «Завтра».",
        reply_markup=None,
    )
    return BUSY_DATE


async def busy_choose_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = parse_date(update.message.text)
    if not d:
        await update.message.reply_text(
            "Не получается распознать дату 😕\n"
            "Введите в формате ДД.ММ.ГГГГ или напишите «Сегодня» / «Завтра»."
        )
        return BUSY_DATE

    room = context.user_data.get("busy_room")
    rows = DB.get_bookings_for_day(room, d)
    if not rows:
        if room:
            text = f"На {d.strftime('%d.%m.%Y')} переговорка {room} свободна ✅"
        else:
            text = f"На {d.strftime('%d.%m.%Y')} обе переговорки свободны ✅"
        await update.message.reply_text(text)
        return ConversationHandler.END

    if room:
        title = f"Занятость {room} на {d.strftime('%d.%m.%Y')}:"
    else:
        title = f"Занятость переговорок на {d.strftime('%d.%m.%Y')}:"

    lines = [title, ""]
    for row in rows:
        r = row["room"]
        interval = format_time_range(row["start_ts"], row["end_ts"])
        if row["is_block"]:
            reason = row["block_reason"] or "блокировка"
            lines.append(f"{r}: {interval} — блокировка ({reason})")
        else:
            who = row["user_full_name"] or "Неизвестно"
            contact = row["user_contact"] or ""
            if contact:
                lines.append(f"{r}: {interval} — бронь | {who} ({contact})")
            else:
                lines.append(f"{r}: {interval} — бронь | {who}")

    await update.message.reply_text("\n".join(lines))
    return ConversationHandler.END


async def busy_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("busy_room", None)
    await update.effective_message.reply_text("Диалог просмотра занятости прерван.")
    return ConversationHandler.END


# ---------------------- АДМИН-ФУНКЦИИ ----------------------
async def admin_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.effective_message.reply_text("Вы не являетесь администратором.")
        return

    text = (
        "Режим администратора:\n\n"
        "/admin_block — заблокировать переговорку на время (ремонт, общий созвон и т.п.)\n"
        "/admin_day <ДД.ММ.ГГГГ> — показать все брони на день\n"
        "/cancel_booking <ID> — отменить любую бронь (у вас есть права админа)"
    )
    await update.effective_message.reply_text(text)

async def admin_reschedule_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пересоздать все напоминания для будущих броней (только для админов)."""
    user = update.effective_user

    if not is_admin(user.id):
        await update.effective_message.reply_text(
            "Эта команда доступна только администраторам."
        )
        return

    if not await ensure_private_chat(update, "пересоздания напоминаний"):
        return

    await update.effective_message.reply_text(
        "Пересоздаю напоминания для всех будущих броней..."
    )

    count = reschedule_all_booking_reminders(context.application)

    await update.effective_message.reply_text(
        f"Готово. Поставлены напоминания для {count} будущих броней."
    )

async def admin_block_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.effective_message.reply_text("Команда только для администраторов.")
        return ConversationHandler.END

    if not await ensure_private_chat(update, "администрирования переговорок"):
        return ConversationHandler.END

    keyboard = [
        [
            InlineKeyboardButton("3 этаж", callback_data="AB_ROOM3"),
            InlineKeyboardButton("4 этаж", callback_data="AB_ROOM4"),
        ],
        [InlineKeyboardButton("Отмена", callback_data="AB_CANCEL")],
    ]
    await update.effective_message.reply_text(
        "Админ: выберите переговорку для блокировки:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    context.user_data["ablock"] = {}
    return ADMIN_BLOCK_ROOM


async def admin_block_choose_room(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "AB_CANCEL":
        context.user_data.pop("ablock", None)
        await query.edit_message_text("Блокировка отменена.")
        return ConversationHandler.END

    key = query.data.replace("AB_", "")
    room = ROOMS.get(key)
    if not room:
        await query.edit_message_text("Не удалось определить переговорку.")
        return ConversationHandler.END

    context.user_data["ablock"]["room"] = room
    await query.edit_message_text(
        "Введите дату блокировки в формате ДД.ММ.ГГГГ или «Сегодня» / «Завтра».",
        reply_markup=None,
    )
    return ADMIN_BLOCK_DATE


async def admin_block_choose_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = parse_date(update.message.text)
    if not d:
        await update.message.reply_text(
            "Не получается распознать дату 😕\nВведите в формате ДД.ММ.ГГГГ."
        )
        return ADMIN_BLOCK_DATE

    context.user_data["ablock"]["date"] = d
    await update.message.reply_text("Введите время начала блокировки (ЧЧ:ММ).")
    return ADMIN_BLOCK_START


async def admin_block_choose_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = parse_time(update.message.text)
    if not parsed:
        await update.message.reply_text("Введите время в формате ЧЧ:ММ.")
        return ADMIN_BLOCK_START

    h, m = parsed
    d = context.user_data["ablock"]["date"]
    start_dt = combine_date_time(d, h, m)
    context.user_data["ablock"]["start_dt"] = start_dt

    await update.message.reply_text("Введите время окончания блокировки (ЧЧ:ММ).")
    return ADMIN_BLOCK_END


async def admin_block_choose_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = parse_time(update.message.text)
    if not parsed:
        await update.message.reply_text("Введите время в формате ЧЧ:ММ.")
        return ADMIN_BLOCK_END

    h, m = parsed
    d = context.user_data["ablock"]["date"]
    start_dt: datetime = context.user_data["ablock"]["start_dt"]
    end_dt = combine_date_time(d, h, m)

    if end_dt <= start_dt:
        await update.message.reply_text(
            "Время окончания должно быть позже времени начала."
        )
        return ADMIN_BLOCK_END

    room = context.user_data["ablock"]["room"]
    conflicts = DB.check_conflicts(room, start_dt, end_dt)
    if conflicts:
        await update.message.reply_text(
            "На это время уже есть брони или блокировки. "
            "Сначала отмените их, либо выберите другой интервал."
        )
        return ADMIN_BLOCK_END

    context.user_data["ablock"]["end_dt"] = end_dt
    await update.message.reply_text(
        "Введите причину блокировки (например, «общий созвон компании»)."
    )
    return ADMIN_BLOCK_REASON


async def admin_block_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text.strip() or "блокировка"
    ab = context.user_data["ablock"]
    room = ab["room"]
    start_dt = ab["start_dt"]
    end_dt = ab["end_dt"]

    booking_id = DB.create_booking(
        room=room,
        start_dt=start_dt,
        end_dt=end_dt,
        user_id=None,
        user_full_name=None,
        user_contact=None,
        topic=None,
        is_block=True,
        block_reason=reason,
    )

    await update.message.reply_text(
        f"Переговорка {room} заблокирована на {format_dt_range(dt_to_ts(start_dt), dt_to_ts(end_dt))}\n"
        f"Причина: {reason}\n(ID блокировки: {booking_id})"
    )
    context.user_data.pop("ablock", None)
    return ConversationHandler.END


async def admin_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.effective_message.reply_text("Команда только для администраторов.")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Укажите дату: /admin_day ДД.ММ.ГГГГ"
        )
        return

    d = parse_date(context.args[0])
    if not d:
        await update.effective_message.reply_text("Не могу распознать дату.")
        return

    rows = DB.get_bookings_for_day_all(d)
    if not rows:
        await update.effective_message.reply_text(
            f"На {d.strftime('%d.%m.%Y')} брони и блокировки отсутствуют."
        )
        return

    lines = [f"Все брони и блокировки на {d.strftime('%d.%m.%Y')}:\n"]
    for row in rows:
        id_ = row["id"]
        room = row["room"]
        interval = format_time_range(row["start_ts"], row["end_ts"])
        if row["is_block"]:
            reason = row["block_reason"] or "блокировка"
            lines.append(f"ID {id_}: {room}, {interval} — БЛОКИРОВКА ({reason})")
        else:
            who = row["user_full_name"] or "Неизвестно"
            contact = row["user_contact"] or ""
            topic = row["topic"] or "—"
            if contact:
                lines.append(
                    f"ID {id_}: {room}, {interval} — {who} ({contact}), тема: {topic}"
                )
            else:
                lines.append(
                    f"ID {id_}: {room}, {interval} — {who}, тема: {topic}"
                )

    await update.effective_message.reply_text("\n".join(lines))

# ---------------- Удаление брони по ID ----------------

async def delete_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменить бронь и уведомить общий чат после успешной отмены."""
    user = update.effective_user
    admin_user = user.id in ADMIN_IDS

    if not context.args:
        await update.message.reply_text(
            "Напиши команду в формате:\n"
            "/del <ID_брони>\n\n"
            "ID можно посмотреть в списке «Мои брони»."
        )
        return

    try:
        booking_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID брони должен быть числом. Пример: /del 12")
        return

    now_ts = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        # Сохраняем детали для уведомления ДО отметки брони как отмененной.
        cursor.execute(
            "SELECT * FROM bookings WHERE id = ? AND canceled = 0",
            (booking_id,),
        )
        row = cursor.fetchone()

        if admin_user:
            # Администратор может отменить любую активную бронь.
            cursor.execute(
                """
                UPDATE bookings
                SET canceled = 1, canceled_at = ?
                WHERE id = ? AND canceled = 0
                """,
                (now_ts, booking_id),
            )
        else:
            # Пользователь может отменить только собственную бронь.
            cursor.execute(
                """
                UPDATE bookings
                SET canceled = 1, canceled_at = ?
                WHERE id = ? AND user_id = ? AND canceled = 0
                """,
                (now_ts, booking_id, user.id),
            )
        updated = cursor.rowcount
        conn.commit()
    finally:
        conn.close()

    if updated == 0:
        if admin_user:
            msg = "Не нашла активную бронь с таким ID."
        else:
            msg = (
                "Не нашла активную бронь с таким ID, созданную от твоего имени.\n"
                "Проверь ID в списке «Мои брони»."
            )
        await update.message.reply_text(msg)
        return

    # Очищаем запланированные напоминания по отмененной брони.
    jobs_removed = 0
    job_queue = context.application.job_queue
    if job_queue is not None:
        for job in job_queue.get_jobs_by_name(f"reminder_{booking_id}"):
            job.schedule_removal()
            jobs_removed += 1
    logger.info("Deleted booking %s, removed %s reminder jobs", booking_id, jobs_removed)
    await update.message.reply_text("Готово, бронь удалена.")

    # Этот обработчик обслуживает обе команды: /del и /cancel_booking.
    # Раньше уведомление было лишь в неиспользуемой cancel_booking_command.
    if GROUP_CHAT_ID is None:
        logger.warning("GROUP_CHAT_ID не указан: уведомление об отмене брони %s пропущено", booking_id)
        return

    if row is None:
        logger.warning("Нет данных брони %s для уведомления общего чата", booking_id)
        return

    meeting = (
        f"{row['room']}, {format_dt_range(row['start_ts'], row['end_ts'])}\n"
        f"Тема: {row['topic'] or '—'}\n"
        f"Забронировал: {row['user_full_name'] or '—'} "
        f"({row['user_contact'] or '—'})"
    )
    text_group = (
        "❌ Бронь переговорки отменена:\n\n"
        f"{meeting}\n"
        f"ID брони: {booking_id}"
    )
    try:
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text_group)
    except Exception as exc:
        logger.warning("Не удалось уведомить чат об отмене брони %s: %s", booking_id, exc)
    else:
        logger.info("Уведомление об отмене брони %s отправлено в общий чат", booking_id)


async def export_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выгрузка всей таблицы bookings в CSV. Только для админов, только в личке."""
    user = update.effective_user

    if not is_admin(user.id):
        await update.effective_message.reply_text(
            "Эта команда доступна только администраторам."
        )
        return

    if not await ensure_private_chat(update, "выгрузки базы бронирований"):
        return

    rows = DB.get_all_bookings()
    if not rows:
        await update.effective_message.reply_text("В базе пока нет ни одной брони.")
        return

    output = io.StringIO()
    writer = csv.writer(output, delimiter=",")

    # Заголовки в том же порядке, что и в базе
    writer.writerow(
        [
            "id",
            "room",
            "start_ts",
            "end_ts",
            "user_id",
            "user_full_name",
            "user_contact",
            "topic",
            "is_block",
            "block_reason",
            "canceled",
            "canceled_at",
            "created_at",
        ]
    )

    for row in rows:
        writer.writerow(
            [
                row["id"],
                row["room"],
                row["start_ts"],
                row["end_ts"],
                row["user_id"],
                row["user_full_name"],
                row["user_contact"],
                row["topic"],
                row["is_block"],
                row["block_reason"],
                row["canceled"],
                row["canceled_at"],
                row["created_at"],
            ]
        )

    output.seek(0)
    data = output.getvalue().encode("utf-8-sig")
    file_obj = io.BytesIO(data)
    file_obj.name = "bookings_export.csv"

    await update.effective_message.reply_document(
        document=file_obj,
        filename="bookings_export.csv",
        caption="Выгрузка всех бронирований (сырой формат БД).",
    )

async def import_bookings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запросить у админа CSV для импорта."""
    user = update.effective_user

    if not is_admin(user.id):
        await update.effective_message.reply_text(
            "Эта команда доступна только администраторам."
        )
        return

    if not await ensure_private_chat(update, "импорта базы бронирований"):
        return

    context.user_data["awaiting_import_bookings"] = True
    await update.effective_message.reply_text(
        "Ок, импорт базы.\n\n"
        "Пришлите мне файлом CSV, который был получен из этой же версии бота "
        "командой /export_bookings.\n\n"
        "Внимание: текущие бронирования в базе будут полностью заменены "
        "на данные из файла."
    )

async def import_bookings_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка присланного CSV с бронированиями."""
    message = update.effective_message
    user = update.effective_user

    # Фильтр на всякий случай, если кто-то ещё пришлёт csv
    if not is_admin(user.id):
        return

    if not context.user_data.get("awaiting_import_bookings"):
        # Мы сейчас не ждём импорт — можно молча игнорировать
        return

    doc = message.document
    if not doc:
        return

    if not (doc.file_name or "").lower().endswith(".csv"):
        await message.reply_text("Мне нужен именно .csv файл, который вы выгрузили из бота.")
        return

    # Скачиваем файл
    file = await doc.get_file()
    data = await file.download_as_bytearray()
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter=",")

    required_fields = {
        "id",
        "room",
        "start_ts",
        "end_ts",
        "user_id",
        "user_full_name",
        "user_contact",
        "topic",
        "is_block",
        "block_reason",
        "canceled",
        "canceled_at",
        "created_at",
    }

    if not reader.fieldnames or not required_fields.issubset(set(reader.fieldnames)):
        await message.reply_text(
            "Не получается распознать формат CSV.\n"
            "Убедитесь, что файл выгружен этой же версией бота через /export_bookings."
        )
        return

    conn = DB.conn
    cur = conn.cursor()

    try:
        # Чистим таблицу
        cur.execute("DELETE FROM bookings")

        count = 0
        for row in reader:
            # Небольшой хелпер для чисел
            def to_int(name, allow_none=False):
                value = (row.get(name) or "").strip()
                if value == "":
                    return None if allow_none else 0
                return int(value)

            room = row.get("room") or ""

            start_ts = to_int("start_ts")
            end_ts = to_int("end_ts")
            user_id = to_int("user_id", allow_none=True)
            user_full_name = row.get("user_full_name") or ""
            user_contact = row.get("user_contact") or ""
            topic = row.get("topic") or ""
            is_block = to_int("is_block")
            block_reason = row.get("block_reason") or ""
            canceled = to_int("canceled")
            canceled_at = to_int("canceled_at", allow_none=True)
            created_at = to_int("created_at", allow_none=True)

            cur.execute(
                """
                INSERT INTO bookings (
                    room,
                    start_ts,
                    end_ts,
                    user_id,
                    user_full_name,
                    user_contact,
                    topic,
                    is_block,
                    block_reason,
                    canceled,
                    canceled_at,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    room,
                    start_ts,
                    end_ts,
                    user_id,
                    user_full_name,
                    user_contact,
                    topic,
                    is_block,
                    block_reason,
                    canceled,
                    canceled_at,
                    created_at,
                ),
            )
            count += 1

        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.exception("Ошибка при импорте бронирований из CSV")
        await message.reply_text(f"Что-то пошло не так при импорте: {e}")
        return
    finally:
        context.user_data["awaiting_import_bookings"] = False

    await message.reply_text(f"Импорт завершён. Загружено записей: {count}.")


# ---------------------- НАПОМИНАНИЯ ----------------------
def schedule_reminder_for_booking(app, booking_id: int):
    """
    Запланировать напоминание за день до встречи (если ещё есть время).
    Если JobQueue не настроен — просто логируем и выходим, чтобы не падать.
    """
    jq = getattr(app, "job_queue", None)
    if jq is None:
        logger.warning(
            "JobQueue is not configured, skipping reminder for booking %s",
            booking_id,
        )
        return

    row = DB.get_booking(booking_id)
    if not row or row["canceled"] or row["is_block"]:
        return

    start_dt = ts_to_dt(row["start_ts"])
    reminder_dt = start_dt - timedelta(days=1)
    delay = (reminder_dt - now()).total_seconds()

    if delay <= 0:
        # Уже поздно напоминать — пропускаем
        return

    jq.run_once(
        reminder_job,
        when=delay,
        data={"booking_id": booking_id},
        name=f"reminder_{booking_id}",
    )


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    booking_id = data.get("booking_id")
    row = DB.get_booking(booking_id)
    if not row or row["canceled"] or row["is_block"]:
        return

    start_dt = ts_to_dt(row["start_ts"])
    if now() >= start_dt:
        return  # встреча уже началась

    room = row["room"]
    interval = format_time_range(row["start_ts"], row["end_ts"])
    topic = row["topic"] or "—"
    who = row["user_full_name"] or "Неизвестно"
    contact = row["user_contact"] or ""

    # Напоминание пользователю
    if row["user_id"]:
        text_user = (
            "Напоминание о встрече завтра:\n\n"
            f"{room}, {start_dt.strftime('%d.%m.%Y')}, {interval}\n"
            f"Тема: {topic}"
        )
        try:
            await context.bot.send_message(chat_id=row["user_id"], text=text_user)
        except Exception as e:
            logger.warning("Не удалось отправить напоминание пользователю: %s", e)

    # Напоминание в общий чат
    if GROUP_CHAT_ID is not None:
        text_group = (
            "Напоминание: завтра запланирована встреча в переговорке.\n\n"
            f"{room}, {start_dt.strftime('%d.%m.%Y')}, {interval}\n"
            f"Тема: {topic}\n"
            f"Ответственный: {who} ({contact})"
        )
        try:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text_group)
        except Exception as e:
            logger.warning("Не удалось отправить напоминание в общий чат: %s", e)


async def post_init(app):
    """
    Вызывается один раз после инициализации приложения — дозапускаем напоминания
    для уже созданных будущих броней.
    """
    logger.info("post_init: планируем напоминания для будущих броней")

    jq = getattr(app, "job_queue", None)
    if jq is None:
        logger.warning("JobQueue is not configured, skipping reminders in post_init")
        return

    rows = DB.get_future_bookings()
    for row in rows:
        if row["canceled"] or row["is_block"]:
            continue
        booking_id = row["id"]
        start_dt = ts_to_dt(row["start_ts"])
        reminder_dt = start_dt - timedelta(days=1)
        delay = (reminder_dt - now()).total_seconds()
        if delay <= 0:
            continue
        jq.run_once(
            reminder_job,
            when=delay,
            data={"booking_id": booking_id},
            name=f"reminder_{booking_id}",
        )


# ---------------------- ОБЩИЙ ERROR-HANDLER ----------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Просто логируем исключения, чтобы PTB не ругался, что нет error handlers."""
    logger.error("Exception while handling an update:", exc_info=context.error)

# ← сюда вставляем reschedule_all_booking_reminders
def reschedule_all_booking_reminders(app) -> int:
    """
    Пересоздаём все напоминания для будущих, не отменённых броней.
    Используется:
    • при запуске бота (post_init)
    • по админской команде /admin_reschedule_reminders
    Возвращает количество созданных задач в job_queue.
    """
    jq = getattr(app, "job_queue", None)
    if jq is None:
        logger.warning("JobQueue is not configured, skipping reschedule")
        return 0

    # 1. Удаляем все старые задачи-напоминания
    for job in jq.jobs():
        if job.name and job.name.startswith("reminder_"):
            job.schedule_removal()

    # 2. Берём из БД все будущие, не отменённые и не блокировки
    rows = DB.get_future_bookings()

    count = 0
    for row in rows:
        if row["canceled"] or row["is_block"]:
            continue

        booking_id = row["id"]
        schedule_reminder_for_booking(app, booking_id)
        count += 1

    logger.info("Rescheduled reminders for %s future bookings", count)
    return count


# ---------------------- MAIN ----------------------
def load_admins_and_chat():
    global ADMIN_IDS, GROUP_CHAT_ID
    admins_env = os.getenv("ADMIN_IDS", "")
    if admins_env.strip():
        try:
            ADMIN_IDS = {int(x.strip()) for x in admins_env.split(",") if x.strip()}
        except ValueError:
            logger.warning("Не удалось распарсить ADMIN_IDS. Ожидались целые числа.")

    group_chat_env = os.getenv("GROUP_CHAT_ID")
    if group_chat_env:
        try:
            GROUP_CHAT_ID = int(group_chat_env)
        except ValueError:
            logger.warning("GROUP_CHAT_ID должен быть целым числом.")


def main():
    global DB

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения.")

    DB = BookingStorage(DB_PATH)

    load_admins_and_chat()

    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    # Общий error handler
    app.add_error_handler(error_handler)

    # Общие команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.Regex("^Помощь$"), help_command))

    # Бронирование
    book_conv = ConversationHandler(
        entry_points=[
            CommandHandler("book", book_start),
            MessageHandler(
                filters.TEXT
                & ~filters.COMMAND
                & filters.Regex("Забронировать переговорку"),
                book_start,
            ),
        ],
        states={
            BOOK_ROOM: [
                CallbackQueryHandler(book_choose_room, pattern="^ROOM_")
            ],
            BOOK_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, book_choose_date)
            ],
            BOOK_START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, book_choose_start)
            ],
            BOOK_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, book_choose_end)
            ],
            BOOK_TOPIC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, book_topic)
            ],
            BOOK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, book_name)
            ],
            BOOK_CONTACT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, book_contact)
            ],
            BOOK_CONFIRM: [
                CallbackQueryHandler(book_confirm, pattern="^CONFIRM_")
            ],
        },
        fallbacks=[CommandHandler("cancel", book_cancel_command)],
        name="booking_conversation",
    )
    app.add_handler(book_conv)

   # Мои брони / отмена
    app.add_handler(CommandHandler("my", my_bookings))
    app.add_handler(MessageHandler(filters.Regex("^Мои брони$"), my_bookings))
    app.add_handler(CommandHandler(["del", "cancel_booking"], delete_booking))
    
    # Занятость
    app.add_handler(CommandHandler("today", today_occupancy))
    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.Regex("Занятость на сегодня"),
            today_occupancy,
        )
    )

    app.add_handler(CommandHandler("month", month_occupancy))
    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.Regex("Занятость на ближайший месяц"),
            month_occupancy,
        )
    )

    busy_conv = ConversationHandler(
        entry_points=[CommandHandler("busy", busy_start)],
        states={
            BUSY_ROOM: [
                CallbackQueryHandler(busy_choose_room, pattern="^BUSY_")
            ],
            BUSY_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, busy_choose_date)
            ],
        },
        fallbacks=[CommandHandler("cancel", busy_cancel_command)],
        name="busy_conversation",
    )
    app.add_handler(busy_conv)


    # Админ
    app.add_handler(CommandHandler("admin", admin_info))
    app.add_handler(CommandHandler("admin_reschedule_reminders", admin_reschedule_reminders))

    admin_block_conv = ConversationHandler(
        entry_points=[CommandHandler("admin_block", admin_block_start)],
        states={
            ADMIN_BLOCK_ROOM: [
                CallbackQueryHandler(admin_block_choose_room, pattern="^AB_")
            ],
            ADMIN_BLOCK_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_block_choose_date)
            ],
            ADMIN_BLOCK_START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_block_choose_start)
            ],
            ADMIN_BLOCK_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_block_choose_end)
            ],
            ADMIN_BLOCK_REASON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_block_reason)
            ],
        },
        fallbacks=[CommandHandler("cancel", busy_cancel_command)],
        name="admin_block_conversation",
    )
    app.add_handler(admin_block_conv)

    app.add_handler(CommandHandler("admin_day", admin_day))
    app.add_handler(CommandHandler("export_bookings", export_bookings))
    app.add_handler(CommandHandler("import_bookings", import_bookings_start))

    app.add_handler(
        MessageHandler(
            filters.Document.FileExtension("csv"),
            import_bookings_file,
        )
    )

    logger.info("Бот запущен. Ожидаю апдейты...")
    app.run_polling()


if __name__ == "__main__":
    main()
