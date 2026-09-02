import os
import sqlite3
import asyncio
import calendar
from datetime import datetime, timedelta
import pytz
from icalendar import Calendar, Event

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BufferedInputFile
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАСТРОЙКИ МАСТЕРА ---
BOT_TOKEN = "8910204900:AAGw63KIO2BdBagoVHTgRZj9l4vHQsaA5EQ"
MASTER_CHAT_ID = 1293157140
CHANNEL_ID = -1001886513960
ADDRESS = "ул. Гагарина 232, 1 подъезд, 5 этаж, кв. 12"
MOSCOW_TZ = pytz.timezone("Europe/Moscow")

MONTH_NAMES_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
}

STANDARD_TIMES = [
    f"{h:02d}:{m:02d}" for h in range(8, 22) for m in (0, 30)
]

SERVICES = {
    "manicure": {"title": "Маникюр", "duration": 2.5},
    "pedicure": {"title": "Педикюр", "duration": 1.5},
    "extension": {"title": "Наращивание / Коррекция", "duration": 3.5},
    "complex": {"title": "Маникюр + Педикюр", "duration": 4.0},
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            time TEXT,
            is_booked INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id INTEGER,
            client_chat_id INTEGER,
            client_name TEXT,
            client_username TEXT,
            service_key TEXT,
            status TEXT DEFAULT 'booked',
            reminded_24h INTEGER DEFAULT 0,
            reminded_2h INTEGER DEFAULT 0,
            FOREIGN KEY (slot_id) REFERENCES slots(id)
        )
    """)
    conn.commit()
    conn.close()

# --- ГЕНЕРАЦИЯ КАЛЕНДАРЯ (.ICS) ---
def generate_ics(service_title: str, client_name: str, client_contact: str, start_dt: datetime, duration_hours: float) -> bytes:
    cal = Calendar()
    cal.add('prodid', '-//Manicure Booking System//RU')
    cal.add('version', '2.0')
    end_dt = start_dt + timedelta(hours=duration_hours)

    event = Event()
    event.add('summary', f"{service_title} - {client_name}")
    event.add('description', f"Клиент: {client_name}\nКонтакт: {client_contact}\nУслуга: {service_title}")
    event.add('location', ADDRESS)
    event.add('dtstart', start_dt)
    event.add('dtend', end_dt)
    cal.add_component(event)
    return cal.to_ical()

# --- СОСТОЯНИЯ (FSM) ---
class ClientBooking(StatesGroup):
    choosing_date = State()
    choosing_time = State()
    choosing_service = State()
    entering_name = State()

class AdminManualBooking(StatesGroup):
    choosing_slot = State()
    choosing_service = State()
    entering_client_name = State()
    entering_client_contact = State()

# --- ПОСТОЯННЫЕ НИЖНИЕ КЛАВИАТУРЫ ---
def get_master_persistent_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🌸 Панель управления"), KeyboardButton(text="📝 Быстрая запись")]
        ],
        resize_keyboard=True,
        is_persistent=True
    )

def get_client_persistent_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💅 Записаться на процедуру"), KeyboardButton(text="📍 Моя запись / Адрес")]
        ],
        resize_keyboard=True,
        is_persistent=True
    )

# --- ИНТЕРАКТИВНОЕ МЕНЮ АДМИНКИ ---
def get_admin_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить окошки", callback_data="admin_pick_month")],
        [InlineKeyboardButton(text="🗑 Удалить свободные окошки", callback_data="admin_delete_slots_menu")],
        [InlineKeyboardButton(text="📢 Опубликовать график в канал", callback_data="admin_post_channel")],
        [InlineKeyboardButton(text="📝 Записать клиента вручную", callback_data="admin_manual_book")]
    ])

def build_month_calendar(year: int, month: int):
    month_name = MONTH_NAMES_RU[month]
    cal = calendar.monthcalendar(year, month)
    
    buttons = [
        [InlineKeyboardButton(text=f"🗓 {month_name} {year}", callback_data="ignore")]
    ]
    
    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    buttons.append([InlineKeyboardButton(text=wd, callback_data="ignore") for wd in week_days])
    
    for week in cal:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
            else:
                d_str = f"{day:02d}.{month:02d}"
                row.append(InlineKeyboardButton(text=str(day), callback_data=f"adate_{d_str}_{year}"))
        buttons.append(row)
        
    prev_month = 12 if month == 1 else month - 1
    prev_year = year - 1 if month == 1 else year
    next_month = 1 if month == 12 else month + 1
    next_year = year + 1 if month == 12 else year

    buttons.append([
        InlineKeyboardButton(text="⬅️ Пред. месяц", callback_data=f"acal_{prev_year}_{prev_month}"),
        InlineKeyboardButton(text="След. месяц ➡️", callback_data=f"acal_{next_year}_{next_month}")
    ])
    buttons.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_services_kb(prefix="service"):
    buttons = []
    for key, val in SERVICES.items():
        buttons.append([InlineKeyboardButton(text=f"{val['title']} ({val['duration']}ч)", callback_data=f"{prefix}_{key}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- АДМИН-МЕНЮ ---
@dp.message(Command("admin"))
@dp.message(F.text == "🌸 Панель управления")
async def show_admin_panel(message: types.Message):
    if message.from_user.id != MASTER_CHAT_ID:
        return
    await message.answer(
        "🌸 Панель управления расписанием",
        reply_markup=get_master_persistent_kb()
    )
    await message.answer("Выберите действие:", reply_markup=get_admin_main_kb())

@dp.message(F.text == "📝 Быстрая запись")
async def quick_manual_book(message: types.Message, state: FSMContext):
    if message.from_user.id != MASTER_CHAT_ID:
        return
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("SELECT id, date, time FROM slots WHERE is_booked = 0 ORDER BY id ASC LIMIT 15")
    slots = cur.fetchall()
    conn.close()

    if not slots:
        await message.answer("Нет свободных слотов для записи!")
        return

    buttons = [[InlineKeyboardButton(text=f"{d} в {t}", callback_data=f"manslot_{sid}")] for sid, d, t in slots]
    buttons.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="admin_menu")])
    await message.answer("Выберите слот для записи клиента:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(AdminManualBooking.choosing_slot)

@dp.callback_query(F.data == "admin_menu")
async def admin_menu_callback(call: types.CallbackQuery):
    if call.from_user.id != MASTER_CHAT_ID:
        return
    await call.message.edit_text("🌸 Панель управления расписанием", reply_markup=get_admin_main_kb())

@dp.callback_query(F.data == "admin_pick_month")
async def admin_pick_month(call: types.CallbackQuery):
    if call.from_user.id != MASTER_CHAT_ID:
        return
    now = datetime.now(MOSCOW_TZ)
    await call.message.edit_text("📅 Выберите день в календаре:", reply_markup=build_month_calendar(now.year, now.month))

@dp.callback_query(F.data.startswith("acal_"))
async def admin_change_calendar_month(call: types.CallbackQuery):
    if call.from_user.id != MASTER_CHAT_ID:
        return
    _, y_str, m_str = call.data.split("_")
    await call.message.edit_text("📅 Выберите день в календаре:", reply_markup=build_month_calendar(int(y_str), int(m_str)))

@dp.callback_query(F.data.startswith("adate_"))
async def admin_pick_time(call: types.CallbackQuery):
    if call.from_user.id != MASTER_CHAT_ID:
        return
    parts = call.data.split("_")
    d_str = parts[1]
    year_str = parts[2] if len(parts) > 2 else str(datetime.now().year)

    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("SELECT time FROM slots WHERE date = ?", (d_str,))
    existing = [r[0] for r in cur.fetchall()]
    conn.close()

    buttons = []
    row = []
    for t in STANDARD_TIMES:
        status_icon = "✅" if t in existing else "➕"
        row.append(InlineKeyboardButton(text=f"{status_icon} {t}", callback_data=f"atoggle_{d_str}_{t}_{year_str}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    month_num = int(d_str.split(".")[1])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад к календарю", callback_data=f"acal_{year_str}_{month_num}")])
    buttons.append([InlineKeyboardButton(text="✨ Готово (в главное меню)", callback_data="admin_menu")])

    await call.message.edit_text(
        f"🗓 Дата: {d_str}\n\n"
        f"• Нажимайте на время с шагом 30 минут:\n"
        f"• ➕ — добавить окошко\n"
        f"• ✅ — удалить выставленное окошко",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data.startswith("atoggle_"))
async def admin_toggle_time(call: types.CallbackQuery):
    if call.from_user.id != MASTER_CHAT_ID:
        return
    parts = call.data.split("_")
    d_str, t_str = parts[1], parts[2]
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("SELECT id, is_booked FROM slots WHERE date = ? AND time = ?", (d_str, t_str))
    res = cur.fetchone()

    if res:
        if res[1] == 1:
            await call.answer("Этот слот уже занят клиентом!", show_alert=True)
            conn.close()
            return
        cur.execute("DELETE FROM slots WHERE id = ?", (res[0],))
    else:
        cur.execute("INSERT INTO slots (date, time, is_booked) VALUES (?, ?, 0)", (d_str, t_str))
    conn.commit()
    conn.close()

    await admin_pick_time(call)

# --- УДАЛЕНИЕ СЛОТОВ ---
@dp.callback_query(F.data == "admin_delete_slots_menu")
async def admin_delete_menu(call: types.CallbackQuery):
    if call.from_user.id != MASTER_CHAT_ID:
        return
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("SELECT id, date, time FROM slots WHERE is_booked = 0 ORDER BY id ASC")
    slots = cur.fetchall()
    conn.close()

    if not slots:
        await call.answer("Нет свободных слотов для удаления!", show_alert=True)
        return

    buttons = []
    for sid, d, t in slots:
        buttons.append([InlineKeyboardButton(text=f"❌ Удалить {d} в {t}", callback_data=f"adelslot_{sid}")])
    buttons.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="admin_menu")])

    await call.message.edit_text("🗑 Нажмите на слот, который хотите удалить:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("adelslot_"))
async def admin_delete_action(call: types.CallbackQuery):
    if call.from_user.id != MASTER_CHAT_ID:
        return
    slot_id = int(call.data.split("_")[1])
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM slots WHERE id = ? AND is_booked = 0", (slot_id,))
    conn.commit()
    conn.close()
    await call.answer("Слот удален!")
    await admin_delete_menu(call)

# --- ПУБЛИКАЦИЯ В КАНАЛ (БЕЗ АДРЕСА) ---
@dp.callback_query(F.data == "admin_post_channel")
async def admin_post_channel(call: types.CallbackQuery):
    if call.from_user.id != MASTER_CHAT_ID:
        return
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("SELECT date, time FROM slots WHERE is_booked = 0 ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await call.answer("Нет свободных окошек для публикации!", show_alert=True)
        return

    first_date = rows[0][0]
    month_num = int(first_date.split(".")[1])
    month_title = MONTH_NAMES_RU.get(month_num, "месяц")

    schedule_dict = {}
    for d, t in rows:
        schedule_dict.setdefault(d, []).append(t)

    text = f"🌸 Свободные окошки на {month_title}:\n\n"
    for d, times in schedule_dict.items():
        text += f"🗓 {d}: {', '.join(times)}\n"
    text += f"\n✨ Жмите на кнопку ниже для быстрой записи:"

    bot_me = await bot.get_me()
    channel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Записаться тут ✨", url=f"https://t.me/{bot_me.username}?start=book")]
    ])

    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=text, reply_markup=channel_kb)
        await call.answer("Пост успешно опубликован в канал! 🚀", show_alert=True)
    except Exception as e:
        await call.answer(f"Ошибка публикации: {e}", show_alert=True)

# --- РУЧНАЯ ЗАПИСЬ МАСТЕРОМ ---
@dp.callback_query(F.data == "admin_manual_book")
async def admin_manual_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != MASTER_CHAT_ID:
        return
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("SELECT id, date, time FROM slots WHERE is_booked = 0 ORDER BY id ASC LIMIT 15")
    slots = cur.fetchall()
    conn.close()

    if not slots:
        await call.answer("Нет свободных слотов!", show_alert=True)
        return

    buttons = [[InlineKeyboardButton(text=f"{d} в {t}", callback_data=f"manslot_{sid}")] for sid, d, t in slots]
    buttons.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="admin_menu")])
    await call.message.edit_text("Выберите слот для записи клиента:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(AdminManualBooking.choosing_slot)

@dp.callback_query(AdminManualBooking.choosing_slot, F.data.startswith("manslot_"))
async def admin_manual_slot_picked(call: types.CallbackQuery, state: FSMContext):
    slot_id = int(call.data.split("_")[1])
    await state.update_data(slot_id=slot_id)
    await call.message.edit_text("Выберите услугу для клиента:", reply_markup=get_services_kb("manservice"))
    await state.set_state(AdminManualBooking.choosing_service)

@dp.callback_query(AdminManualBooking.choosing_service, F.data.startswith("manservice_"))
async def admin_manual_service_picked(call: types.CallbackQuery, state: FSMContext):
    service_key = call.data.split("_")[1]
    await state.update_data(service_key=service_key)
    await call.message.edit_text("Введите имя клиента:")
    await state.set_state(AdminManualBooking.entering_client_name)

@dp.message(AdminManualBooking.entering_client_name)
async def admin_manual_name_entered(message: types.Message, state: FSMContext):
    await state.update_data(client_name=message.text.strip())
    await message.answer("Введите контакт клиента (@username или телефон):")
    await state.set_state(AdminManualBooking.entering_client_contact)

@dp.message(AdminManualBooking.entering_client_contact)
async def admin_manual_finish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    contact = message.text.strip()
    slot_id = data["slot_id"]
    service_key = data["service_key"]
    name = data["client_name"]

    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("UPDATE slots SET is_booked = 1 WHERE id = ?", (slot_id,))
    cur.execute("""
        INSERT INTO appointments (slot_id, client_name, client_username, service_key, status)
        VALUES (?, ?, ?, ?, 'confirmed')
    """, (slot_id, name, contact, service_key))
    app_id = cur.lastrowid
    cur.execute("SELECT date, time FROM slots WHERE id = ?", (slot_id,))
    slot_date, slot_time = cur.fetchone()
    conn.commit()
    conn.close()

    bot_me = await bot.get_me()
    invite_link = f"https://t.me/{bot_me.username}?start=reg_{app_id}"

    current_year = datetime.now().year
    day, month = map(int, slot_date.split("."))
    hour, minute = map(int, slot_time.split(":"))
    start_dt = MOSCOW_TZ.localize(datetime(current_year, month, day, hour, minute))
    srv = SERVICES[service_key]

    ics_bytes = generate_ics(srv["title"], name, contact, start_dt, srv["duration"])
    ics_file = BufferedInputFile(ics_bytes, filename=f"booking_{slot_date}_{slot_time}.ics")

    await message.answer(
        f"✅ Клиент успешно записан!\n\n"
        f"👤 {name} ({contact})\n"
        f"🗓 {slot_date} в {slot_time}\n"
        f"💅 {srv['title']}\n\n"
        f"🔗 Ссылка для клиента для автонапоминаний:\n{invite_link}\n\n"
        f"📎 Нажмите на файл ниже на iPhone, чтобы добавить запись в календарь:",
        reply_markup=get_master_persistent_kb()
    )
    await bot.send_document(chat_id=MASTER_CHAT_ID, document=ics_file)
    await state.clear()

# --- КЛИЕНТСКИЙ СЦЕНАРИЙ ЗАПИСИ И ПОСТОЯННОЕ МЕНЮ ---
@dp.message(Command("start"))
@dp.message(F.text == "💅 Записаться на процедуру")
async def client_start(message: types.Message, state: FSMContext):
    if message.from_user.id == MASTER_CHAT_ID:
        await message.answer("🌸 Панель мастера активна:", reply_markup=get_master_persistent_kb())
        await message.answer("Выберите действие:", reply_markup=get_admin_main_kb())
        return

    args = message.text.split()
    if len(args) > 1 and args[1].startswith("reg_"):
        app_id = int(args[1].split("_")[1])
        conn = sqlite3.connect("bot_database.db")
        cur = conn.cursor()
        cur.execute("UPDATE appointments SET client_chat_id = ? WHERE id = ?", (message.chat.id, app_id))
        cur.execute("""
            SELECT a.service_key, s.date, s.time 
            FROM appointments a JOIN slots s ON a.slot_id = s.id 
            WHERE a.id = ?
        """, (app_id,))
        res = cur.fetchone()
        conn.commit()
        conn.close()

        if res:
            s_key, d, t = res
            srv_title = SERVICES[s_key]["title"]
            await message.answer(
                f"🌸 Вы подключили напоминания!\n\n"
                f"Жду вас {d} в {t} на процедуру: {srv_title}\n"
                f"📍 Адрес: {ADDRESS}\n\n"
                f"Я пришлю напоминание за 24 часа и за 2 часа до визита! ✨",
                reply_markup=get_client_persistent_kb()
            )
            return

    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT date FROM slots WHERE is_booked = 0 ORDER BY id ASC")
    dates = cur.fetchall()
    conn.close()

    if not dates:
        await message.answer(
            "🌸 К сожалению, пока нет свободных окошек. Следите за обновлениями в канале!",
            reply_markup=get_client_persistent_kb()
        )
        return

    buttons = [[InlineKeyboardButton(text=f"🗓 {d[0]}", callback_data=f"cdate_{d[0]}")] for d in dates]
    await message.answer(
        "🌸 Добро пожаловать!\nВыберите удобную дату:",
        reply_markup=get_client_persistent_kb()
    )
    await message.answer("Свободные даты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(ClientBooking.choosing_date)

@dp.message(F.text == "📍 Моя запись / Адрес")
async def client_check_booking(message: types.Message):
    if message.from_user.id == MASTER_CHAT_ID:
        return
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("""
        SELECT a.service_key, s.date, s.time 
        FROM appointments a 
        JOIN slots s ON a.slot_id = s.id 
        WHERE a.client_chat_id = ? AND a.status IN ('booked', 'confirmed')
        ORDER BY a.id DESC LIMIT 1
    """, (message.chat.id,))
    res = cur.fetchone()
    conn.close()

    if res:
        s_key, d, t = res
        srv_title = SERVICES[s_key]["title"]
        await message.answer(
            f"🌸 Ваша текущая запись:\n\n"
            f"🗓 Когда: {d} в {t}\n"
            f"Процедура: {srv_title}\n"
            f"📍 Адрес: {ADDRESS}\n\n"
            f"Жду вас! До встречи ✨",
            reply_markup=get_client_persistent_kb()
        )
    else:
        # Адрес полностью скрыт, только предложение записаться
        await message.answer(
            "У вас пока нет активных записей 🌸\n\n"
            "Чтобы выбрать удобный день и время, нажмите кнопку «💅 Записаться на процедуру» ниже.",
            reply_markup=get_client_persistent_kb()
        )

@dp.callback_query(ClientBooking.choosing_date, F.data.startswith("cdate_"))
async def client_date_picked(call: types.CallbackQuery, state: FSMContext):
    chosen_date = call.data.split("_")[1]
    await state.update_data(chosen_date=chosen_date)

    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("SELECT id, time FROM slots WHERE date = ? AND is_booked = 0 ORDER BY id ASC", (chosen_date,))
    times = cur.fetchall()
    conn.close()

    buttons = [[InlineKeyboardButton(text=f"⏰ {t[1]}", callback_data=f"ctime_{t[0]}_{t[1]}")] for t in times]
    await call.message.edit_text(f"🗓 Дата: {chosen_date}\nВыберите удобное время:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(ClientBooking.choosing_time)

@dp.callback_query(ClientBooking.choosing_time, F.data.startswith("ctime_"))
async def client_time_picked(call: types.CallbackQuery, state: FSMContext):
    _, slot_id, slot_time = call.data.split("_")
    await state.update_data(slot_id=int(slot_id), slot_time=slot_time)
    await call.message.edit_text("✨ Выберите желаемую процедуру:", reply_markup=get_services_kb("cservice"))
    await state.set_state(ClientBooking.choosing_service)

@dp.callback_query(ClientBooking.choosing_service, F.data.startswith("cservice_"))
async def client_service_picked(call: types.CallbackQuery, state: FSMContext):
    service_key = call.data.split("_")[1]
    await state.update_data(service_key=service_key)
    await call.message.edit_text("🌸 Введите, пожалуйста, ваше имя:")
    await state.set_state(ClientBooking.entering_name)

@dp.message(ClientBooking.entering_name)
async def client_finish(message: types.Message, state: FSMContext):
    name = message.text.strip()
    data = await state.get_data()
    slot_id = data["slot_id"]
    service_key = data["service_key"]
    chosen_date = data["chosen_date"]
    slot_time = data["slot_time"]
    user_name = f"@{message.from_user.username}" if message.from_user.username else "Без @тега"

    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("UPDATE slots SET is_booked = 1 WHERE id = ?", (slot_id,))
    cur.execute("""
        INSERT INTO appointments (slot_id, client_chat_id, client_name, client_username, service_key, status)
        VALUES (?, ?, ?, ?, ?, 'booked')
    """, (slot_id, message.chat.id, name, user_name, service_key))
    conn.commit()
    conn.close()

    srv = SERVICES[service_key]

    await message.answer(
        f"🌸 Вы успешно записаны!\n\n"
        f"🗓 Когда: {chosen_date} в {slot_time}\n"
        f"Процедура: {srv['title']}\n"
        f"📍 Адрес: {ADDRESS}\n\n"
        f"Я напомню вам о встрече за сутки и за 2 часа. До встречи! ✨",
        reply_markup=get_client_persistent_kb()
    )

    current_year = datetime.now().year
    day, month = map(int, chosen_date.split("."))
    hour, minute = map(int, slot_time.split(":"))
    start_dt = MOSCOW_TZ.localize(datetime(current_year, month, day, hour, minute))

    ics_bytes = generate_ics(srv["title"], name, user_name, start_dt, srv["duration"])
    ics_file = BufferedInputFile(ics_bytes, filename=f"booking_{chosen_date}_{slot_time}.ics")

    await bot.send_message(
        chat_id=MASTER_CHAT_ID,
        text=f"🔔 Новая онлайн-запись!\n\n"
             f"• Клиент: {name} ({user_name})\n"
             f"• Дата: {chosen_date} в {slot_time}\n"
             f"• Услуга: {srv['title']}\n"
             f"• Длительность: {srv['duration']} ч.\n\n"
             f"📎 Файл для добавления в календарь iPhone:",
        reply_markup=get_master_persistent_kb()
    )
    await bot.send_document(chat_id=MASTER_CHAT_ID, document=ics_file)
    await state.clear()

# --- ПЛАНИРОВЩИК НАПОМИНАНИЙ ---
async def check_reminders():
    now = datetime.now(MOSCOW_TZ)
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()

    cur.execute("""
        SELECT a.id, a.client_chat_id, a.client_name, a.service_key, s.date, s.time, a.reminded_24h, a.reminded_2h
        FROM appointments a
        JOIN slots s ON a.slot_id = s.id
        WHERE a.status IN ('booked', 'confirmed') AND a.client_chat_id IS NOT NULL
    """)
    records = cur.fetchall()

    current_year = now.year
    for rec in records:
        app_id, chat_id, name, s_key, s_date, s_time, rem_24, rem_2 = rec
        day, month = map(int, s_date.split("."))
        hour, minute = map(int, s_time.split(":"))
        app_dt = MOSCOW_TZ.localize(datetime(current_year, month, day, hour, minute))

        time_diff = app_dt - now
        hours_diff = time_diff.total_seconds() / 3600.0
        srv_title = SERVICES[s_key]["title"]

        if 23.5 <= hours_diff <= 24.5 and not rem_24:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Буду обязательно", callback_data=f"conf_{app_id}"),
                InlineKeyboardButton(text="🔄 Отменить / Перенести", callback_data=f"canc_{app_id}")
            ]])
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"🌸 Напоминание о записи на завтра!\n\n"
                         f"Жду вас завтра в {s_time} на процедуру: {srv_title}\n"
                         f"📍 Адрес: {ADDRESS}\n\n"
                         f"Пожалуйста, подтвердите визит:",
                    reply_markup=kb
                )
                cur.execute("UPDATE appointments SET reminded_24h = 1 WHERE id = ?", (app_id,))
                conn.commit()
            except Exception:
                pass

        if 1.5 <= hours_diff <= 2.5 and not rem_2:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да, уже собираюсь", callback_data=f"conf_{app_id}"),
                InlineKeyboardButton(text="⚠️ Не смогу прийти", callback_data=f"canc_{app_id}")
            ]])
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"☕️ До нашей встречи осталось 2 часа!\n\n"
                         f"Жду вас к {s_time} на процедуру: {srv_title}\n"
                         f"📍 Адрес: {ADDRESS}",
                    reply_markup=kb
                )
                cur.execute("UPDATE appointments SET reminded_2h = 1 WHERE id = ?", (app_id,))
                conn.commit()
            except Exception:
                pass

    conn.close()

# --- ОБРАБОТЧИКИ КНОПОК ПОДТВЕРЖДЕНИЯ И ОТМЕНЫ ---
@dp.callback_query(F.data.startswith("conf_"))
async def handle_confirm(call: types.CallbackQuery):
    app_id = int(call.data.split("_")[1])
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("UPDATE appointments SET status = 'confirmed' WHERE id = ?", (app_id,))
    cur.execute("SELECT client_name, service_key FROM appointments WHERE id = ?", (app_id,))
    res = cur.fetchone()
    conn.commit()
    conn.close()

    await call.message.edit_text(call.message.text + "\n\n✅ Запись подтверждена! Жду вас ✨", reply_markup=None)
    await call.answer("Спасибо за подтверждение!")
    if res:
        await bot.send_message(MASTER_CHAT_ID, f"🟢 Клиент подтвердил запись!\n👤 {res[0]} ({SERVICES[res[1]]['title']})")

@dp.callback_query(F.data.startswith("canc_"))
async def handle_cancel(call: types.CallbackQuery):
    app_id = int(call.data.split("_")[1])
    conn = sqlite3.connect("bot_database.db")
    cur = conn.cursor()
    cur.execute("SELECT slot_id, client_name, service_key FROM appointments WHERE id = ?", (app_id,))
    res = cur.fetchone()
    if res:
        slot_id, name, s_key = res
        cur.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (app_id,))
        cur.execute("UPDATE slots SET is_booked = 0 WHERE id = ?", (slot_id,))
        cur.execute("SELECT date, time FROM slots WHERE id = ?", (slot_id,))
        s_date, s_time = cur.fetchone()
        conn.commit()

        await call.message.edit_text("🤍 Запись отменена. Буду рада видеть вас в другой раз!", reply_markup=None)
        await call.answer("Запись отменена")

        await bot.send_message(
            MASTER_CHAT_ID,
            f"🔴 Внимание: клиент отменил запись!\n\n"
            f"👤 {name}\n"
            f"🗓 Освободилось окно: {s_date} в {s_time}\n"
            f"Была услуга: {SERVICES[s_key]['title']}\n\n"
            f"Слот автоматически вернулся в свободные."
        )
    conn.close()

# --- ВСТРОЕННЫЙ СЕРВЕР ДЛЯ FREE WEB SERVICE ---
async def handle_ping(request):
    return web.Response(text="Bot is running 24/7!")

async def run_dummy_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# --- ЗАПУСК ---
async def main():
    init_db()
    scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
    scheduler.add_job(check_reminders, "interval", minutes=1)
    scheduler.start()
    
    await run_dummy_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
