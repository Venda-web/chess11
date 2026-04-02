import asyncio
import json
import logging
import sqlite3
import random
import string
from datetime import datetime
from typing import Dict, Optional, List, Any

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiohttp

# ========== КОНФИГУРАЦИЯ ==========
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # Замените на токен вашего бота от @BotFather
SUPERUSER_IDS = [123456789, 987654321]  # Сюда вставьте ваш Telegram ID (суперюзер)
WEBSITE_URL = "https://your-chess-site.com"  # URL вашего шахматного сайта
API_SECRET = "super_secret_key_change_me_12345"  # Секретный ключ для API

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

# ========== ИНИЦИАЛИЗАЦИЯ БД ==========
def init_db():
    conn = sqlite3.connect("chess_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            is_superuser INTEGER DEFAULT 0,
            registered_at TEXT,
            games_played INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS game_sessions (
            session_id TEXT PRIMARY KEY,
            creator_id INTEGER,
            opponent_id INTEGER,
            game_state TEXT,
            created_at TEXT,
            status TEXT DEFAULT 'waiting'
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            action TEXT,
            target_id INTEGER,
            timestamp TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_invites (
            invite_code TEXT PRIMARY KEY,
            creator_id INTEGER,
            session_id TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ========== FSM СОСТОЯНИЯ ==========
class GameStates(StatesGroup):
    waiting_for_opponent = State()
    waiting_for_move = State()
    creating_invite = State()

class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_user_id = State()

# ========== ИНИЦИАЛИЗАЦИЯ БОТА ==========
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def is_superuser(telegram_id: int) -> bool:
    return telegram_id in SUPERUSER_IDS

def add_user(telegram_id: int, username: str = "", first_name: str = ""):
    conn = sqlite3.connect("chess_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO users (telegram_id, username, first_name, registered_at, is_superuser)
        VALUES (?, ?, ?, ?, ?)
    """, (telegram_id, username, first_name, datetime.now().isoformat(), 1 if is_superuser(telegram_id) else 0))
    conn.commit()
    conn.close()

def generate_invite_code(creator_id: int, session_id: str) -> str:
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    conn = sqlite3.connect("chess_bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO pending_invites (invite_code, creator_id, session_id, created_at) VALUES (?, ?, ?, ?)",
                   (code, creator_id, session_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return code

def get_session_by_code(code: str) -> Optional[Dict]:
    conn = sqlite3.connect("chess_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT session_id, creator_id FROM pending_invites WHERE invite_code = ?", (code,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"session_id": row[0], "creator_id": row[1]}
    return None

def delete_invite_code(code: str):
    conn = sqlite3.connect("chess_bot.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM pending_invites WHERE invite_code = ?", (code,))
    conn.commit()
    conn.close()

def get_user_stats(telegram_id: int) -> Dict:
    conn = sqlite3.connect("chess_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT games_played, wins FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"games_played": row[0], "wins": row[1]}
    return {"games_played": 0, "wins": 0}

def increment_games(telegram_id: int, is_win: bool = False):
    conn = sqlite3.connect("chess_bot.db")
    cursor = conn.cursor()
    if is_win:
        cursor.execute("UPDATE users SET games_played = games_played + 1, wins = wins + 1 WHERE telegram_id = ?", (telegram_id,))
    else:
        cursor.execute("UPDATE users SET games_played = games_played + 1 WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()

def log_admin_action(admin_id: int, action: str, target_id: int = None):
    conn = sqlite3.connect("chess_bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO admin_logs (admin_id, action, target_id, timestamp) VALUES (?, ?, ?, ?)",
                   (admin_id, action, target_id, datetime.now().isoformat()))
    conn.commit()
    conn.close()

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🎮 Новая игра", callback_data="new_game")],
        [InlineKeyboardButton(text="🔗 Присоединиться по коду", callback_data="join_game")],
        [InlineKeyboardButton(text="📊 Моя статистика", callback_data="my_stats")],
        [InlineKeyboardButton(text="🌐 Открыть сайт", url=WEBSITE_URL)]
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="👑 Дать права суперюзера", callback_data="admin_add_superuser")],
        [InlineKeyboardButton(text="📋 Список пользователей", callback_data="admin_list_users")],
        [InlineKeyboardButton(text="📜 Логи администратора", callback_data="admin_logs")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
    ])

# ========== ОБРАБОТЧИКИ КОМАНД ==========
@dp.message(Command("start"))
async def cmd_start(message: Message):
    add_user(message.from_user.id, message.from_user.username or "", message.from_user.first_name or "")
    is_admin = is_superuser(message.from_user.id)
    await message.answer(
        f"♟️ Добро пожаловать в шахматный бот, {message.from_user.first_name}!\n\n"
        f"Здесь вы можете:\n"
        f"• Создавать игры и приглашать друзей по коду\n"
        f"• Играть прямо на нашем шахматном сайте\n"
        f"• Отслеживать свою статистику\n\n"
        f"⬇️ Используйте кнопки ниже для навигации:",
        reply_markup=get_main_keyboard(is_admin)
    )

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_superuser(message.from_user.id):
        await message.answer("⛔ У вас нет прав администратора.")
        return
    await message.answer("🔐 Панель управления администратора:", reply_markup=get_admin_keyboard())

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    is_admin = is_superuser(callback.from_user.id)
    await callback.message.edit_text(
        "♟️ Главное меню:",
        reply_markup=get_main_keyboard(is_admin)
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if not is_superuser(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text("⚙️ Административная панель:", reply_markup=get_admin_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "my_stats")
async def my_stats(callback: CallbackQuery):
    stats = get_user_stats(callback.from_user.id)
    await callback.message.edit_text(
        f"📈 *Ваша статистика*\n\n"
        f"🎮 Сыграно партий: {stats['games_played']}\n"
        f"🏆 Побед: {stats['wins']}\n"
        f"📊 Процент побед: {round(stats['wins']/stats['games_played']*100, 1) if stats['games_played'] > 0 else 0}%\n\n"
        f"💡 Играйте больше, чтобы улучшить результат!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "new_game")
async def new_game(callback: CallbackQuery):
    session_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))
    invite_code = generate_invite_code(callback.from_user.id, session_id)
    
    conn = sqlite3.connect("chess_bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO game_sessions (session_id, creator_id, created_at, status) VALUES (?, ?, ?, ?)",
                   (session_id, callback.from_user.id, datetime.now().isoformat(), "waiting"))
    conn.commit()
    conn.close()
    
    game_link = f"{WEBSITE_URL}?session={session_id}&code={invite_code}"
    
    await callback.message.edit_text(
        f"🎲 *Новая игра создана!*\n\n"
        f"🔗 *Код для приглашения:* `{invite_code}`\n"
        f"🌐 *Ссылка на игру:* [Нажмите чтобы играть]({game_link})\n\n"
        f"📤 Отправьте код или ссылку другу, чтобы он присоединился.\n"
        f"⏳ Ожидание оппонента...",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Отменить создание", callback_data="cancel_game")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "join_game")
async def join_game_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🔐 *Введите код приглашения:*\n\n"
        "Код должен состоять из 8 символов (буквы и цифры).\n"
        "Пример: `A7B3K9M2`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="back_to_main")]])
    )
    await state.set_state(GameStates.waiting_for_opponent)
    await callback.answer()

@dp.message(GameStates.waiting_for_opponent)
async def process_join_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    session_data = get_session_by_code(code)
    
    if not session_data:
        await message.answer("❌ Неверный код приглашения. Попробуйте ещё раз или создайте новую игру.")
        return
    
    if session_data["creator_id"] == message.from_user.id:
        await message.answer("❌ Вы не можете присоединиться к своей собственной игре.")
        await state.clear()
        return
    
    game_link = f"{WEBSITE_URL}?session={session_data['session_id']}&join=1"
    
    await message.answer(
        f"✅ *Вы присоединились к игре!*\n\n"
        f"🌐 *Ссылка для игры:* [Нажмите чтобы играть]({game_link})\n\n"
        f"💡 Игра начнётся, когда оба игрока перейдут по ссылке.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(is_superuser(message.from_user.id))
    )
    
    delete_invite_code(code)
    await state.clear()

@dp.callback_query(F.data == "cancel_game")
async def cancel_game(callback: CallbackQuery):
    await callback.message.edit_text("❌ Создание игры отменено.", reply_markup=get_main_keyboard(is_superuser(callback.from_user.id)))
    await callback.answer()

# ========== АДМИН-ФУНКЦИИ ==========
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_superuser(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text(
        "📢 *Режим рассылки*\n\n"
        "Введите сообщение для рассылки всем пользователям бота:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_panel")]])
    )
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.answer()

@dp.message(AdminStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if not is_superuser(message.from_user.id):
        await message.answer("⛔ Доступ запрещён")
        await state.clear()
        return
    
    conn = sqlite3.connect("chess_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id FROM users")
    users = cursor.fetchall()
    conn.close()
    
    success_count = 0
    for user in users:
        try:
            await bot.send_message(user[0], f"📢 *Объявление от администрации:*\n\n{message.text}", parse_mode="Markdown")
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение {user[0]}: {e}")
    
    log_admin_action(message.from_user.id, "broadcast", None)
    
    await message.answer(f"✅ Рассылка завершена! Отправлено {success_count} пользователям.")
    await state.clear()
    await message.answer("⚙️ Админ-панель:", reply_markup=get_admin_keyboard())

@dp.callback_query(F.data == "admin_add_superuser")
async def add_superuser_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_superuser(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text(
        "👑 *Выдача прав суперюзера*\n\n"
        "Введите Telegram ID пользователя, которому хотите дать права:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_panel")]])
    )
    await state.set_state(AdminStates.waiting_for_user_id)
    await callback.answer()

@dp.message(AdminStates.waiting_for_user_id)
async def process_add_superuser(message: Message, state: FSMContext):
    if not is_superuser(message.from_user.id):
        await message.answer("⛔ Доступ запрещён")
        await state.clear()
        return
    
    try:
        target_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Неверный формат. Введите числовой ID.")
        return
    
    conn = sqlite3.connect("chess_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_superuser = 1 WHERE telegram_id = ?", (target_id,))
    if cursor.rowcount == 0:
        cursor.execute("INSERT INTO users (telegram_id, is_superuser, registered_at) VALUES (?, ?, ?)",
                       (target_id, 1, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
    log_admin_action(message.from_user.id, "add_superuser", target_id)
    
    await message.answer(f"✅ Пользователь с ID {target_id} теперь является суперпользователем!")
    await state.clear()
    await message.answer("⚙️ Админ-панель:", reply_markup=get_admin_keyboard())

@dp.callback_query(F.data == "admin_list_users")
async def list_users(callback: CallbackQuery):
    if not is_superuser(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    conn = sqlite3.connect("chess_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, first_name, games_played, wins, is_superuser FROM users ORDER BY games_played DESC LIMIT 20")
    users = cursor.fetchall()
    conn.close()
    
    if not users:
        await callback.message.edit_text("📋 Список пользователей пуст.")
        return
    
    text = "📋 *Топ-20 пользователей:*\n\n"
    for u in users:
        text += f"• {u[2] or u[1] or u[0]} [ID: {u[0]}]\n"
        text += f"  🎮 {u[3]} игр | 🏆 {u[4]} побед | {'👑 Админ' if u[5] else '👤 Игрок'}\n\n"
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]]))
    await callback.answer()

@dp.callback_query(F.data == "admin_logs")
async def show_admin_logs(callback: CallbackQuery):
    if not is_superuser(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    conn = sqlite3.connect("chess_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT admin_id, action, target_id, timestamp FROM admin_logs ORDER BY id DESC LIMIT 30")
    logs = cursor.fetchall()
    conn.close()
    
    if not logs:
        await callback.message.edit_text("📜 Логи отсутствуют.")
        return
    
    text = "📜 *Последние действия администраторов:*\n\n"
    for log in logs:
        text += f"• Админ {log[0]} | {log[1]}"
        if log[2]:
            text += f" | цель: {log[2]}"
        text += f"\n  🕐 {log[3][:19]}\n\n"
    
    await callback.message.edit_text(text[:4000], parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]]))
    await callback.answer()

# ========== WEBHOOK ДЛЯ ИНТЕГРАЦИИ С САЙТОМ ==========
@dp.message(Command("get_id"))
async def cmd_get_id(message: Message):
    await message.answer(f"🆔 Ваш Telegram ID: `{message.from_user.id}`", parse_mode="Markdown")

# ========== ЗАПУСК БОТА ==========
async def main():
    logger.info("Бот запускается...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    print("=" * 50)
    print("♟️ ШАХМАТНЫЙ ТЕЛЕГРАМ БОТ")
    print(f"🤖 Суперюзеры: {SUPERUSER_IDS}")
    print("⚠️ НЕ ЗАБУДЬТЕ ЗАМЕНИТЬ BOT_TOKEN и SUPERUSER_IDS!")
    print("=" * 50)
    asyncio.run(main())