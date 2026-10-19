import asyncio
import logging
import sqlite3
import os
import random
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, 
    Update, ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import uvicorn

# ==========================================
# НАСТРОЙКИ
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_ЗДЕСЬ") 
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "") 
DB_PATH = "kicker.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

scheduler = AsyncIOScheduler()

# ==========================================
# ФРАЗЫ ДЛЯ РАНДОМИЗАЦИИ
# ==========================================
MORNING_PHRASES = [
    "Вставай, сонное хуйло. Какой план на сегодня?",
    "Так, боец. Докладывай, что ты ОБЯЗАН сделать сегодня.",
    "Хватит спать. Напиши свой план, пока я не разозлился.",
    "Подъем. День не ждет. Что делаем сегодня?",
    "Утро начинается не с кофе, а с плана. Пиши."
]

EVENING_PHRASES = [
    "Отчитывайся. Сделал?",
    "Ну что, воин, где результат?",
    "Вечер наступил. Показывай, что сделал.",
    "День прошел. Жми кнопки, не молчи.",
    "Время пришло. План выполнен или ты сдулся?"
]

STREAK_PHRASES = {
    3: "🔥 Ого, 3 дня без слива. Ты входишь во вкус.",
    7: "🚀 7 дней подряд! Ты машина, а не человек.",
    14: "👑 14 дней! Гатс тобой гордится. Не смей останавливаться.",
    30: "🏆 30 ДНЕЙ! Ты легенда. Но расслабляться рано."
}

# ==========================================
# БАЗА ДАННЫХ (SQLite)
# ==========================================
def db_connect():
    return sqlite3.connect(DB_PATH)

def init_db():
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                name TEXT,
                timezone TEXT,
                points INTEGER DEFAULT 0,
                streak INTEGER DEFAULT 0
            )
        """)
        # Добавили поле reminder_time для задач
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                date TEXT,
                plan_text TEXT,
                reminder_time TEXT,
                status TEXT DEFAULT 'pending'
            )
        """)
        con.commit()

def get_user(user_id):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT name, timezone, points, streak FROM users WHERE user_id = ?", (user_id,))
        return cur.fetchone()

def create_user(user_id, username, name, timezone):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("INSERT INTO users (user_id, username, name, timezone) VALUES (?, ?, ?, ?)", (user_id, username, name, timezone))
        con.commit()

def update_user_points_streak(user_id, points_add, streak_add):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("UPDATE users SET points = points + ?, streak = streak + ? WHERE user_id = ?", (points_add, streak_add, user_id))
        con.commit()

def save_task(user_id, date_str, text, r_time=None):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("INSERT INTO tasks (user_id, date, plan_text, reminder_time) VALUES (?, ?, ?, ?)", (user_id, date_str, text, r_time))
        con.commit()

def get_main_plan(user_id, date_str):
    # Ищем задачу без времени (это и есть утренний план)
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT id, plan_text, status FROM tasks WHERE user_id = ? AND date = ? AND reminder_time IS NULL", (user_id, date_str))
        return cur.fetchone()

def get_micro_tasks_by_time(user_id, date_str, r_time):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT id, plan_text FROM tasks WHERE user_id = ? AND date = ? AND reminder_time = ? AND status = 'pending'", (user_id, date_str, r_time))
        return cur.fetchall()

def update_task_status(task_id, status):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        con.commit()

def get_timezone_offset(tz_str):
    try:
        if "МСК" in tz_str.upper():
            offset = int(tz_str.upper().replace("МСК", "").replace("+", ""))
            return offset
    except:
        pass
    return 0

# ==========================================
# КЛАВИАТУРЫ
# ==========================================
def get_main_menu():
    kb = [
        [KeyboardButton(text="➕ Добавить задачу"), KeyboardButton(text="👤 Мой профиль")],
        [KeyboardButton(text="🏆 Таблица лидеров")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# ==========================================
# СОСТОЯНИЯ (FSM)
# ==========================================
class RegState(StatesGroup):
    waiting_for_name = State()
    waiting_for_timezone = State()

class TaskState(StatesGroup):
    waiting_for_text = State()
    waiting_for_time = State()

# ==========================================
# ХЕНДЛЕРЫ
# ==========================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if user:
        await message.answer(f"Ты уже в системе, {user[0]}. Не расслабляйся.", reply_markup=get_main_menu())
        return
    
    await message.answer("Слушай сюда. Я твой новый командир. Начнем с базы.\n\nКак к тебе обращаться по-мужски?")
    await state.set_state(RegState.waiting_for_name)

@router.message(RegState.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Принято. Теперь укажи свой часовой пояс относительно Москвы (например, МСК+0, МСК+2, МСК-1).")
    await state.set_state(RegState.waiting_for_timezone)

@router.message(RegState.waiting_for_timezone)
async def process_timezone(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data['name']
    offset = get_timezone_offset(message.text.strip())
    
    create_user(message.from_user.id, message.from_user.username, name, str(offset))
    await state.clear()
    
    await message.answer(f"Всё, {name}. Ты в системе. Завтра в 09:00 я спрошу твой план.", reply_markup=get_main_menu())

# ==========================================
# ДОБАВЛЕНИЕ ЗАДАЧ (FSM)
# ==========================================
@router.message(Command("add"))
@router.message(F.text == "➕ Добавить задачу")
async def start_add_task(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start.")
        return
    await message.answer("Что делаем? Напиши задачу (например: 'Пойти в зал'):")
    await state.set_state(TaskState.waiting_for_text)

@router.message(TaskState.waiting_for_text)
async def process_task_text(message: Message, state: FSMContext):
    text = message.text.strip()
    if len(text) < 3:
        await message.answer("Слишком коротко. Напиши нормально.")
        return
    await state.update_data(task_text=text)
    await message.answer("Во сколько напомнить? Напиши время в формате ЧЧ:ММ (например, 18:30) или напиши 'Без времени':")
    await state.set_state(TaskState.waiting_for_time)

@router.message(TaskState.waiting_for_time)
async def process_task_time(message: Message, state: FSMContext):
    time_input = message.text.strip().upper()
    data = await state.get_data()
    text = data['task_text']
    
    # Проверка на "без времени"
    if time_input in ["БЕЗ ВРЕМЕНИ", "НЕТ", "БЕЗ"]:
        r_time = None
        reply_text = f"Задача добавлена в общий список на сегодня.\n\nВечером спрошу за нее."
    else:
        # Валидация времени
        try:
            datetime.strptime(time_input, "%H:%M")
            r_time = time_input
            reply_text = f"Принято. Напомню в <b>{r_time}</b>. Не вздумай слить."
        except ValueError:
            await message.answer("Неправильный формат времени. Напиши как ЧЧ:ММ (например, 18:30).")
            return

    # Сохраняем в БД на сегодня
    user_tz = int(get_user(message.from_user.id)[1])
    user_time = datetime.utcnow() + timedelta(hours=3 + user_tz)
    today = user_time.strftime("%Y-%m-%d")
    
    save_task(message.from_user.id, today, text, r_time)
    await state.clear()
    await message.answer(reply_text, reply_markup=get_main_menu())

# ==========================================
# ОБРАБОТКА УТРЕННЕГО ПЛАНА (ТЕКСТОМ)
# ==========================================
@router.message(F.text & ~F.text.startswith('/') & ~F.text.startswith('➕') & ~F.text.startswith('👤') & ~F.text.startswith('🏆'))
async def handle_main_plan(message: Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start.")
        return

    user_tz = int(user[1])
    user_time = datetime.utcnow() + timedelta(hours=3 + user_tz)
    today = user_time.strftime("%Y-%m-%d")
    
    task = get_main_plan(message.from_user.id, today)
    
    if task:
        # Если юзер пишет текст, а план уже есть, и это не команда /add — он пытается добавить задачу без времени
        # Или просто болтает. Отправим его в кнопку "Добавить задачу".
        await message.answer("Ты уже сдал план на сегодня. Если хочешь добавить задачу, жми кнопку <b>➕ Добавить задачу</b>.", reply_markup=get_main_menu())
        return

    text = message.text.strip()
    if len(text) < 10:
        await message.answer("Это не план. Напиши конкретное действие. Пока не напишешь — я не отстану.")
        return

    save_task(message.from_user.id, today, text, r_time=None)
    await message.answer("Принято. Вечером спрошу. Не вздумай слить.", reply_markup=get_main_menu())

# ==========================================
# КОМАНДЫ И КНОПКИ ПРОФИЛЯ
# ==========================================
@router.message(Command("stats"))
@router.message(F.text == "👤 Мой профиль")
async def cmd_stats(message: Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напиши /start.")
        return
    
    name, tz, points, streak = user
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'success'", (message.from_user.id,))
        success = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'fail'", (message.from_user.id,))
        fail = cur.fetchone()[0]

    await message.answer(f"📊 <b>Твой отчет, {name}:</b>\n\n✅ Выполнено: {success}\n❌ Просрано: {fail}\n🔥 Стрик: {streak} дней\n💰 Очки: {points}", reply_markup=get_main_menu())

@router.message(Command("top"))
@router.message(F.text == "🏆 Таблица лидеров")
async def cmd_top(message: Message):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("""
            SELECT name, username, points, 
            (SELECT COUNT(*) FROM tasks WHERE user_id = users.user_id AND status = 'success') as success_count
            FROM users 
            ORDER BY points DESC LIMIT 5
        """)
        users = cur.fetchall()
    
    if not users:
        await message.answer("Пока никого нет в списке.")
        return
        
    text = "🏆 <b>Таблица лидеров (Топ-5):</b>\n\n"
    for i, u in enumerate(users, 1):
        name, username, points, success_count = u
        display_name = f"@{username}" if username else name
        text += f"{i}. {display_name} — {points} очков (сделок: {success_count})\n"
    
    await message.answer(text, reply_markup=get_main_menu())

# ==========================================
# ОБРАБОТКА КНОПОК ОТЧЕТА (УТРО/ВЕЧЕР)
# ==========================================
@router.callback_query(F.data.startswith("report_"))
async def handle_report(call: CallbackQuery):
    action = call.data.split("_")[1]
    user_id = call.from_user.id
    user = get_user(user_id)
    
    if not user:
        await call.answer("Ты кто?")
        return

    user_tz = int(user[1])
    user_time = datetime.utcnow() + timedelta(hours=3 + user_tz)
    today = user_time.strftime("%Y-%m-%d")
    
    # Получаем ID задачи из callback_data (например, report_success_15)
    parts = call.data.split("_")
    task_id = int(parts[2])
    
    if action == "success":
        update_task_status(task_id, 'success')
        # +10 очков, +1 стрик (только для утреннего плана)
        if len(parts) == 3 and parts[1] == 'success': # логика для микро-задач и утреннего
            update_user_points_streak(user_id, 10, 1)
            
            # Проверка на стрики
            new_streak = user[3] + 1
            streak_msg = STREAK_PHRASES.get(new_streak, "")
            
            response = "Неплохо. Завтра планка выше. (+10 очков)"
            if streak_msg:
                response += f"\n\n{streak_msg}"
            
            await call.message.edit_text(response)
    else:
        update_task_status(task_id, 'fail')
        if len(parts) == 3 and parts[1] == 'fail':
            update_user_points_streak(user_id, -5, 0) # Стрик сбрасывается, -5 очков
            await call.message.edit_text("Ты меня разочаровал. Гатс бы не сдался. Завтра исправишься. (-5 очков)")

# ==========================================
# ПЛАНИРОВЩИК (CRON)
# ==========================================
async def check_schedules():
    now_utc = datetime.utcnow()
    
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT user_id, name, timezone, points, streak FROM users")
        users = cur.fetchall()

    for user in users:
        user_id, name, tz_str, points, streak = user
        offset = int(tz_str)
        
        user_time = now_utc + timedelta(hours=3 + offset)
        current_time_str = user_time.strftime("%H:%M")
        today = user_time.strftime("%Y-%m-%d")
        
        # 1. Утро (09:00) - спрашиваем план
        if current_time_str == "09:00":
            task = get_main_plan(user_id, today)
            if not task:
                await bot.send_message(user_id, f"<b>{name}</b>, {random.choice(MORNING_PHRASES)}", reply_markup=get_main_menu())
        
        # 2. Вечер (21:00) - требуем отчет за план
        elif current_time_str == "21:00":
            task = get_main_plan(user_id, today)
            if task and task[2] == 'pending':
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Сделал", callback_data=f"report_success_{task[0]}"),
                     InlineKeyboardButton(text="❌ Просрал", callback_data=f"report_fail_{task[0]}")]
                ])
                await bot.send_message(user_id, f"Твой план был:\n<i>{task[1]}</i>\n\n<b>{random.choice(EVENING_PHRASES)}</b>", reply_markup=kb)
                
        # 3. Анти-слив утром (10:00)
        elif current_time_str == "10:00":
            task = get_main_plan(user_id, today)
            if not task:
                await bot.send_message(user_id, "Ты проигнорировал меня. Это минус. Сдулся?")
                
        # 4. Анти-слив вечером (23:00)
        elif current_time_str == "23:00":
            task = get_main_plan(user_id, today)
            if task and task[2] == 'pending':
                update_task_status(task[0], 'fail')
                update_user_points_streak(user_id, -5, 0)
                await bot.send_message(user_id, "Ты не отчитался. Считаю, что ты сдулся. Завтра докажи, что ты не тряпка.")
        
        # 5. НАПОМИНАНИЯ ПО МИКРО-ЗАДАЧАМ
        # Проверяем, есть ли задачи на это время
        micro_tasks = get_micro_tasks_by_time(user_id, today, current_time_str)
        for mt in micro_tasks:
            task_id, task_text = mt
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Сделал", callback_data=f"report_success_{task_id}"),
                 InlineKeyboardButton(text="❌ Просрал", callback_data=f"report_fail_{task_id}")]
            ])
            await bot.send_message(user_id, f"⏰ <b>Пора делать:</b>\n<i>{task_text}</i>\n\nСделал?", reply_markup=kb)

# ==========================================
# FASTAPI / WEBHOOK
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(check_schedules, 'cron', minute='*')
    scheduler.start()
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
        logging.info(f"Webhook установлен на {WEBHOOK_URL}")
    yield
    if WEBHOOK_URL:
        await bot.delete_webhook()
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/ping")
async def ping():
    return {"status": "alive"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))