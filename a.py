import logging
import os
import json
import time
import uuid
import threading
from datetime import datetime
import re

import telebot
from telebot import types
from flask import Flask
from pymongo import MongoClient

TOKEN = "8967848819:AAEoy23NSIYQR2ne7efvPLJ3CuWg4kyKewI"
ADMIN_PASSWORD = "mirrexworkforyou"
MONGO_URL = "mongodb+srv://chernovfedor784_db_user:cSZDTtyFCuVbC2NR@cluster0.a8gt5bk.mongodb.net/?retryWrites=true&w=majority"

# ========== MONGODB ==========
if MONGO_URL:
    try:
        mongo_client = MongoClient(
            MONGO_URL,
            serverSelectionTimeoutMS=5000,
            tls=True,
            tlsAllowInvalidCertificates=True,
        )
        mongo_client.admin.command("ping")
        db = mongo_client["telegram_bot"]
        users_collection = db["users"]
        orders_collection = db["orders"]
        admins_collection = db["admins"]
        banned_collection = db["banned"]
        USING_MONGO = True
        print("✅ Подключено к MongoDB!")
    except Exception as e:
        USING_MONGO = False
        print(f"⚠️ MongoDB не настроен или недоступен: {e}")
else:
    USING_MONGO = False
    print("⚠️ MongoDB не настроен")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)

bot = telebot.TeleBot(TOKEN)

# ========== FLASK ДЛЯ RENDER ==========
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health():
    return "OK", 200


def run_flask():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_flask, daemon=True).start()

try:
    BOT_INFO = bot.get_me()
    BOT_USERNAME = BOT_INFO.username if BOT_INFO and getattr(BOT_INFO, "username", None) else ""
except Exception:
    BOT_USERNAME = ""

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "bot_data.json")

ADMIN_IDS = set()
USER_DATA = {}
ORDERS = {}
CURRENT_CHAT = {}
BANNED_USERS = set()
PENDING_ORDERS = {}
ADMIN_LOGIN_PENDING = set()

LAST_MESSAGE_TS = {}
RATE_LIMIT_SECONDS = 2

def is_rate_limited(user_id):
    now = time.time()
    last = LAST_MESSAGE_TS.get(str(user_id), 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    LAST_MESSAGE_TS[str(user_id)] = now
    return False

PRICES = {
    "ranked": {
        "Алмаз": 100,
        "Мифик": 500,
        "Лега": 700,
        "Мастер": 1000,
    },
    "trophies": {
        "10к": 50,
        "20к": 200,
        "30к": 300,
        "40к": 350,
        "50к": 400,
        "60к": 400,
        "70к": 500,
        "90к": 600,
        "100к+": "700-800",
    },
    "character": {
        "1к": 20,
        "5к": 50,
        "10к": 200,
        "20к": 400,
        "30к": 500,
        "40к": 600,
        "50к": "700-800",
    },
}

CATEGORY_NAMES = {
    "ranked": "Ранкед",
    "trophies": "Трофеи",
    "character": "Персонаж",
}


def load_data():
    global ADMIN_IDS, USER_DATA, ORDERS, BANNED_USERS
    try:
        if USING_MONGO:
            admin_doc = admins_collection.find_one({"_id": "state"}) or {}
            ADMIN_IDS = set(admin_doc.get("admin_ids", []))

            USER_DATA = {}
            for doc in users_collection.find({}):
                user_id = str(doc.get("_id"))
                USER_DATA[user_id] = doc.get("data", {})

            ORDERS = {}
            for doc in orders_collection.find({}):
                order_id = str(doc.get("_id"))
                ORDERS[order_id] = doc.get("data", {})

            BANNED_USERS = set(
                str(doc.get("_id")) for doc in banned_collection.find({}) if doc.get("_id") is not None
            )
            print(f"✅ Загружено заказов: {len(ORDERS)}, пользователей: {len(USER_DATA)}")
            return

        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            ADMIN_IDS = set(data.get("admin_ids", []))
            USER_DATA = data.get("user_data", {})
            ORDERS = data.get("orders", {})
            BANNED_USERS = set(data.get("banned_users", []))
            print(f"✅ Загружено заказов: {len(ORDERS)}, пользователей: {len(USER_DATA)}")
    except Exception as e:
        print(f"Load error: {e}")


def save_data():
    try:
        if USING_MONGO:
            admins_collection.delete_many({})
            admins_collection.insert_one({"_id": "state", "admin_ids": sorted(ADMIN_IDS)})

            users_collection.delete_many({})
            for user_id, user_data in USER_DATA.items():
                users_collection.insert_one({"_id": str(user_id), "data": user_data})

            orders_collection.delete_many({})
            for order_id, order_data in ORDERS.items():
                orders_collection.insert_one({"_id": str(order_id), "data": order_data})

            banned_collection.delete_many({})
            for user_id in sorted(BANNED_USERS):
                banned_collection.insert_one({"_id": str(user_id)})
            return

        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "admin_ids": sorted(ADMIN_IDS),
                    "user_data": USER_DATA,
                    "orders": ORDERS,
                    "banned_users": sorted(BANNED_USERS),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        print(f"Save error: {e}")


load_data()


def get_user_display(user_id):
    user_id = str(user_id)
    if user_id in USER_DATA:
        info = USER_DATA[user_id]
        username = info.get("username", "")
        first_name = info.get("first_name", "")
        if username and username != "No username":
            return f"@{username}"
        if first_name:
            return first_name
    return f"ID: {user_id}"


def get_user_full(user_id):
    user_id = str(user_id)
    if user_id in USER_DATA:
        info = USER_DATA[user_id]
        username = info.get("username", "")
        first_name = info.get("first_name", "")
        if username and username != "No username":
            return f"@{username} (ID: {user_id})"
        if first_name:
            return f"{first_name} (ID: {user_id})"
    return f"ID: {user_id}"


def init_user(user_id, username=None, first_name=None):
    user_id = str(user_id)
    if user_id not in USER_DATA:
        USER_DATA[user_id] = {
            "username": username or "No username",
            "first_name": first_name or "",
            "messages": [],
            "orders": [],
        }
        save_data()


def record_user_message(user_id, message):
    user_id = str(user_id)
    init_user(user_id)
    if user_id in ADMIN_IDS:
        return

    user_messages = USER_DATA.setdefault(user_id, {}).setdefault("messages", [])
    user_messages.append(
        {
            "text": message.text or message.caption or f"[{message.content_type}]",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "from_user": True,
            "content_type": message.content_type,
        }
    )
    if len(user_messages) > 40:
        USER_DATA[user_id]["messages"] = user_messages[-40:]
    save_data()


def build_user_history_text(user_id, limit=8):
    user_id = str(user_id)
    messages = USER_DATA.get(user_id, {}).get("messages", [])
    if not messages:
        return "📝 История сообщений: пока пусто"

    recent_messages = messages[-limit:]
    lines = ["📝 Последние сообщения:"]
    for item in recent_messages:
        text = str(item.get("text", "")).replace("\n", " ")
        if len(text) > 80:
            text = text[:77] + "..."
        timestamp = item.get("timestamp", "")
        lines.append(f"• {timestamp} — {text}")
    return "\n".join(lines)


def generate_order_id():
    return str(uuid.uuid4())[:8]


def format_price(price):
    return f"{price}₽" if isinstance(price, (int, float)) else str(price)


def is_admin(user_id):
    return str(user_id) in ADMIN_IDS


def is_admin_login_pending(user_id):
    return str(user_id) in ADMIN_LOGIN_PENDING


def send_main_menu(user_id, chat_id):
    bot.send_message(chat_id, "Главное меню:", reply_markup=get_main_menu(user_id))


def send_admin_menu(chat_id):
    bot.send_message(chat_id, "Админ панель", reply_markup=get_admin_menu())


def close_user_chat(user_id):
    CURRENT_CHAT.pop(str(user_id), None)


def set_default_commands():
    commands = [
        telebot.types.BotCommand("start", "Начать работу с ботом"),
        telebot.types.BotCommand("menu", "Открыть главное меню"),
        telebot.types.BotCommand("help", "Показать справку"),
        telebot.types.BotCommand("orders", "Показать мои заказы"),
        telebot.types.BotCommand("support", "Связаться с поддержкой"),
    ]
    try:
        bot.set_my_commands(commands)
    except Exception as e:
        logging.warning(f"Не удалось установить системные команды: {e}")


def format_order_text(order_id, order):
    emoji, status_text = get_status_info(order.get("status", "pending"))
    price = format_price(order.get("price", 0))
    return (
        f"🆔 {order_id}\n"
        f"📋 {order.get('service', '')}\n"
        f"💰 {price}\n"
        f"📊 {emoji} {status_text}\n"
        f"📅 {order.get('created_at', '')}\n"
        f"📝 Данные: {order.get('data', 'Не указаны')}"
    )


def safe_edit_message_text(call, text, reply_markup=None, parse_mode=None):
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=reply_markup, parse_mode=parse_mode)
        return
    except telebot.apihelper.ApiTelegramException as e:
        if "there is no text in the message to edit" in str(e):
            try:
                bot.edit_message_caption(text, call.message.chat.id, call.message.message_id, reply_markup=reply_markup, parse_mode=parse_mode)
                return
            except Exception:
                pass
        raise
    except Exception:
        try:
            bot.edit_message_caption(text, call.message.chat.id, call.message.message_id, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        except Exception:
            pass


def get_status_info(status):
    return {
        "pending": ("⏳", "Ожидает"),
        "accepted": ("🟡", "Принят"),
        "working": ("🔵", "В работе"),
        "completed": ("✅", "Выполнен"),
        "cancelled": ("❌", "Отменён"),
    }.get(status, ("📌", status or "Неизвестно"))


# ===== Клавиатуры =====
def get_user_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton("📋 Услуги"),
        types.KeyboardButton("🛒 Мои заказы"),
        types.KeyboardButton("ℹ️ О нас"),
        types.KeyboardButton("💬 Поддержка"),
        types.KeyboardButton("❓ Как пользоваться"),
    )
    return markup


def get_pending_order_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("❌ Отменить заказ", callback_data="cancel_pending"))
    return markup


def get_pending_order_keyboard_for(owner_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_pending:{owner_id}"))
    return markup


def get_confirm_order_keyboard(owner_id=None):
    markup = types.InlineKeyboardMarkup(row_width=2)
    if owner_id:
        markup.add(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_pending:{owner_id}"),
            types.InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_pending:{owner_id}"),
        )
    else:
        markup.add(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_pending"),
            types.InlineKeyboardButton("❌ Отменить", callback_data="cancel_pending"),
        )
    return markup


def get_add_more_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("➕ Добавить ещё", callback_data="add_more"),
        types.InlineKeyboardButton("✅ Оформить заказ", callback_data="confirm_pending"),
        types.InlineKeyboardButton("❌ Отменить заказ", callback_data="cancel_pending"),
    )
    return markup


def get_add_more_keyboard_for(owner_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("➕ Добавить ещё", callback_data=f"add_more:{owner_id}"),
        types.InlineKeyboardButton("✅ Оформить заказ", callback_data=f"confirm_pending:{owner_id}"),
        types.InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_pending:{owner_id}"),
    )
    return markup


def get_admin_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton("📊 Заказы"),
        types.KeyboardButton("👤 Пользователи"),
    )
    markup.add(
        types.KeyboardButton("📋 Главное меню"),
        types.KeyboardButton("🚪 Выйти из админки"),
    )
    return markup


def get_main_menu(user_id):
    return get_user_menu()


def get_services_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🏆 Ранкед", callback_data="service_ranked"),
        types.InlineKeyboardButton("📊 Общие трофеи", callback_data="service_trophies"),
        types.InlineKeyboardButton("🎯 Один персонаж", callback_data="service_character"),
        types.InlineKeyboardButton("🔙 Назад", callback_data="back_menu"),
    )
    return markup


def get_ranked_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    for rank, price in PRICES["ranked"].items():
        markup.add(types.InlineKeyboardButton(f"{rank} — {format_price(price)}", callback_data=f"order_ranked_{rank}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_services"))
    return markup


def get_trophies_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    for trophy, price in PRICES["trophies"].items():
        markup.add(types.InlineKeyboardButton(f"{trophy} — {format_price(price)}", callback_data=f"order_trophies_{trophy}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_services"))
    return markup


def get_character_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    for trophy, price in PRICES["character"].items():
        markup.add(types.InlineKeyboardButton(f"{trophy} — {format_price(price)}", callback_data=f"order_character_{trophy}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_services"))
    return markup


def get_orders_keyboard(orders_list):
    markup = types.InlineKeyboardMarkup(row_width=1)
    if not orders_list:
        markup.add(types.InlineKeyboardButton("❌ Нет заказов", callback_data="none"))
        return markup

    for order_id in orders_list:
        order = ORDERS.get(order_id, {})
        if not order:
            continue
        emoji, text = get_status_info(order.get("status", "pending"))
        label = f"{emoji} {order.get('service', '')[:22]} — {text}"
        markup.add(types.InlineKeyboardButton(label, callback_data=f"view_{order_id}"))

    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_menu"))
    return markup


def get_order_actions(order_id, user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    order = ORDERS.get(order_id, {})
    status = order.get("status", "pending")

    if str(user_id) in ADMIN_IDS:
        if status == "pending":
            markup.add(
                types.InlineKeyboardButton("✅ Принять", callback_data=f"accept_{order_id}"),
                types.InlineKeyboardButton("❌ Отказать", callback_data=f"cancel_{order_id}"),
            )
        elif status == "accepted":
            markup.add(types.InlineKeyboardButton("🔵 В работу", callback_data=f"work_{order_id}"))
        elif status == "working":
            markup.add(types.InlineKeyboardButton("✅ Выполнен", callback_data=f"complete_{order_id}"))
        elif status in {"completed", "cancelled"}:
            markup.add(types.InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{order_id}"))
        markup.add(types.InlineKeyboardButton("💬 Чат по заказу", callback_data=f"order_chat_{order_id}"))
    else:
        if str(order.get("user_id")) == user_id and status in {"pending", "accepted"}:
            markup.add(types.InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_{order_id}"))
        markup.add(types.InlineKeyboardButton("💬 Чат по заказу", callback_data=f"order_chat_{order_id}"))

    markup.add(types.InlineKeyboardButton("🔙 К заказам", callback_data="back_my_orders"))
    return markup


def get_user_list_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    if not USER_DATA:
        markup.add(types.InlineKeyboardButton("❌ Нет пользователей", callback_data="none"))
        return markup

    for user_id in sorted(USER_DATA):
        display = get_user_display(user_id)
        badge = "🔴 " if user_id in BANNED_USERS else "🟢 "
        markup.add(types.InlineKeyboardButton(f"{badge}{display}", callback_data=f"user_{user_id}"))

    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_admin"))
    return markup


def get_admin_user_actions(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    if user_id in BANNED_USERS:
        markup.add(types.InlineKeyboardButton("🔓 Разбанить", callback_data=f"unban_{user_id}"))
    else:
        markup.add(types.InlineKeyboardButton("🔒 Забанить", callback_data=f"ban_{user_id}"))
    markup.add(types.InlineKeyboardButton("💬 Поддержка", callback_data=f"support_chat_{user_id}"))
    markup.add(types.InlineKeyboardButton("📋 Заказы", callback_data=f"user_orders_{user_id}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_users"))
    return markup


def notify_admins(text, photo_id=None, parse_mode="Markdown"):
    for admin_id in ADMIN_IDS:
        try:
            if photo_id:
                bot.send_photo(int(admin_id), photo_id, caption=text, parse_mode=parse_mode)
            else:
                bot.send_message(int(admin_id), text, parse_mode=parse_mode)
        except Exception as e:
            print(f"Admin notify error: {e}")


def forward_message_content(chat_id, message, caption_prefix=""):
    if message.content_type == "text":
        bot.send_message(chat_id, f"{caption_prefix}{message.text}")
    elif message.content_type == "photo":
        bot.send_photo(
            chat_id,
            message.photo[-1].file_id,
            caption=f"{caption_prefix}{message.caption or '📸 Скриншот'}",
        )
    elif message.content_type == "document":
        bot.send_document(chat_id, message.document.file_id, caption=f"{caption_prefix}{message.caption or ''}")
    elif message.content_type == "video":
        bot.send_video(chat_id, message.video.file_id, caption=f"{caption_prefix}{message.caption or '🎥 Видео'}")
    elif message.content_type == "voice":
        bot.send_voice(chat_id, message.voice.file_id)
    elif message.content_type == "sticker":
        bot.send_sticker(chat_id, message.sticker.file_id)
    elif message.content_type == "animation":
        bot.send_animation(chat_id, message.animation.file_id, caption=f"{caption_prefix}{message.caption or '🎞 Анимация'}")
    else:
        bot.send_message(chat_id, f"{caption_prefix}[{message.content_type}]")


def append_chat_message(order_id, sender, text):
    if order_id and order_id in ORDERS:
        ORDERS[order_id].setdefault("chat_history", []).append(
            {
                "from": sender,
                "text": text,
                "time": datetime.now().strftime("%H:%M"),
            }
        )
        save_data()


# ===== Команды =====
@bot.message_handler(commands=["start"])
def start(message):
    user_id = str(message.from_user.id)
    init_user(user_id, message.from_user.username, message.from_user.first_name)

    welcome_text = (
        "🏆 Добро пожаловать в буст-сервис Nulls Brawl! 🏆\n\n"
        "Мы поможем вам быстро и безопасно поднять:\n"
        "• 🏆 Ранкед\n"
        "• 📊 Общие трофеи\n"
        "• 🎯 Любого персонажа\n\n"
        "✅ Работаем конфиденциально\n"
        "✅ Поддержка доступна в любое время\n"
        "✅ Часы работы менеджера — нажмите Поддержка\n\n"
        "Выберите нужный раздел ниже или нажмите /menu."
    )

    if user_id in ADMIN_IDS:
        bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=get_admin_menu())
    else:
        bot.send_message(message.chat.id, welcome_text, parse_mode="Markdown", reply_markup=get_user_menu())


@bot.message_handler(func=lambda m: m.text == "📋 Главное меню")
def back_to_main(message):
    user_id = str(message.from_user.id)
    if user_id in ADMIN_IDS:
        bot.send_message(message.chat.id, "Админ панель", reply_markup=get_admin_menu())
    else:
        bot.send_message(message.chat.id, "Главное меню:", reply_markup=get_user_menu())


@bot.message_handler(func=lambda m: m.text == "🚪 Выйти из админки")
def exit_admin(message):
    user_id = str(message.from_user.id)
    if user_id in ADMIN_IDS:
        ADMIN_IDS.discard(user_id)
        save_data()
        bot.send_message(
            message.chat.id,
            "✅ Вы вышли из админ панели.\n\n🏆 Добро пожаловать в буст сервис Nulls Brawl!",
            reply_markup=get_user_menu(),
        )


@bot.message_handler(func=lambda m: m.text == "📋 Услуги")
def show_services(message):
    close_user_chat(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "📋 Наши услуги:\n\n"
        "🏆 Ранкед — повышение ранга\n"
        "📊 Общие трофеи — набор трофеев на аккаунт\n"
        "🎯 Персонаж — прокачка одного бойца\n\n"
        "Выберите нужную категорию ниже.",
        reply_markup=get_services_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == "💬 Поддержка")
def support_request(message):
    user_id = str(message.from_user.id)
    if user_id in ADMIN_IDS:
        bot.send_message(message.chat.id, "👑 Вы уже находитесь в админке.", reply_markup=get_admin_menu())
        return

    admin_ids = [admin_id for admin_id in ADMIN_IDS if admin_id]
    if admin_ids:
        CURRENT_CHAT[user_id] = {"target_id": admin_ids[0], "order_id": None}
        bot.send_message(
            message.chat.id,
            "💬 Поддержка отвечает с 9:00 до 02:00. Напишите ваш вопрос, и администратор ответит вам здесь.",
            reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Закрыть чат", callback_data="close_chat")),
        )
    else:
        bot.send_message(
            message.chat.id,
            "💬 Поддержка отвечает с 9:00 до 02:00. Напишите ваш вопрос, и мы ответим, как только сможем.",
            reply_markup=get_user_menu(),
        )


@bot.message_handler(func=lambda m: m.text == "ℹ️ О нас")
def about_us(message):
    close_user_chat(message.from_user.id)
    text = (
        "🏆 О нас\n\n"
        "Мы делаем буст в Nulls Brawl быстро, безопасно и качественно.\n\n"
        "✅ Работаем от 1 часа до 2 дней\n"
        "✅ Поддерживаем чистую и аккуратную работу\n"
        "✅ Доводим заказ до результата\n\n"
        "👤 Исполнители:\n"
        "🔥 Опытные игроки с рангом Лега 1 — Мастер 3\n\n"
        "💬 Все вопросы и переписка — только внутри бота\n\n"
        "🕒 Часы работы менеджера:\n"
        "Нажмите на кнопку Поддержка, чтобы узнать о часах работы менеджера.\n\n"
        "📞 Связь:\n"
        "@mirrex | @mess123ages_bot"
    )
    bot.send_message(message.chat.id, text, reply_markup=get_main_menu(str(message.from_user.id)))

@bot.message_handler(func=lambda m: m.text == "❓ Как пользоваться")
def how_to_use(message):
    close_user_chat(message.from_user.id)
    text = (
        "❓ Как пользоваться ботом\n\n"
        "1) Нажмите '🛒 Мои заказы'.\n"
        "2) Откройте нужный заказ из списка.\n"
        "3) Нажмите '💬 Чат по заказу' в информации заказа.\n"
        "4) Пишите сообщение, и админ ответит здесь.\n\n"
        "Если нужен менеджер, нажмите '💬 Поддержка'."
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=get_main_menu(str(message.from_user.id)))


@bot.message_handler(func=lambda m: m.text == "🛒 Мои заказы")
def my_orders(message):
    user_id = str(message.from_user.id)
    close_user_chat(user_id)
    init_user(user_id)

    if user_id in ADMIN_IDS:
        return

    orders_list = [
        order_id
        for order_id in USER_DATA.get(user_id, {}).get("orders", [])
        if ORDERS.get(order_id, {}).get("user_id") == user_id
    ]
    if not orders_list:
        bot.send_message(message.chat.id, "У вас пока нет заказов.", reply_markup=get_user_menu())
        return

    bot.send_message(message.chat.id, "📋 Ваши заказы:", reply_markup=get_orders_keyboard(orders_list))


@bot.message_handler(commands=["help"])
def help_command(message):
    close_user_chat(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "📌 Команды бота:\n"
        "• /start — начать работу\n"
        "• /menu — открыть главное меню\n"
        "• /orders — показать мои заказы\n"
        "• /support — написать в поддержку\n"
        "• /adm — вход в админ панель\n\n"
        "Используйте кнопки снизу для быстрого доступа ко всем функциям.",
        reply_markup=get_main_menu(message.from_user.id),
    )


@bot.message_handler(commands=["orders"])
def orders_command(message):
    return my_orders(message)


@bot.message_handler(commands=["support"])
def support_command(message):
    return support_request(message)


@bot.message_handler(commands=["menu"])
def show_menu_command(message):
    send_main_menu(message.from_user.id, message.chat.id)


@bot.message_handler(commands=["adm"])
def admin_command(message):
    return admin_panel(message)


def admin_panel(message):
    user_id = str(message.from_user.id)
    if is_admin(user_id):
        send_admin_menu(message.chat.id)
    else:
        ADMIN_LOGIN_PENDING.add(user_id)
        bot.send_message(
            message.chat.id,
            "🔐 Введите код доступа:",
            reply_markup=types.ReplyKeyboardRemove(),
        )


@bot.message_handler(func=lambda m: m.text == "📊 Заказы")
def admin_all_orders(message):
    user_id = str(message.from_user.id)
    if user_id not in ADMIN_IDS:
        return

    if not ORDERS:
        bot.send_message(message.chat.id, "📊 Нет заказов", reply_markup=get_admin_menu())
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for order_id, order in list(ORDERS.items())[-20:]:
        emoji, status_text = get_status_info(order.get("status", "pending"))
        label = f"{emoji} {order.get('service', '')[:25]} | {get_user_display(order.get('user_id', ''))}"
        markup.add(types.InlineKeyboardButton(label, callback_data=f"view_{order_id}"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_admin"))

    bot.send_message(message.chat.id, "📊 Все заказы:", reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == "👤 Пользователи")
def admin_users(message):
    user_id = str(message.from_user.id)
    if user_id not in ADMIN_IDS:
        return
    bot.send_message(message.chat.id, "👤 Список пользователей:", reply_markup=get_user_list_keyboard())


# ===== Обработка сообщений =====
@bot.message_handler(content_types=['text','photo','document','video','animation','sticker','voice','video_note'])
def handle_messages(message):
    user_id = str(message.from_user.id)

    if is_rate_limited(user_id):
        pending = PENDING_ORDERS.get(user_id)
        if not (pending and pending.get("stage") == "await_data"):
            return

    if is_admin_login_pending(user_id):
        ADMIN_LOGIN_PENDING.discard(user_id)
        if message.text and message.text.strip() == ADMIN_PASSWORD:
            ADMIN_IDS.add(user_id)
            save_data()
            bot.send_message(
                message.chat.id,
                "✅ Пароль верный. Добро пожаловать в админ панель!",
                reply_markup=get_admin_menu(),
            )
        else:
            bot.send_message(
                message.chat.id,
                "❌ Неверный пароль. Попробуйте снова командой /adm.",
                reply_markup=get_user_menu(),
            )
        return

    menu_commands = {
        "📋 Главное меню": back_to_main,
        "🚪 Выйти из админки": exit_admin,
        "📋 Услуги": show_services,
        "ℹ️ О нас": about_us,
        "🛒 Мои заказы": my_orders,
        "❓ Как пользоваться": how_to_use,
        "📊 Заказы": admin_all_orders,
        "👤 Пользователи": admin_users,
    }
    if message.text and message.text in menu_commands:
        try:
            menu_commands[message.text](message)
        except Exception as e:
            logging.exception("Menu handler failed")
            bot.send_message(message.chat.id, "⚠️ Не удалось открыть раздел", reply_markup=get_main_menu(user_id))
        return

    if user_id in BANNED_USERS:
        bot.send_message(message.chat.id, "❌ Вы забанены.")
        return

    init_user(user_id, message.from_user.username, message.from_user.first_name)
    if user_id not in ADMIN_IDS:
        record_user_message(user_id, message)

    if user_id in PENDING_ORDERS:
        pending = PENDING_ORDERS[user_id]
        if pending.get("stage") == "await_data":
            if message.text and message.text.strip().lower() in {"❌ отменить заказ", "отмена", "отменить"}:
                del PENDING_ORDERS[user_id]
                bot.send_message(message.chat.id, "❌ Заказ отменён.", reply_markup=get_user_menu())
                return

            photo_id = None
            user_data_text = "📸 Скриншот"
            if message.content_type == "photo":
                photo_id = message.photo[-1].file_id
                user_data_text = message.caption or "📸 Скриншот"
            elif message.content_type == "text":
                user_data_text = message.text
            elif message.content_type == "document":
                photo_id = message.document.file_id
                user_data_text = f"📄 Файл: {message.document.file_name}"
            elif message.content_type == "video":
                photo_id = message.video.file_id
                user_data_text = message.caption or "🎥 Видео"
            elif message.content_type == "voice":
                photo_id = message.voice.file_id
                user_data_text = "🎤 Голосовое"
            elif message.content_type == "sticker":
                user_data_text = "🏷 Стикер"
            elif message.content_type == "animation":
                photo_id = message.animation.file_id
                user_data_text = message.caption or "🎞 Анимация"

            pending.setdefault("data", "")
            pending.setdefault("attachments", [])
            if pending["data"]:
                pending["data"] += "\n" + user_data_text
            else:
                pending["data"] = user_data_text

            if photo_id:
                pending["attachments"].append(photo_id)
                pending["photo_id"] = photo_id

            pending["stage"] = "confirm"
            preview_text = (
                f"📝 Проверьте заказ\n\n"
                f"📋 {pending['service']}\n"
                f"💰 {format_price(pending['price'])}\n"
                f"📝 Данные: {pending['data']}\n\n"
                "Хотите добавить ещё данные или файлы?"
            )

            if photo_id and message.content_type == "photo":
                bot.send_photo(
                    message.chat.id,
                    photo_id,
                    caption=preview_text,
                    reply_markup=get_add_more_keyboard_for(user_id),
                )
            elif photo_id and message.content_type == "document":
                bot.send_document(
                    message.chat.id,
                    photo_id,
                    caption=preview_text,
                    reply_markup=get_add_more_keyboard_for(user_id),
                )
            elif photo_id and message.content_type == "video":
                bot.send_video(
                    message.chat.id,
                    photo_id,
                    caption=preview_text,
                    reply_markup=get_add_more_keyboard_for(user_id),
                )
            elif photo_id and message.content_type == "animation":
                bot.send_animation(
                    message.chat.id,
                    photo_id,
                    caption=preview_text,
                    reply_markup=get_add_more_keyboard_for(user_id),
                )
            else:
                bot.send_message(
                    message.chat.id,
                    preview_text,
                    reply_markup=get_add_more_keyboard_for(user_id),
                )
            return

        if pending.get("stage") == "confirm":
            bot.send_message(
                message.chat.id,
                "✅ Ваш заказ готов. Нажмите Подтвердить или Отменить.",
                reply_markup=get_confirm_order_keyboard(user_id),
            )
            return

    if user_id in CURRENT_CHAT and user_id not in ADMIN_IDS:
        chat_data = CURRENT_CHAT[user_id]
        target_id = chat_data.get("target_id")
        order_id = chat_data.get("order_id")
        if target_id:
            try:
                caption_prefix = f"💬 Сообщение от {get_user_display(user_id)}:\n"
                forward_message_content(int(target_id), message, caption_prefix=caption_prefix)

                append_chat_message(order_id, "user", message.text or message.caption or f"[{message.content_type}]")
                bot.send_message(message.chat.id, "✅ Сообщение отправлено", reply_markup=get_user_menu())
                return
            except Exception as e:
                bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
                return

    if user_id in ADMIN_IDS and user_id in CURRENT_CHAT:
        chat_data = CURRENT_CHAT[user_id]
        target_id = chat_data.get("target_id")
        order_id = chat_data.get("order_id")
        if target_id:
            try:
                caption_prefix = "💬 Сообщение от администратора:\n"
                forward_message_content(int(target_id), message, caption_prefix=caption_prefix)

                append_chat_message(order_id, "admin", message.text or message.caption or f"[{message.content_type}]")
                bot.send_message(message.chat.id, f"✅ Отправлено {get_user_display(target_id)}", reply_markup=get_admin_menu())
                return
            except Exception as e:
                bot.send_message(message.chat.id, f"❌ Ошибка: {e}", reply_markup=get_admin_menu())
                return

    if message.text:
        bot.send_message(
            message.chat.id,
            "🤖 Я вас услышал! Чтобы открыть меню, нажмите /menu или выберите одну из кнопок ниже.",
            reply_markup=get_main_menu(user_id),
        )
        return

    if user_id not in USER_DATA:
        USER_DATA[user_id] = {"username": message.from_user.username or "No username", "first_name": message.from_user.first_name or "", "messages": [], "orders": []}

    USER_DATA[user_id]["messages"].append({
        "text": message.text or f"[{message.content_type}]",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "from_user": True,
    })
    save_data()

    if message.text and user_id not in ADMIN_IDS:
        bot.send_message(
            message.chat.id,
            "📋 Для оформления заказа откройте раздел Услуги.\nℹ️ Узнать больше можно в разделе О нас.\n💬 Нужна помощь — обратитесь в Поддержку.",
            reply_markup=get_user_menu(),
        )


# ===== Callback'ы =====
@bot.callback_query_handler(func=lambda c: True)
def handle_callbacks(call):
    user_id = str(call.from_user.id)
    data = call.data

    if data == "back_menu":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.send_message(call.message.chat.id, "Главное меню:", reply_markup=get_main_menu(user_id))
        return

    if data == "back_services":
        bot.edit_message_text("📋 Выберите категорию услуг:", call.message.chat.id, call.message.message_id, reply_markup=get_services_keyboard())
        return

    if data == "back_admin":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.send_message(call.message.chat.id, "Админ панель", reply_markup=get_admin_menu())
        return

    if data == "back_users":
        bot.edit_message_text("👤 Список пользователей:", call.message.chat.id, call.message.message_id, reply_markup=get_user_list_keyboard())
        return

    if data == "back_my_orders":
        orders_list = USER_DATA.get(user_id, {}).get("orders", [])
        try:
            bot.edit_message_text("📋 Ваши заказы:", call.message.chat.id, call.message.message_id, reply_markup=get_orders_keyboard(orders_list))
            bot.answer_callback_query(call.id, "📋 Список заказов открыт")
        except Exception as e:
            print(f"back_my_orders edit failed: {e}")
            bot.answer_callback_query(call.id, "📋 Откройте список заказов", show_alert=False)
            bot.send_message(call.message.chat.id, "📋 Ваши заказы:", reply_markup=get_orders_keyboard(orders_list))
        return

    if data == "service_ranked":
        bot.edit_message_text("🏆 Ранкед\n\nВыберите ранг:", call.message.chat.id, call.message.message_id, reply_markup=get_ranked_keyboard(), parse_mode="Markdown")
        return

    if data == "service_trophies":
        bot.edit_message_text("📊 Общие трофеи\n\nВыберите количество:", call.message.chat.id, call.message.message_id, reply_markup=get_trophies_keyboard(), parse_mode="Markdown")
        return

    if data == "service_character":
        bot.edit_message_text("🎯 Один персонаж\n\nВыберите количество трофеев:", call.message.chat.id, call.message.message_id, reply_markup=get_character_keyboard(), parse_mode="Markdown")
        return

    if data.startswith("order_") and not data.startswith("order_chat_"):
        parts = data.split("_", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Ошибка")
            return
        category = parts[1]
        name = parts[2]
        price = PRICES.get(category, {}).get(name, 0)
        service_name = f"{CATEGORY_NAMES.get(category, category.capitalize())} — {name}"

        PENDING_ORDERS[user_id] = {
            "service": service_name,
            "price": price,
            "category": category,
            "name": name,
            "stage": "await_data",
        }
        CURRENT_CHAT.pop(user_id, None)

        bot.edit_message_text(
            f"📝 Оформление заказа\n\n📋 {service_name}\n💰 {format_price(price)}\n\nℹ️ Для буста нужен вход в аккаунт.\n📸 Скриншот не обязателен, но желателен.\n💬 Отправьте комментарий, скриншот или файл.\n\nЧтобы отменить — нажмите кнопку ниже.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_pending_order_keyboard_for(user_id),
            parse_mode="Markdown",
        )
        bot.answer_callback_query(call.id)
        return

    if data.startswith("view_"):
        order_id = data.replace("view_", "")
        order = ORDERS.get(order_id)
        if not order:
            bot.answer_callback_query(call.id, "❌ Заказ не найден")
            return

        emoji, status_text = get_status_info(order.get("status", "pending"))
        text = (
            f"🆔 {order_id}\n"
            f"📋 {order.get('service', '')}\n"
            f"💰 {order.get('price', 0)}₽\n"
            f"📊 {emoji} {status_text}\n"
            f"📅 {order.get('created_at', '')}\n"
            f"📝 Данные: {order.get('data', 'Не указаны')}"
        )

        if order.get("photo_id"):
            try:
                bot.send_photo(call.message.chat.id, order["photo_id"], caption=format_order_text(order_id, order), reply_markup=get_order_actions(order_id, user_id))
                bot.delete_message(call.message.chat.id, call.message.message_id)
                return
            except Exception as e:
                print(f"Photo error: {e}")

        bot.edit_message_text(format_order_text(order_id, order), call.message.chat.id, call.message.message_id, reply_markup=get_order_actions(order_id, user_id))
        return

    if data.startswith("accept_"):
        order_id = data.replace("accept_", "")
        order = ORDERS.get(order_id)
        if order:
            order["status"] = "accepted"
            save_data()
            bot.answer_callback_query(call.id, "✅ Заказ принят")
            try:
                bot.send_message(int(order["user_id"]), f"✅ Ваш заказ {order_id} принят!", parse_mode="Markdown")
            except Exception:
                pass
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=get_order_actions(order_id, user_id))
        else:
            bot.answer_callback_query(call.id, "❌ Заказ не найден")
        return

    if data.startswith("work_"):
        order_id = data.replace("work_", "")
        order = ORDERS.get(order_id)
        if order:
            order["status"] = "working"
            save_data()
            bot.answer_callback_query(call.id, "🔵 Заказ в работе")
            try:
                bot.send_message(int(order["user_id"]), f"🔵 Ваш заказ {order_id} в работе!", parse_mode="Markdown")
            except Exception:
                pass
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=get_order_actions(order_id, user_id))
        else:
            bot.answer_callback_query(call.id, "❌ Заказ не найден")
        return

    if data.startswith("complete_"):
        order_id = data.replace("complete_", "")
        order = ORDERS.get(order_id)
        if order:
            order["status"] = "completed"
            save_data()
            bot.answer_callback_query(call.id, "✅ Заказ выполнен")
            try:
                bot.send_message(int(order["user_id"]), f"✅ Ваш заказ {order_id} выполнен!", parse_mode="Markdown")
            except Exception:
                pass
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=get_order_actions(order_id, user_id))
        else:
            bot.answer_callback_query(call.id, "❌ Заказ не найден")
        return

    if data.startswith("confirm_pending"):
        parts = data.split(":", 1)
        if len(parts) == 1 and user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌ Эта кнопка не для вас")
            return
        owner = parts[1].strip() if len(parts) > 1 and parts[1].strip() else user_id
        if owner != user_id and user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌ Эта кнопка не для вас")
            return

        owner = str(owner)
        pending = None
        if owner in PENDING_ORDERS:
            pending = PENDING_ORDERS.pop(owner, None)
        elif user_id in PENDING_ORDERS:
            pending = PENDING_ORDERS.pop(user_id, None)

        if not pending:
            msg_text = call.message.caption or call.message.text or ""
            svc = None
            price = None
            data_text = None
            m = re.search(r"📋\s*(.+)", msg_text)
            if m:
                svc = m.group(1).strip()
            m = re.search(r"💰\s*([^\n]+)", msg_text)
            if m:
                price = m.group(1).strip()
            m = re.search(r"📝\s*Данные:\s*([^\n]+)", msg_text)
            if m:
                data_text = m.group(1).strip()

            photo_id = None
            if getattr(call.message, 'photo', None):
                try:
                    photo_id = call.message.photo[-1].file_id
                except Exception:
                    photo_id = None

            if svc:
                pending = {
                    "service": svc,
                    "price": price or 0,
                    "category": None,
                    "name": None,
                    "stage": "confirm",
                    "data": data_text or "Не указаны",
                    "attachments": [photo_id] if photo_id else [],
                    "photo_id": photo_id,
                }
            else:
                lines = [l.strip() for l in msg_text.splitlines() if l.strip()]
                for line in lines:
                    if '—' in line or any(v in line for v in CATEGORY_NAMES.values()):
                        svc = line
                        break
                if svc:
                    pending = {
                        "service": svc,
                        "price": price or 0,
                        "category": None,
                        "name": None,
                        "stage": "confirm",
                        "data": data_text or "Не указаны",
                        "attachments": [photo_id] if photo_id else [],
                        "photo_id": photo_id,
                    }

            if not pending:
                print(f"Confirm fallback failed. call.data={data} msg_len={len(msg_text)}")
                try:
                    bot.answer_callback_query(call.id, f"❌ Нет активного заказа для подтверждения (DBG)", show_alert=True)
                except Exception:
                    pass

        if not pending:
            bot.answer_callback_query(call.id, "❌ Нет активного заказа для подтверждения")
            return

        order_id = generate_order_id()
        order = {
            "user_id": owner,
            "service": pending["service"],
            "price": pending["price"],
            "category": pending["category"],
            "name": pending["name"],
            "status": "pending",
            "data": pending.get("data", "Не указаны"),
            "photo_id": pending.get("photo_id") or (pending.get("attachments") or [None])[0],
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "chat_history": [],
        }

        ORDERS[order_id] = order
        USER_DATA.setdefault(owner, {"username": call.from_user.username or "No username", "first_name": call.from_user.first_name or "", "messages": [], "orders": []})
        USER_DATA[owner]["orders"].append(order_id)
        save_data()

        safe_edit_message_text(
            call,
            f"✅ Заказ подтверждён!\n\n🆔 {order_id}\n📋 {order['service']}\n💰 {format_price(order['price'])}\n\n⏳ Ожидайте подтверждения администратора.",
            reply_markup=None,
        )
        bot.answer_callback_query(call.id, "✅ Заказ подтверждён")
        bot.send_message(
            call.message.chat.id,
            "Ваш заказ успешно создан. Чтобы открыть чат с админом, перейдите в '🛒 Мои заказы', откройте нужный заказ и нажмите '💬 Чат по заказу'.",
            reply_markup=get_user_menu(),
        )
        try:
            bot.send_message(call.message.chat.id, "Финальная часть оформления заказа прошла. По поводу оплаты с вами свяжется наш менеджер.")
        except Exception:
            pass

        notify_admins(
            f"🔔 НОВЫЙ ЗАКАЗ!\n\n🆔 {order_id}\n👤 {get_user_full(owner)}\n📋 {order['service']}\n💰 {format_price(order['price'])}\n📝 Данные: {order['data']}\n\n⚡ Требуется внимание администратора.",
            photo_id=order.get("photo_id"),
        )
        return

    if data.startswith("cancel_pending"):
        parts = data.split(":", 1)
        if len(parts) == 1 and user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌ Эта кнопка не для вас")
            return
        owner = parts[1] if len(parts) > 1 else user_id
        if owner != user_id and user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌ Эта кнопка не для вас")
            return

        owner = str(owner)
        if owner in PENDING_ORDERS:
            PENDING_ORDERS.pop(owner, None)
        else:
            PENDING_ORDERS.pop(user_id, None)
        safe_edit_message_text(
            call,
            "❌ Заказ отменён.",
            reply_markup=None,
        )
        bot.answer_callback_query(call.id, "Заказ отменён")
        bot.send_message(call.message.chat.id, "Если хотите, выберите услугу снова:", reply_markup=get_user_menu())
        return

    if data.startswith("add_more"):
        parts = data.split(":", 1)
        if len(parts) == 1 and user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌ Эта кнопка не для вас")
            return
        owner = parts[1] if len(parts) > 1 else user_id
        if owner != user_id and user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌ Эта кнопка не для вас")
            return

        owner = str(owner)
        pending = PENDING_ORDERS.get(owner)
        if not pending:
            pending = PENDING_ORDERS.get(user_id)
            if not pending:
                bot.answer_callback_query(call.id, "❌ Нет активного заказа для редактирования")
                return
        pending["stage"] = "await_data"
        safe_edit_message_text(
            call,
            "✏️ Отправьте дополнительные данные, скриншот или файл. Чтобы отменить — нажмите кнопку ниже.",
            reply_markup=get_pending_order_keyboard_for(owner),
        )
        bot.answer_callback_query(call.id, "Отправьте дополнительные данные")
        return

    if data.startswith("cancel_"):
        order_id = data.replace("cancel_", "")
        order = ORDERS.get(order_id)
        if not order:
            bot.answer_callback_query(call.id, "❌ Заказ не найден")
            return

        if str(order.get("user_id")) != user_id and user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌ У вас нет права отменять этот заказ")
            return

        order["status"] = "cancelled"
        owner_id = str(order.get("user_id"))
        if owner_id in USER_DATA and order_id in USER_DATA[owner_id].get("orders", []):
            USER_DATA[owner_id]["orders"].remove(order_id)
        save_data()
        bot.answer_callback_query(call.id, "❌ Заказ отменён")
        try:
            bot.send_message(int(order["user_id"]), f"❌ Ваш заказ {order_id} отменён.", parse_mode="Markdown")
        except Exception:
            pass
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=get_order_actions(order_id, user_id))
        return

    if data.startswith("delete_"):
        order_id = data.replace("delete_", "")
        order = ORDERS.get(order_id)
        if order:
            owner_id = order.get("user_id")
            if owner_id in USER_DATA and order_id in USER_DATA[owner_id].get("orders", []):
                USER_DATA[owner_id]["orders"].remove(order_id)
            del ORDERS[order_id]
            save_data()
            bot.answer_callback_query(call.id, "🗑 Заказ удалён")
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            bot.send_message(call.message.chat.id, "🗑 Заказ удалён", reply_markup=get_admin_menu())
        else:
            bot.answer_callback_query(call.id, "❌ Заказ не найден")
        return

    if data.startswith("order_chat_"):
        order_id = data.replace("order_chat_", "")
        order = ORDERS.get(order_id)
        if not order:
            bot.answer_callback_query(call.id, "❌ Заказ не найден")
            return

        if user_id in ADMIN_IDS:
            target_user_id = order.get("user_id")
        else:
            admin_ids = [admin_id for admin_id in ADMIN_IDS if admin_id]
            if not admin_ids:
                bot.answer_callback_query(call.id, "❌ Администратор сейчас не доступен")
                return
            target_user_id = admin_ids[0]

        CURRENT_CHAT[user_id] = {"target_id": target_user_id, "order_id": order_id}

        history_text = ""
        if order.get("chat_history"):
            history_text = "\n📜 История чата:\n"
            for msg in order["chat_history"][-10:]:
                sender = "👤 Вы" if msg["from"] == "user" else "👑 Админ"
                history_text += f"{sender} [{msg['time']}]: {msg['text']}\n"

        try:
            if user_id in ADMIN_IDS:
                try:
                    safe_edit_message_text(
                        call,
                        f"💬 Чат по заказу {order_id}\n👤 {get_user_display(order.get('user_id'))}\n{history_text}\n✏️ Напишите сообщение:",
                        reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Закрыть чат", callback_data="close_chat")),
                        parse_mode="Markdown",
                    )
                    bot.answer_callback_query(call.id, "💬 Чат по заказу открыт")
                except Exception as e:
                    print(f"order_chat admin edit failed: {e}")
                    bot.send_message(
                        call.message.chat.id,
                        f"💬 Чат по заказу {order_id}\n👤 {get_user_display(order.get('user_id'))}\n{history_text}\n✏️ Напишите сообщение:",
                        parse_mode="Markdown",
                        reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Закрыть чат", callback_data="close_chat")),
                    )
                    bot.answer_callback_query(call.id, "💬 Чат по заказу открыт")
            else:
                bot.send_message(
                    call.message.chat.id,
                    f"💬 Чат по заказу {order_id}\n{history_text}\n✏️ Пишите сюда.",
                    parse_mode="Markdown",
                    reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Закрыть чат", callback_data="close_chat")),
                )
                bot.answer_callback_query(call.id, "💬 Чат по заказу открыт")
        except Exception as e:
            print(f"order_chat error: {e}")
            try:
                bot.answer_callback_query(call.id, "❌ Не удалось открыть чат", show_alert=True)
            except Exception:
                pass
        return

    if data.startswith("support_chat_"):
        target_id = data.replace("support_chat_", "")
        CURRENT_CHAT[user_id] = {"target_id": target_id, "order_id": None}
        try:
            safe_edit_message_text(
                call,
                f"💬 Поддержка\n👤 {get_user_display(target_id)}\n\n✏️ Напишите сообщение:",
                reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔙 Закрыть чат", callback_data="close_chat")),
                parse_mode="Markdown",
            )
            bot.answer_callback_query(call.id, "💬 Чат поддержки открыт")
        except Exception as e:
            print(f"support_chat error: {e}")
            try:
                bot.answer_callback_query(call.id, "❌ Не удалось открыть чат", show_alert=True)
            except Exception:
                pass
        return

    if data == "close_chat":
        CURRENT_CHAT.pop(user_id, None)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        if user_id in ADMIN_IDS:
            bot.send_message(call.message.chat.id, "Админ панель", reply_markup=get_admin_menu())
        else:
            bot.send_message(call.message.chat.id, "Главное меню:", reply_markup=get_user_menu())
        return

    if data.startswith("user_") and not data.startswith("user_orders_"):
        target_id = data.replace("user_", "")
        history_text = build_user_history_text(target_id)
        bot.edit_message_text(
            f"👤 {get_user_full(target_id)}\n\n{history_text}",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_admin_user_actions(target_id),
        )
        return

    if data.startswith("user_orders_"):
        target_id = data.replace("user_orders_", "")
        orders_list = USER_DATA.get(target_id, {}).get("orders", [])
        if not orders_list:
            bot.answer_callback_query(call.id, "Нет заказов")
            return
        bot.edit_message_text(
            f"📋 Заказы {get_user_display(target_id)}:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_orders_keyboard(orders_list),
        )
        return

    if data.startswith("ban_"):
        target_id = data.replace("ban_", "")
        BANNED_USERS.add(target_id)
        save_data()
        bot.answer_callback_query(call.id, "🔒 Пользователь забанен")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=get_admin_user_actions(target_id))
        return

    if data.startswith("unban_"):
        target_id = data.replace("unban_", "")
        BANNED_USERS.discard(target_id)
        save_data()
        bot.answer_callback_query(call.id, "🔓 Пользователь разбанен")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=get_admin_user_actions(target_id))
        return

    if data == "none":
        bot.answer_callback_query(call.id, "Нет данных")


# ===== Запуск =====
def run_bot():
    set_default_commands()
    while True:
        try:
            logging.info("Бот запущен")
            # bot.remove_webhook()
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            logging.exception("Ошибка при работе бота")
            time.sleep(5)


if __name__ == "__main__":
    run_bot()
