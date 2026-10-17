import asyncio
import logging
import sqlite3
import os
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Update
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import uvicorn

# ==========================================
# НАСТРОЙКИ
# ==========================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_ЗДЕСЬ") # ЗАМЕНИ НА СВОЙ
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "") # Заполним на Render
DB_PATH = "kicker.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

scheduler = AsyncIOScheduler()

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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                date TEXT,
                plan_text TEXT,
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

def save_task(user_id, date_str, text):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("INSERT INTO tasks (user_id, date, plan_text) VALUES (?, ?, ?)", (user_id, date_str, text))
        con.commit()

def get_pending_task(user_id, date_str):
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT id, plan_text, status FROM tasks WHERE user_id = ? AND date = ?", (user_id, date_str))
        return cur.fetchone()

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
# СОСТОЯНИЯ (FSM)
# ==========================================
class RegState(StatesGroup):
    waiting_for_name = State()
    waiting_for_timezone = State()

# ==========================================
# ХЕНДЛЕРЫ
# ==========================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if user:
        await message.answer(f"Ты уже в системе, {user[0]}. Не расслабляйся. /stats — твой прогресс, /top — лидеры.")
        return
    
    await message.answer("Слушай сюда. Я твой новый командир. Здесь не будет нюней и соплей. С этого дня ты будешь делать то, что запланировал. Начнем с базы.\n\nКак к тебе обращаться по-мужски?")
    await state.set_state(RegState.waiting_for_name)

@router.message(RegState.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Принято. Теперь укажи свой часовой пояс относительно Москвы (например, МСК+0, МСК+2, МСК-1).\n\nЭто нужно, чтобы я будил тебя ровно в 09:00 и требовал отчет в 21:00.")
    await state.set_state(RegState.waiting_for_timezone)

@router.message(RegState.waiting_for_timezone)
async def process_timezone(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data['name']
    tz_str = message.text.strip()
    offset = get_timezone_offset(tz_str)
    
    create_user(message.from_user.id, message.from_user.username, name, str(offset))
    await state.clear()
    
    await message.answer(f"Всё, {name}. Ты в системе. Завтра в 09:00 я спрошу твой план. Пока свободен. Но помни: я слежу.")

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Ты кто такой? Напиши /start.")
        return
    
    name, tz, points, streak = user
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'success'", (message.from_user.id,))
        success = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'fail'", (message.from_user.id,))
        fail = cur.fetchone()[0]

    await message.answer(f"📊 <b>Твой отчет, {name}:</b>\n\n✅ Выполнено: {success}\n❌ Просрано: {fail}\n🔥 Стрик: {streak} дней\n💰 Очки: {points}")

@router.message(Command("top"))
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
    
    await message.answer(text)

@router.message(F.text & ~F.text.startswith('/'))
async def handle_plan(message: Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    task = get_pending_task(message.from_user.id, today)
    
    if task:
        await message.answer("Ты уже сдал план на сегодня. Вечером спрошу. Не вздумай слить.")
        return

    text = message.text.strip()
    if len(text) < 10:
        await message.answer("Это не план. Напиши конкретное действие. Пока не напишешь — я не отстану.")
        return

    save_task(message.from_user.id, today, text)
    await message.answer("Принято. Вечером спрошу. Не вздумай слить.")

@router.callback_query(F.data.startswith("report_"))
async def handle_report(call: CallbackQuery):
    action = call.data.split("_")[1]
    user_id = call.from_user.id
    user = get_user(user_id)
    
    if not user:
        await call.answer("Ты кто?")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    task = get_pending_task(user_id, today)
    
    if not task:
        await call.answer("Нет активной задачи на сегодня.")
        return
    
    task_id, plan_text, status = task
    
    if status != 'pending':
        await call.answer("Ты уже отчитался. Не жми кнопки зря.", show_alert=True)
        return

    if action == "success":
        update_task_status(task_id, 'success')
        with db_connect() as con:
            cur = con.cursor()
            cur.execute("UPDATE users SET points = points + 10, streak = streak + 1 WHERE user_id = ?", (user_id,))
            con.commit()
        await call.message.edit_text(f"Неплохо. Завтра планка выше. (+10 очков)")
    else:
        update_task_status(task_id, 'fail')
        with db_connect() as con:
            cur = con.cursor()
            cur.execute("UPDATE users SET points = points - 5, streak = 0 WHERE user_id = ?", (user_id,))
            con.commit()
        await call.message.edit_text(f"Ты меня разочаровал. Гатс бы не сдался. Завтра исправишься. (-5 очков)")

# ==========================================
# ПЛАНИРОВЩИК (CRON)
# ==========================================
async def check_schedules():
    now_utc = datetime.utcnow()
    
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT user_id, name, timezone, points FROM users")
        users = cur.fetchall()

    for user in users:
        user_id, name, tz_str, points = user
        offset = int(tz_str)
        
        user_time = now_utc + timedelta(hours=3 + offset)
        current_time_str = user_time.strftime("%H:%M")
        today = user_time.strftime("%Y-%m-%d")
        
        if current_time_str == "09:00":
            await bot.send_message(user_id, f"<b>{name}</b>, вставай. Какой план на сегодня? Напиши текстом, что ты <b>ОБЯЗАН</b> сделать сегодня.")
        elif current_time_str == "21:00":
            task = get_pending_task(user_id, today)
            if task and task[2] == 'pending':
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Сделал", callback_data="report_success"),
                     InlineKeyboardButton(text="❌ Просрал", callback_data="report_fail")]
                ])
                await bot.send_message(user_id, f"Твой план был:\n<i>{task[1]}</i>\n\nОтчитывайся. Сделал?", reply_markup=kb)
        elif current_time_str == "10:00":
            task = get_pending_task(user_id, today)
            if not task:
                await bot.send_message(user_id, "Ты проигнорировал меня. Это минус. Сдулся?")
        elif current_time_str == "23:00":
            task = get_pending_task(user_id, today)
            if task and task[2] == 'pending':
                update_task_status(task[0], 'fail')
                with db_connect() as con:
                    cur = con.cursor()
                    cur.execute("UPDATE users SET points = points - 5, streak = 0 WHERE user_id = ?", (user_id,))
                    con.commit()
                await bot.send_message(user_id, "Ты не отчитался. Считаю, что ты сдулся. Завтра докажи, что ты не тряпка.")

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

# ==========================================
# ЗАПУСК (ЛОКАЛЬНО)
# ==========================================
if __name__ == "__main__":
    # Локально можно запускать через uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))