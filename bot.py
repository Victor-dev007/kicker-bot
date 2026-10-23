import asyncio
import logging
import sqlite3
import os
import random
import re
import hashlib
import hmac
import json
from urllib.parse import parse_qsl
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, 
    Update, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import uvicorn

# ==========================================
# НАСТРОЙКИ
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ Ошибка: Переменная окружения BOT_TOKEN не задана!")

WEBAPP_URL = os.getenv("WEBAPP_URL", "https://kicker-bot-kp47.onrender.com/webapp")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "") 
DB_PATH = "kicker.db"
USE_POLLING = False 

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

scheduler = AsyncIOScheduler()

# ==========================================
# ФРАЗЫ
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

# ==========================================
# БАЗА ДАННЫХ
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
                streak INTEGER DEFAULT 0,
                morning_time TEXT,
                evening_time TEXT,
                timer_end TEXT,
                penalty_5min_sent INTEGER DEFAULT 0,
                penalty_morning_sent INTEGER DEFAULT 0,
                last_report_msg_id INTEGER
            )
        """)
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
        # ТУТ 10 ПЕРЕМЕННЫХ
        cur.execute("SELECT name, timezone, points, streak, morning_time, evening_time, timer_end, penalty_5min_sent, penalty_morning_sent, last_report_msg_id FROM users WHERE user_id = ?", (user_id,))
        return cur.fetchone()

def create_user(user_id, username, name, timezone, morning_time, evening_time):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("INSERT INTO users (user_id, username, name, timezone, morning_time, evening_time) VALUES (?, ?, ?, ?, ?, ?)", 
                    (user_id, username, name, timezone, morning_time, evening_time))
        con.commit()

def add_points_and_increment_streak(user_id, points_add):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("UPDATE users SET points = points + ?, streak = streak + 1 WHERE user_id = ?", (points_add, user_id))
        con.commit()

def add_points_and_reset_streak(user_id, points_add):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("UPDATE users SET points = points + ?, streak = 0, timer_end = NULL WHERE user_id = ?", (points_add, user_id))
        con.commit()

def save_task(user_id, date_str, text, r_time=None):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("INSERT INTO tasks (user_id, date, plan_text, reminder_time) VALUES (?, ?, ?, ?)", (user_id, date_str, text, r_time))
        con.commit()

def get_main_plan(user_id, date_str):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT id, plan_text, status FROM tasks WHERE user_id = ? AND date = ? AND reminder_time IS NULL", (user_id, date_str))
        return cur.fetchone()

def get_all_tasks_today(user_id, date_str):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT id, plan_text, reminder_time, status FROM tasks WHERE user_id = ? AND date = ?", (user_id, date_str))
        return cur.fetchall()

def get_pending_tasks_today(user_id, date_str):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT id, plan_text, reminder_time FROM tasks WHERE user_id = ? AND date = ? AND status = 'pending'", (user_id, date_str))
        return cur.fetchall()

def update_task_status(task_id, status):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        con.commit()

def clear_timer_and_penalty(user_id):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("UPDATE users SET timer_end = NULL, penalty_5min_sent = 0 WHERE user_id = ?", (user_id,))
        con.commit()

def set_timer(user_id, end_time_str):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("UPDATE users SET timer_end = ? WHERE user_id = ?", (end_time_str, user_id))
        con.commit()

def get_timezone_offset(tz_str):
    tz_str = tz_str.upper().replace("МСК", "").strip()
    if re.match(r'^[+-]\d+$', tz_str):
        try:
            return int(tz_str)
        except ValueError:
            pass
    return None

def format_time_check(time_str, min_h, max_h):
    try:
        datetime.strptime(time_str, "%H:%M")
        parts = time_str.split(":")
        h = int(parts[0])
        if min_h <= h <= max_h:
            return True
    except:
        pass
    return False

# ==========================================
# WEBAPP AUTH
# ==========================================
def validate_webapp_data(init_data: str, bot_token: str) -> dict | None:
    try:
        parsed_data = dict(parse_qsl(init_data))
        if "hash" not in parsed_data:
            return None
        
        hash_str = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if calculated_hash == hash_str:
            return json.loads(parsed_data["user"])
        return None
    except Exception as e:
        logging.error(f"WebApp auth error: {e}")
        return None

# ==========================================
# FASTAPI APP & API
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(check_schedules, 'cron', minute='*')
    scheduler.start()
    if not USE_POLLING and WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
        logging.info(f"Webhook установлен на {WEBHOOK_URL}")
        
        # --- НОВОЕ: Устанавливаем кнопку меню слева от поля ввода ---
        from aiogram.types import MenuButtonWebApp
        menu_btn = MenuButtonWebApp(text="📱 Открыть", web_app=WebAppInfo(url=WEBAPP_URL))
        await bot.set_chat_menu_button(menu_button=menu_btn)
        logging.info("Кнопка меню WebApp установлена.")
        # -------------------------------------------------------------
        
    yield
    if not USE_POLLING and WEBHOOK_URL:
        await bot.delete_webhook()
    # Безопасное выключение планировщика
    if scheduler.running:
        scheduler.shutdown()
    if not USE_POLLING and WEBHOOK_URL:
        await bot.delete_webhook()
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

class TaskCreate(BaseModel):
    text: str
    time: str | None = None

class TaskAction(BaseModel):
    task_id: int

@app.get("/webapp", response_class=HTMLResponse)
async def get_webapp():
    return FileResponse("webapp/index.html")

@app.get("/api/me")
async def api_get_me(x_init_data: str = Header(None)):
    user_data = validate_webapp_data(x_init_data, BOT_TOKEN)
    if not user_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    user = get_user(user_data['id'])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return {"name": user[0], "points": user[2], "streak": user[3]}

@app.get("/api/tasks")
async def api_get_tasks(x_init_data: str = Header(None)):
    user_data = validate_webapp_data(x_init_data, BOT_TOKEN)
    if not user_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    user = get_user(user_data['id'])
    user_tz = int(user[1])
    user_time = datetime.utcnow() + timedelta(hours=3 + user_tz)
    today = user_time.strftime("%Y-%m-%d")
    
    tasks = get_all_tasks_today(user_data['id'], today)
    result = []
    for t in tasks:
        result.append({
            "id": t[0],
            "text": t[1],
            "time": t[2],
            "status": t[3],
            "is_main": t[2] is None
        })
    return {"tasks": result}

@app.post("/api/add_task")
async def api_add_task(task: TaskCreate, x_init_data: str = Header(None)):
    user_data = validate_webapp_data(x_init_data, BOT_TOKEN)
    if not user_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    user = get_user(user_data['id'])
    user_tz = int(user[1])
    user_time = datetime.utcnow() + timedelta(hours=3 + user_tz)
    today = user_time.strftime("%Y-%m-%d")
    
    save_task(user_data['id'], today, task.text, task.time)
    return {"status": "ok"}

@app.post("/api/complete_task")
async def api_complete_task(action: TaskAction, x_init_data: str = Header(None)):
    user_data = validate_webapp_data(x_init_data, BOT_TOKEN)
    if not user_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    update_task_status(action.task_id, 'success')
    return {"status": "ok"}

@app.post("/api/fail_task")
async def api_fail_task(action: TaskAction, x_init_data: str = Header(None)):
    user_data = validate_webapp_data(x_init_data, BOT_TOKEN)
    if not user_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    update_task_status(action.task_id, 'fail')
    clear_timer_and_penalty(user_data['id'])
    return {"status": "ok"}

@app.post("/api/submit_day")
async def api_submit_day(x_init_data: str = Header(None)):
    user_data = validate_webapp_data(x_init_data, BOT_TOKEN)
    if not user_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    user_id = user_data['id']
    user = get_user(user_id)
    if not user or not user[6]:
        raise HTTPException(status_code=400, detail="День уже закрыт или таймер не запущен")

    user_tz = int(user[1])
    user_time = datetime.utcnow() + timedelta(hours=3 + user_tz)
    today = user_time.strftime("%Y-%m-%d")
    
    tasks = get_all_tasks_today(user_id, today)
    has_pending = any(t[3] == 'pending' for t in tasks)
    if has_pending:
        raise HTTPException(status_code=400, detail="Есть незакрытые задачи")
        
    has_fail = any(t[3] == 'fail' for t in tasks)
    main_plan = next((t for t in tasks if t[2] is None), None)
    
    if has_fail or not main_plan:
        add_points_and_reset_streak(user_id, 0)
        result_text = "❌ День закрыт. Но ты имеешь проебы или не ставил план утром. Очков нет, стрик сброшен."
    else:
        total_points = 5
        new_streak = user[3] + 1
        bonus_text = ""
        if new_streak == 5:
            total_points += 5
            bonus_text = "\n🔥 Бонус за 5 дней стрика: +5 очков!"
        elif new_streak >= 10 and new_streak % 10 == 0:
            total_points += 10
            bonus_text = f"\n🔥 Бонус за {new_streak} дней стрика: +10 очков!"
            
        add_points_and_increment_streak(user_id, total_points)
        result_text = f"✅ День сдан! +{total_points} очков. {bonus_text}"
    
    if user[9]:
        try:
            await bot.edit_message_text(chat_id=user_id, message_id=user[9], text=f"{result_text}\n\n(Закрыто через мини-аппку)")
        except Exception as e:
            logging.error(f"Не удалось отредактировать сообщение: {e}")

    clear_timer_and_penalty(user_id)
    return {"status": "ok", "message": result_text}

@app.post("/webhook")
async def webhook(request: Request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/ping")
async def ping():
    return {"status": "alive"}

# ==========================================
# КЛАВИАТУРЫ
# ==========================================
def get_main_menu():
    kb = [
        [KeyboardButton(text="📱 Открыть WebApp", web_app=WebAppInfo(url=WEBAPP_URL))],
        [KeyboardButton(text="➕ Добавить задачу"), KeyboardButton(text="📋 Мои задачи")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="🏆 Таблица лидеров")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# ==========================================
# СОСТОЯНИЯ (FSM)
# ==========================================
class RegState(StatesGroup):
    waiting_for_name = State()
    waiting_for_timezone = State()
    waiting_for_morning_time = State()
    waiting_for_evening_time = State()

class TaskState(StatesGroup):
    waiting_for_text = State()
    waiting_for_time = State()

# ==========================================
# ХЕНДЛЕРЫ БОТА
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
    await message.answer("Принято. Теперь укажи свой часовой пояс. Пиши строго со знаком + или - (например: +3, -1, МСК+2):")
    await state.set_state(RegState.waiting_for_timezone)

@router.message(RegState.waiting_for_timezone)
async def process_timezone(message: Message, state: FSMContext):
    tz = get_timezone_offset(message.text.strip())
    if tz is None:
        await message.answer("Пиши нормально в формате '+4' или '-1', или 'МСК+2'. По-другому не приму.")
        return
    await state.update_data(timezone=str(tz))
    await message.answer("Часовой пояс зафиксирован. Теперь скажи, во сколько утром я должен спрашивать твой план? (формат ЧЧ:ММ, строго с 04:00 до 12:00):")
    await state.set_state(RegState.waiting_for_morning_time)

@router.message(RegState.waiting_for_morning_time)
async def process_morning_time(message: Message, state: FSMContext):
    time_str = message.text.strip()
    if not format_time_check(time_str, 4, 12):
        await message.answer("Это не утро. Ставь нормальное время с 04:00 до 12:00 (например, 08:30).")
        return
    await state.update_data(morning_time=time_str)
    await message.answer("Принято. Теперь укажи, во сколько вечером я должен требовать отчет? (формат ЧЧ:ММ, строго с 18:00 до 02:00):")
    await state.set_state(RegState.waiting_for_evening_time)

@router.message(RegState.waiting_for_evening_time)
async def process_evening_time(message: Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        datetime.strptime(time_str, "%H:%M")
        parts = time_str.split(":")
        h = int(parts[0])
        if not (18 <= h <= 23 or 0 <= h <= 2):
            raise ValueError
    except:
        await message.answer("Вечер — это с 18:00 до 02:00. Напиши правильное время (например, 21:00).")
        return

    data = await state.get_data()
    create_user(message.from_user.id, message.from_user.username, data['name'], data['timezone'], data['morning_time'], time_str)
    await state.clear()
    
    await message.answer(f"Всё, {data['name']}. Ты в системе. Завтра в {data['morning_time']} я спрошу твой план.", reply_markup=get_main_menu())

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
    await message.answer("Во сколько напомнить? Напиши время в формате ЧЧ:ММ (например, 18:30):")
    await state.set_state(TaskState.waiting_for_time)

@router.message(TaskState.waiting_for_time)
async def process_task_time(message: Message, state: FSMContext):
    time_input = message.text.strip().upper()
    data = await state.get_data()
    text = data['task_text']
    
    try:
        datetime.strptime(time_input, "%H:%M")
        r_time = time_input
        reply_text = f"Принято. Напомню в <b>{r_time}</b>. Не вздумай слить."
    except ValueError:
        await message.answer("Неправильный формат времени. Напиши как ЧЧ:ММ (например, 18:30).")
        return

    user_tz = int(get_user(message.from_user.id)[1])
    user_time = datetime.utcnow() + timedelta(hours=3 + user_tz)
    today = user_time.strftime("%Y-%m-%d")
    
    save_task(message.from_user.id, today, text, r_time)
    await state.clear()
    await message.answer(reply_text, reply_markup=get_main_menu())

@router.message(F.text == "📋 Мои задачи")
async def show_my_tasks(message: Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start.")
        return

    user_tz = int(user[1])
    user_time = datetime.utcnow() + timedelta(hours=3 + user_tz)
    today = user_time.strftime("%Y-%m-%d")
    
    tasks = get_pending_tasks_today(message.from_user.id, today)
    
    if not tasks:
        await message.answer("На сегодня у тебя нет активных задач. Либо все сделал, либо ничего не планировал.", reply_markup=get_main_menu())
        return
        
    await message.answer("📋 <b>Твои задачи на сегодня:</b>", reply_markup=get_main_menu())
    for task in tasks:
        task_id, text, r_time = task
        time_str = f" (на {r_time})" if r_time else ""
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Сделал", callback_data=f"report_success_{task_id}"),
             InlineKeyboardButton(text="❌ Просрал", callback_data=f"report_fail_{task_id}")]
        ])
        await message.answer(f"🔹 <i>{text}</i>{time_str}", reply_markup=kb)

@router.message(F.text & ~F.text.startswith('/') & ~F.text.startswith('➕') & ~F.text.startswith('👤') & ~F.text.startswith('🏆') & ~F.text.startswith('📋') & ~F.text.startswith('📱'))
async def handle_text(message: Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start.")
        return

    # ИСПРАВЛЕНО! Теперь 10 переменных.
    name, tz, points, streak, m_time, e_time, timer_end_str, p5, pm, last_msg = user
    user_id = message.from_user.id
    user_tz = int(tz)
    
    user_time = datetime.utcnow() + timedelta(hours=3 + user_tz)
    today = user_time.strftime("%Y-%m-%d")
    current_time_str = user_time.strftime("%H:%M")

    if timer_end_str:
        timer_end = datetime.strptime(timer_end_str, "%Y-%m-%d %H:%M")
        if user_time >= timer_end:
            await message.answer("День уже закрыт. Иди спи, завтра работаем.")
            return
        else:
            await message.answer("Ты еще не сдал день! Иди доделывай задачи.")
            return

    is_night = False
    if e_time > m_time:
        if current_time_str >= e_time or current_time_str < m_time:
            is_night = True
    else:
        if current_time_str >= e_time and current_time_str < m_time:
            is_night = True

    if is_night:
        await message.answer("Не пиши хуйни, иди спи, завтра работаем.")
        return

    task = get_main_plan(user_id, today)
    if current_time_str >= m_time and current_time_str < e_time:
        if not task:
            if current_time_str > m_time:
                await message.answer("Утро уже прошло, план не принят. Хочешь добавить задачи — жми кнопку «➕ Добавить задачу».")
                return
            text = message.text.strip()
            if len(text) < 10:
                await message.answer("Это не план. Напиши конкретное действие. Пока не напишешь — я не отстану.")
                return
            save_task(user_id, today, text, r_time=None)
            await message.answer("Принято. Вечером спрошу. Не вздумай слить.", reply_markup=get_main_menu())
            return
    
    if task:
        await message.answer("Иди выполняй дела. Если хочешь добавить задачу, жми ➕ Добавить задачу.")
    else:
        await message.answer("У тебя нет плана на сегодня. Жми ➕ Добавить задачу.")

@router.message(Command("stats"))
@router.message(F.text == "👤 Мой профиль")
async def cmd_stats(message: Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Напиши /start.")
        return
    
    name, tz, points, streak, m_time, e_time, timer, p5, pm, last_msg = user
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'success'", (message.from_user.id,))
        success = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'fail'", (message.from_user.id,))
        fail = cur.fetchone()[0]

    await message.answer(f"📊 <b>Твой отчет, {name}:</b>\n\n✅ Выполнено: {success}\n❌ Просрано: {fail}\n🔥 Стрик: {streak} дней\n💰 Очки: {points}\n\n⏰ Утро: {m_time} | Вечер: {e_time}", reply_markup=get_main_menu())

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

@router.callback_query(F.data.startswith("report_"))
async def handle_report(call: CallbackQuery):
    parts = call.data.split("_")
    action = parts[1]
    
    user_id = call.from_user.id
    user = get_user(user_id)
    if not user:
        await call.answer("Ты кто?")
        return

    # ТУТ ТОЖЕ ИСПРАВЛЕНО НА 10 ПЕРЕМЕННЫХ
    name, tz, points, streak, m_time, e_time, timer_end_str, p5, pm, last_msg = user
    user_tz = int(tz)

    if action == "cancel":
        task_id = int(parts[2])
        update_task_status(task_id, 'canceled')
        await call.message.edit_text(f"{call.message.text}\n\n🚫 <b>ОТМЕНЕНО.</b>")
        await call.answer("Задача отменена.")

    elif action == "success":
        task_id = int(parts[2])
        update_task_status(task_id, 'success')
        await call.message.edit_text(f"{call.message.text}\n\n✅ <b>ВЫПОЛНЕНО.</b>")
        await call.answer("Задача закрыта.")

    elif action == "fail":
        task_id = int(parts[2])
        update_task_status(task_id, 'fail')
        clear_timer_and_penalty(user_id)
        await call.message.edit_text(f"{call.message.text}\n\n❌ <b>ПРОСРАНО.</b> Гатс бы не сдался.")
        await call.answer()

    elif action == "submit_day":
        user_time = datetime.utcnow() + timedelta(hours=3 + user_tz)
        today = user_time.strftime("%Y-%m-%d")
        
        if not timer_end_str:
            await call.message.edit_text("День уже закрыт или таймер не запущен.")
            await call.answer("Поздно.")
            return

        tasks = get_all_tasks_today(user_id, today)
        has_pending = any(t[3] == 'pending' for t in tasks)
        if has_pending:
            await call.answer("У тебя есть незакрытые задачи. Доделай их и жми снова.", show_alert=True)
            return
            
        has_fail = any(t[3] == 'fail' for t in tasks)
        main_plan = next((t for t in tasks if t[2] is None), None)
        
        if has_fail or not main_plan:
            add_points_and_reset_streak(user_id, 0)
            await call.message.edit_text(f"{call.message.text}\n\n❌ <b>День закрыт.</b> Но ты имеешь проебы или не ставил план утром. Очков нет, стрик сброшен.")
            await call.answer("Провал.")
        else:
            bonus_text = ""
            total_points = 5
            new_streak = streak + 1
            
            if new_streak == 5:
                total_points += 5
                bonus_text = "\n🔥 <b>Бонус за 5 дней стрика: +5 очков!</b>"
            elif new_streak >= 10 and new_streak % 10 == 0:
                total_points += 10
                bonus_text = f"\n🔥 <b>Бонус за {new_streak} дней стрика: +10 очков!</b>"
                
            add_points_and_increment_streak(user_id, total_points)
            clear_timer_and_penalty(user_id)
            await call.message.edit_text(f"{call.message.text}\n\n✅ <b>День сдан!</b> +{total_points} очков. {bonus_text}\n\nОтдыхай, воин.")
            await call.answer("Успех!")

# ==========================================
# ПЛАНИРОВЩИК (CRON)
# ==========================================
async def check_schedules():
    now_utc = datetime.utcnow()
    
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT user_id, name, timezone, points, streak, morning_time, evening_time, timer_end, penalty_5min_sent, penalty_morning_sent FROM users")
        users = cur.fetchall()

    for user in users:
        user_id, name, tz_str, points, streak, m_time, e_time, timer_end_str, p5, pm = user
        offset = int(tz_str)
        
        user_time = now_utc + timedelta(hours=3 + offset)
        current_time_str = user_time.strftime("%H:%M")
        today = user_time.strftime("%Y-%m-%d")
        
        try:
            if current_time_str == m_time:
                task = get_main_plan(user_id, today)
                if not task:
                    await bot.send_message(user_id, f"<b>{name}</b>, {random.choice(MORNING_PHRASES)}", reply_markup=get_main_menu())
            
            target_morning_penalty = (datetime.strptime(m_time, "%H:%M") + timedelta(hours=1)).strftime("%H:%M")
            if current_time_str == target_morning_penalty and pm == 0:
                task = get_main_plan(user_id, today)
                if not task:
                    add_points_and_reset_streak(user_id, -5)
                    with db_connect() as con:
                        cur = con.cursor()
                        cur.execute("UPDATE users SET penalty_morning_sent = 1 WHERE user_id = ?", (user_id,))
                        con.commit()
                    await bot.send_message(user_id, "Ты проигнорировал меня утром. Это минус. Очки сняты, стрик сброшен.")

            if current_time_str == e_time:
                task = get_main_plan(user_id, today)
                all_tasks = get_all_tasks_today(user_id, today)
                pending = [t for t in all_tasks if t[3] == 'pending']
                
                if task or pending:
                    timer_end_dt = user_time + timedelta(hours=2)
                    set_timer(user_id, timer_end_dt.strftime("%Y-%m-%d %H:%M"))
                    
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Сдать день в чате", callback_data="report_submit_day")],
                        [InlineKeyboardButton(text="📱 Сдать в WebApp", web_app=WebAppInfo(url=WEBAPP_URL))]
                    ])
                    
                    msg = f"Твой вечерний отчет.\n"
                    if pending:
                        msg += f"У тебя есть незакрытые задачи!\n"
                        for p in pending:
                            msg += f"🔹 {p[1]}\n"
                        msg += "\nДоделай их до конца 2 часов. Напоминаю: без утреннего плана очков не будет.\n"
                    else:
                        msg += "Все задачи закрыты. Жми «Сдать день».\n"
                    
                    sent_msg = await bot.send_message(user_id, msg, reply_markup=kb)
                    
                    with db_connect() as con:
                        cur = con.cursor()
                        cur.execute("UPDATE users SET last_report_msg_id = ? WHERE user_id = ?", (sent_msg.message_id, user_id))
                        con.commit()
            
            if timer_end_str:
                timer_end = datetime.strptime(timer_end_str, "%Y-%m-%d %H:%M")
                
                if user_time >= timer_end - timedelta(minutes=5) and user_time < timer_end and p5 == 0:
                    with db_connect() as con:
                        cur = con.cursor()
                        cur.execute("UPDATE users SET penalty_5min_sent = 1 WHERE user_id = ?", (user_id,))
                        con.commit()
                    
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Сдать день в чате", callback_data="report_submit_day")],
                        [InlineKeyboardButton(text="📱 Сдать в WebApp", web_app=WebAppInfo(url=WEBAPP_URL))]
                    ])
                    await bot.send_message(user_id, "Осталось 5 минут. Если не будет выполнено — я сбрасываю стрики и не даю очки. Ебашь.", reply_markup=kb)
                
                elif user_time >= timer_end:
                    pending = get_pending_tasks_today(user_id, today)
                    for p in pending:
                        update_task_status(p[0], 'fail')
                        
                    add_points_and_reset_streak(user_id, -5)
                    clear_timer_and_penalty(user_id)
                    await bot.send_message(user_id, "Не все задачи были выполнены, не проеби день завтра. Доброй ночи. (-5 очков, стрик сброшен)")

            pending_micro = get_pending_tasks_today(user_id, today)
            for mt in pending_micro:
                task_id, task_text, r_time = mt
                if r_time == current_time_str:
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Сделал", callback_data=f"report_success_{task_id}"),
                         InlineKeyboardButton(text="❌ Просрал", callback_data=f"report_fail_{task_id}")]
                    ])
                    await bot.send_message(user_id, f"⏰ <b>Пора делать:</b>\n<i>{task_text}</i>\n\nСделал?", reply_markup=kb)

        except Exception as e:
            logging.error(f"Ошибка при отправке юзеру {user_id}: {e}")

# ==========================================
# ЗАПУСК
# ==========================================
if __name__ == "__main__":
    if USE_POLLING:
        logging.info("Запуск в режиме POLLING.")
        async def run_polling():
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot)
        asyncio.run(run_polling())
    else:
        logging.info("Запуск в режиме WEBHOOK.")
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))