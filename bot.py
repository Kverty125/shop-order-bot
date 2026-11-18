"""Shop Order Bot: a small Telegram storefront.

Customers browse a catalog, place orders and chat with the shop. Admins add products
right inside Telegram (even in bulk), manage orders and message customers.

All settings come from environment variables / a .env file (see .env.example).
"""
import hmac
import json
import logging
import math
import os
import sys
import tempfile
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from functools import wraps
from pathlib import Path

import telebot
from telebot import types

try:  # python-dotenv is optional
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ENV_ADMINS = {x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
MONGO_URL = os.getenv("MONGO_URL", "").strip()
MONGO_DB = os.getenv("MONGO_DB", "shop_bot")
DATA_FILE = Path(os.getenv("DATA_FILE", "shop_data.json"))
SHOP_NAME = os.getenv("SHOP_NAME", "Our Shop")
PRICE_FORMAT = os.getenv("PRICE_FORMAT", "${amount}")      # e.g. "{amount} EUR"
WELCOME_TEXT = os.getenv("WELCOME_TEXT", f"Welcome to {SHOP_NAME}! Browse the catalog and place an order.")
ABOUT_TEXT = os.getenv("ABOUT_TEXT", f"{SHOP_NAME}\n\nBrowse the catalog, order in a few taps "
                                     f"and chat with us right here in the bot.")
ENABLE_WEB = os.getenv("ENABLE_WEB", "0") == "1"
PORT = int(os.getenv("PORT", "10000"))

PAGE_SIZE = 8
MAX_QTY = 99
RATE_LIMIT = (15, 10)          # at most 15 messages per 10 seconds per customer
LOGIN_ATTEMPTS = (5, 600)      # 5 wrong passwords -> 10 minute lock

if not TOKEN:
    sys.exit("BOT_TOKEN is not set. Copy .env.example to .env and paste the token from @BotFather.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("shop-bot")
bot = telebot.TeleBot(TOKEN)


# --------------------------------------------------------------------------
# Storage (JSON file or MongoDB). The whole state is one small document.
# --------------------------------------------------------------------------
class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.mongo = None
        self.data = {"admins": [], "banned": [], "users": {}, "products": {}, "orders": {},
                     "counters": {"product": 0, "order": 1000}}
        if MONGO_URL:
            self._connect_mongo()
        self.load()

    def _connect_mongo(self):
        try:
            import certifi
            from pymongo import MongoClient
            client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=10000,
                                 tls=True, tlsCAFile=certifi.where())
            client.admin.command("ping")
            self.mongo = client[MONGO_DB]["state"]
            log.info("Connected to MongoDB")
        except Exception as e:   # do not silently fall back: that would lose data on restarts
            sys.exit(f"MongoDB connection failed: {e}")

    def load(self):
        loaded = None
        try:
            if self.mongo is not None:
                loaded = self.mongo.find_one({"_id": "state"})
                if loaded:
                    loaded.pop("_id", None)
            elif DATA_FILE.exists():
                loaded = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            sys.exit(f"Could not load saved data: {e}")
        if loaded:
            self.data.update(loaded)

    def save(self):
        with self.lock:
            try:
                if self.mongo is not None:
                    self.mongo.replace_one({"_id": "state"}, {"_id": "state", **self.data}, upsert=True)
                    return
                DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=str(DATA_FILE.parent), suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, DATA_FILE)
            except Exception as e:
                log.error("Save failed: %s", e)


db = Store()
D = db.data

# in-memory, per-process state
CURRENT_CHAT = {}      # user_id -> {"order": oid|None}   or   admin_id -> {"target": uid, "order": oid|None}
PENDING = {}           # user_id -> order being created
ADMIN_STATE = {}       # admin_id -> {"action": "add"|"edit", ...}
LOGIN_PENDING = set()
LOGIN_FAILS = defaultdict(list)
MSG_TIMES = defaultdict(lambda: deque(maxlen=RATE_LIMIT[0]))

STATUS = {
    "pending": ("⏳", "Pending"), "accepted": ("🟡", "Accepted"), "in_progress": ("🔵", "In progress"),
    "completed": ("✅", "Completed"), "rejected": ("⛔", "Rejected"), "cancelled": ("❌", "Cancelled"),
}
ACTIVE = {"pending", "accepted", "in_progress"}

BTN_CATALOG, BTN_ORDERS, BTN_SUPPORT, BTN_ABOUT = "🛍 Catalog", "🛒 My orders", "💬 Support", "ℹ️ About"
BTN_PRODUCTS, BTN_ADD, BTN_AORDERS, BTN_USERS, BTN_LEAVE = (
    "📦 Products", "➕ Add product", "📊 Orders", "👥 Users", "🚪 Leave admin")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def clip(text, n):
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def admins():
    return set(D["admins"]) | ENV_ADMINS


def is_admin(uid):
    return str(uid) in admins()


def is_banned(uid):
    return str(uid) in D["banned"]


def fmt_price(value):
    amount = f"{value:,.2f}"
    if amount.endswith(".00"):
        amount = amount[:-3]
    return PRICE_FORMAT.format(amount=amount)


def sold_out(p):
    return p["stock"] is not None and p["stock"] <= 0


def user_label(uid, full=False):
    u = D["users"].get(str(uid), {})
    name = f"@{u['username']}" if u.get("username") else (u.get("first_name") or f"ID {uid}")
    return f"{name} (ID {uid})" if full and not name.startswith("ID") else name


def track_user(tg_user):
    uid = str(tg_user.id)
    with db.lock:
        rec = D["users"].get(uid)
        fresh = {"username": tg_user.username or "", "first_name": tg_user.first_name or ""}
        if rec is None:
            D["users"][uid] = {"joined": now(), **fresh}
            db.save()
        elif any(rec.get(k) != v for k, v in fresh.items()):
            rec.update(fresh)
            db.save()


def rate_limited(uid):
    if is_admin(uid):
        return False
    q, t = MSG_TIMES[uid], time.time()
    if len(q) == q.maxlen and t - q[0] < RATE_LIMIT[1]:
        return True
    q.append(t)
    return False


def clear_state(uid):
    for store in (CURRENT_CHAT, PENDING, ADMIN_STATE):
        store.pop(uid, None)
    LOGIN_PENDING.discard(uid)


def describe(m):
    return m.text or m.caption or f"[{m.content_type}]"


def kb(*rows):
    """Inline keyboard from rows of (text, callback_data) tuples."""
    markup = types.InlineKeyboardMarkup()
    for row in rows:
        if row:
            markup.row(*[types.InlineKeyboardButton(t, callback_data=d) for t, d in row])
    return markup


def page_slice(items, page):
    pages = max(1, -(-len(items) // PAGE_SIZE))
    page = max(0, min(int(page), pages - 1))
    return items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page, pages


def nav_row(prefix, page, pages):
    row = []
    if page > 0:
        row.append(("‹ Prev", f"{prefix}:{page - 1}"))
    if page < pages - 1:
        row.append(("Next ›", f"{prefix}:{page + 1}"))
    return row


def menu_for(uid):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if is_admin(uid):
        markup.row(BTN_PRODUCTS, BTN_ADD)
        markup.row(BTN_AORDERS, BTN_USERS)
        markup.row(BTN_CATALOG, BTN_LEAVE)
    else:
        markup.row(BTN_CATALOG, BTN_ORDERS)
        markup.row(BTN_SUPPORT, BTN_ABOUT)
    return markup


def send_view(chat_id, view):
    text, markup, photo = view
    if photo:
        try:
            bot.send_photo(chat_id, photo, caption=clip(text, 1024), reply_markup=markup)
            return
        except Exception as e:
            log.warning("Could not send photo: %s", e)
    bot.send_message(chat_id, clip(text, 4096), reply_markup=markup)


def render(call, view):
    """Replace the message a button was pressed on with a new view."""
    text, markup, photo = view
    m = call.message
    if not photo and m.content_type == "text":
        try:
            bot.edit_message_text(clip(text, 4096), m.chat.id, m.message_id, reply_markup=markup)
            return
        except telebot.apihelper.ApiTelegramException as e:
            if "not modified" in str(e):
                return
            log.debug("edit failed, resending: %s", e)
    try:
        bot.delete_message(m.chat.id, m.message_id)
    except Exception:
        pass
    send_view(m.chat.id, view)


def toast(call, text="", alert=False):
    try:
        bot.answer_callback_query(call.id, text, show_alert=alert)
    except Exception as e:
        log.debug("answer_callback_query: %s", e)
    call.answered = True


def notify_admins(text, photo_id=None, markup=None):
    for a in admins():
        try:
            if photo_id:
                bot.send_photo(int(a), photo_id, caption=clip(text, 1024), reply_markup=markup)
            else:
                bot.send_message(int(a), clip(text, 4096), reply_markup=markup)
        except Exception as e:
            log.warning("Could not notify admin %s: %s", a, e)


# --------------------------------------------------------------------------
# Products and orders (data layer)
# --------------------------------------------------------------------------
def sorted_ids(mapping, reverse=False):
    return sorted(mapping, key=int, reverse=reverse)


def visible_products():
    return [(pid, D["products"][pid]) for pid in sorted_ids(D["products"]) if D["products"][pid]["available"]]


def parse_price(text):
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        raise ValueError("Price must be a number, e.g. 9.99")
    if not math.isfinite(value) or value < 0:
        raise ValueError("Price must be zero or more.")
    return round(value, 2)


def parse_stock(text):
    try:
        value = int(text)
    except ValueError:
        raise ValueError("Stock must be a whole number (or '-' for unlimited).")
    if value < 0:
        raise ValueError("Stock can't be negative.")
    return value


def parse_product_line(line):
    """'Name | Price | Description | Stock' (description and stock optional)."""
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 2 or not parts[0]:
        raise ValueError("expected: Name | Price | Description | Stock")
    return {
        "name": clip(parts[0], 100),
        "price": parse_price(parts[1]),
        "description": clip(parts[2], 800) if len(parts) > 2 else "",
        "stock": parse_stock(parts[3]) if len(parts) > 3 and parts[3] not in ("", "-") else None,
    }


def add_product(fields, photo_id=None):
    with db.lock:
        D["counters"]["product"] += 1
        pid = str(D["counters"]["product"])
        D["products"][pid] = {**fields, "photo_id": photo_id, "available": True, "created_at": now()}
        db.save()
    return pid


def create_order(uid, pend):
    with db.lock:
        p = D["products"].get(pend["pid"])
        if not p or not p["available"]:
            return None, "Sorry, this product is no longer available."
        if p["stock"] is not None:
            if p["stock"] < pend["qty"]:
                return None, f"Sorry, only {p['stock']} left in stock."
            p["stock"] -= pend["qty"]
        D["counters"]["order"] += 1
        oid = str(D["counters"]["order"])
        order = {"id": oid, "user_id": uid, "product_id": pend["pid"], "name": p["name"],
                 "price": p["price"], "qty": pend["qty"], "total": round(p["price"] * pend["qty"], 2),
                 "comment": pend.get("comment", ""), "photo_id": pend.get("photo_id"),
                 "status": "pending", "created_at": now(), "chat": []}
        D["orders"][oid] = order
        db.save()
    return order, None


def set_status(oid, new):
    with db.lock:
        o = D["orders"].get(oid)
        if not o:
            return None
        if new in ("rejected", "cancelled") and o["status"] in ACTIVE:
            p = D["products"].get(o["product_id"])
            if p and p["stock"] is not None:
                p["stock"] += o["qty"]          # give the items back
        o["status"] = new
        db.save()
        return o


def log_chat(oid, who, text):
    with db.lock:
        o = D["orders"].get(oid) if oid else None
        if o:
            o["chat"].append({"from": who, "text": clip(text, 300), "time": now()})
            del o["chat"][:-30]
            db.save()


# --------------------------------------------------------------------------
# Views: each returns (text, inline_markup, photo_file_id)
# --------------------------------------------------------------------------
def view_catalog(page=0):
    items = visible_products()
    if not items:
        return "The catalog is empty for now. Please check back later.", None, None
    chunk, page, pages = page_slice(items, page)
    rows = [[(f"{p['name']} — {fmt_price(p['price'])}" + (" (sold out)" if sold_out(p) else ""), f"p:{pid}")]
            for pid, p in chunk]
    rows.append(nav_row("cat", page, pages))
    title = "🛍 Catalog" + (f"  ·  page {page + 1}/{pages}" if pages > 1 else "")
    return title, kb(*rows), None


def view_product(pid):
    p = D["products"].get(pid)
    if not p or not p["available"]:
        return "This product is not available.", kb([("‹ Catalog", "cat:0")]), None
    lines = [p["name"], "", f"Price: {fmt_price(p['price'])}"]
    if p["stock"] is not None:
        lines.append(f"In stock: {p['stock']}")
    if p["description"]:
        lines += ["", p["description"]]
    rows = []
    if sold_out(p):
        lines += ["", "Sold out"]
    else:
        rows.append([("🛒 Order", f"q:{pid}:1")])
    rows.append([("‹ Catalog", "cat:0")])
    return "\n".join(lines), kb(*rows), p.get("photo_id")


def view_qty(pid, qty):
    p = D["products"].get(pid)
    if not p or not p["available"] or sold_out(p):
        return "This product is not available.", kb([("‹ Catalog", "cat:0")]), None
    top = min(MAX_QTY, p["stock"] if p["stock"] is not None else MAX_QTY)
    qty = max(1, min(int(qty), top))
    text = (f"{p['name']}\nPrice: {fmt_price(p['price'])}\nQuantity: {qty}\n"
            f"Total: {fmt_price(round(p['price'] * qty, 2))}")
    rows = [[("−", f"q:{pid}:{qty - 1}"), (str(qty), "noop"), ("+", f"q:{pid}:{qty + 1}")],
            [("Continue ›", f"buy:{pid}:{qty}")], [("‹ Back", f"p:{pid}")]]
    return text, kb(*rows), None


def view_details_prompt(pend):
    p = D["products"].get(pend["pid"], {})
    text = (f"Order: {p.get('name', '?')} × {pend['qty']}\n\n"
            "Send any details for this order (contact, account, delivery info) as a message or photo, "
            "or press Skip.")
    return text, kb([("Skip", "skip")], [("Cancel", "no")]), None


def view_confirm(pend):
    p = D["products"].get(pend["pid"], {})
    total = round(p.get("price", 0) * pend["qty"], 2)
    lines = ["Please confirm your order", "", f"Product: {p.get('name', '?')} × {pend['qty']}",
             f"Total: {fmt_price(total)}"]
    if pend.get("comment"):
        lines.append(f"Details: {pend['comment']}")
    rows = [[("✅ Confirm", "ok")], [("✏️ Change details", f"buy:{pend['pid']}:{pend['qty']}"), ("Cancel", "no")]]
    return "\n".join(lines), kb(*rows), pend.get("photo_id")


def order_text(o, admin=False):
    emoji, label = STATUS[o["status"]]
    lines = [f"Order #{o['id']}", f"Product: {o['name']} × {o['qty']}", f"Total: {fmt_price(o['total'])}",
             f"Status: {emoji} {label}", f"Date: {o['created_at']}"]
    if admin:
        lines.insert(1, f"Customer: {user_label(o['user_id'], full=True)}")
    if o.get("comment"):
        lines.append(f"Details: {o['comment']}")
    return "\n".join(lines)


def view_my_orders(uid, page=0):
    mine = [oid for oid in sorted_ids(D["orders"], reverse=True) if D["orders"][oid]["user_id"] == uid]
    if not mine:
        return "You have no orders yet.", None, None
    chunk, page, pages = page_slice(mine, page)
    rows = [[(f"#{oid} · {D['orders'][oid]['name']} ×{D['orders'][oid]['qty']} · "
              f"{STATUS[D['orders'][oid]['status']][0]}", f"o:{oid}")] for oid in chunk]
    rows.append(nav_row("mo", page, pages))
    return "🛒 Your orders", kb(*rows), None


def view_order(oid, admin=False, uid=None):
    o = D["orders"].get(oid)
    if not o or (not admin and o["user_id"] != uid):
        return "Order not found.", None, None
    rows, st = [], o["status"]
    if admin:
        if st == "pending":
            rows.append([("✅ Accept", f"aos:{oid}:accepted"), ("⛔ Reject", f"aos:{oid}:rejected")])
        elif st == "accepted":
            rows.append([("🔵 In progress", f"aos:{oid}:in_progress"), ("✅ Complete", f"aos:{oid}:completed")])
            rows.append([("❌ Cancel", f"aos:{oid}:cancelled")])
        elif st == "in_progress":
            rows.append([("✅ Complete", f"aos:{oid}:completed"), ("❌ Cancel", f"aos:{oid}:cancelled")])
        else:
            rows.append([("🗑 Delete", f"aod:{oid}")])
        rows.append([("💬 Message customer", f"reply:{o['user_id']}:{oid}")])
        rows.append([("‹ Orders", "ao:active:0")])
    else:
        if st == "pending":
            rows.append([("❌ Cancel order", f"oc:{oid}")])
        rows.append([("💬 Chat about this order", f"chat:{oid}")])
        rows.append([("‹ My orders", "mo:0")])
    return order_text(o, admin), kb(*rows), o.get("photo_id")


def view_admin_products(page=0):
    ids = sorted_ids(D["products"], reverse=True)
    chunk, page, pages = page_slice(ids, page)
    rows = []
    for pid in chunk:
        p = D["products"][pid]
        flags = ("" if p["available"] else " [hidden]") + (f" [stock {p['stock']}]" if p["stock"] is not None else "")
        rows.append([(f"#{pid} {p['name']} — {fmt_price(p['price'])}{flags}", f"apv:{pid}")])
    rows.append(nav_row("ap", page, pages))
    rows.append([("➕ Add product", "apa")])
    title = "📦 Products" if ids else "📦 No products yet. Tap “Add product” to create the first one."
    return title, kb(*rows), None


def view_admin_product(pid):
    p = D["products"].get(pid)
    if not p:
        return "Product not found.", kb([("‹ Products", "ap:0")]), None
    lines = [f"#{pid}  {p['name']}", "", f"Price: {fmt_price(p['price'])}",
             f"Stock: {p['stock'] if p['stock'] is not None else 'unlimited'}",
             f"Visibility: {'visible' if p['available'] else 'hidden'}"]
    if p["description"]:
        lines += ["", p["description"]]
    rows = [[("👁 Hide" if p["available"] else "👁 Show", f"apt:{pid}")],
            [("✏️ Name", f"ape:{pid}:name"), ("✏️ Price", f"ape:{pid}:price")],
            [("✏️ Description", f"ape:{pid}:description"), ("✏️ Stock", f"ape:{pid}:stock")],
            [("🖼 Photo", f"ape:{pid}:photo"), ("🗑 Delete", f"apd:{pid}")],
            [("‹ Products", "ap:0")]]
    return "\n".join(lines), kb(*rows), p.get("photo_id")


def view_admin_orders(flt="active", page=0):
    ids = [oid for oid in sorted_ids(D["orders"], reverse=True)
           if flt == "all" or D["orders"][oid]["status"] in ACTIVE]
    chunk, page, pages = page_slice(ids, page)
    rows = [[(("• " if flt == "active" else "") + "Active", "ao:active:0"),
             (("• " if flt == "all" else "") + "All", "ao:all:0")]]
    for oid in chunk:
        o = D["orders"][oid]
        rows.append([(f"#{oid} {STATUS[o['status']][0]} {o['name']} ×{o['qty']} · {user_label(o['user_id'])}",
                      f"aov:{oid}")])
    rows.append(nav_row(f"ao:{flt}", page, pages))
    return ("📊 Orders" if ids else "📊 No orders here yet."), kb(*rows), None


def view_admin_users(page=0):
    ids = [u for u in sorted(D["users"], key=lambda u: D["users"][u].get("joined", ""), reverse=True)
           if not is_admin(u)]
    if not ids:
        return "No customers yet.", None, None
    chunk, page, pages = page_slice(ids, page)
    rows = [[(("🔴 " if is_banned(u) else "🟢 ") + user_label(u), f"auv:{u}")] for u in chunk]
    rows.append(nav_row("au", page, pages))
    return "👥 Customers", kb(*rows), None


def view_admin_user(uid):
    u = D["users"].get(uid)
    if not u:
        return "User not found.", kb([("‹ Customers", "au:0")]), None
    orders = [o for o in D["orders"].values() if o["user_id"] == uid]
    text = (f"{user_label(uid, full=True)}\nJoined: {u.get('joined', '?')}\n"
            f"Orders: {len(orders)}\nStatus: {'banned' if is_banned(uid) else 'active'}")
    rows = [[("🔓 Unban", f"auu:{uid}") if is_banned(uid) else ("🔒 Ban", f"aub:{uid}")],
            [("💬 Message", f"reply:{uid}:0")], [("‹ Customers", "au:0")]]
    return text, kb(*rows), None


# --------------------------------------------------------------------------
# Chat relay
# --------------------------------------------------------------------------
def relay_to_admins(m, uid, oid):
    header = f"💬 {user_label(uid, full=True)}" + (f" · order #{oid}" if oid else " · support")
    markup = kb([("↩️ Reply", f"reply:{uid}:{oid or 0}")])
    delivered = 0
    for a in admins():
        try:
            bot.send_message(int(a), header, reply_markup=markup)
            bot.copy_message(int(a), m.chat.id, m.message_id)
            delivered += 1
        except Exception as e:
            log.warning("Relay to admin %s failed: %s", a, e)
    log_chat(oid, "user", describe(m))
    if not delivered:
        bot.send_message(m.chat.id, "Sorry, nobody from the shop is reachable right now. Please try later.")


def relay_to_user(m, target, oid):
    header = "💬 Message from the shop" + (f" · order #{oid}" if oid else "")
    try:
        bot.send_message(int(target), header, reply_markup=kb([("↩️ Reply", f"chat:{oid or 0}")]))
        bot.copy_message(int(target), m.chat.id, m.message_id)
    except Exception as e:
        bot.send_message(m.chat.id, f"Could not deliver the message: {e}")
        return
    log_chat(oid, "shop", describe(m))
    bot.send_message(m.chat.id, f"✓ Sent to {user_label(target)}",
                     reply_markup=kb([("Close chat", "close")]))


# --------------------------------------------------------------------------
# Menu actions
# --------------------------------------------------------------------------
def do_catalog(m):
    send_view(m.chat.id, view_catalog(0))


def do_my_orders(m):
    uid = str(m.from_user.id)
    if is_admin(uid):
        send_view(m.chat.id, view_admin_orders("active", 0))
    else:
        send_view(m.chat.id, view_my_orders(uid, 0))


def do_support(m):
    uid = str(m.from_user.id)
    CURRENT_CHAT[uid] = {"order": None}
    bot.send_message(m.chat.id, "💬 You are now chatting with the shop. Write your message here.",
                     reply_markup=kb([("Close chat", "close")]))


def do_about(m):
    bot.send_message(m.chat.id, ABOUT_TEXT)


ADD_HELP = ("Send products, one per line:\n"
            "Name | Price | Description | Stock\n\n"
            "Description and stock are optional (empty stock = unlimited).\n"
            "Attach a photo and put the line in the caption to add a photo (one product at a time).\n\n"
            "Example:\nPremium account | 9.99 | Lifetime access | 10\n"
            "T-shirt | 15\n\n"
            "Send more any time. Press a menu button or /cancel to finish.")


def start_add(chat_id, uid):
    clear_state(uid)
    ADMIN_STATE[uid] = {"action": "add"}
    bot.send_message(chat_id, ADD_HELP)


def do_products(m):
    send_view(m.chat.id, view_admin_products(0))


def do_add(m):
    start_add(m.chat.id, str(m.from_user.id))


def do_admin_orders(m):
    send_view(m.chat.id, view_admin_orders("active", 0))


def do_admin_users(m):
    send_view(m.chat.id, view_admin_users(0))


def do_leave(m):
    uid = str(m.from_user.id)
    if uid in ENV_ADMINS:
        bot.send_message(m.chat.id, "You are an admin through ADMIN_IDS. Remove your ID from .env to revoke it.")
        return
    with db.lock:
        if uid in D["admins"]:
            D["admins"].remove(uid)
            db.save()
    bot.send_message(m.chat.id, "You left the admin panel.", reply_markup=menu_for(uid))


USER_ACTIONS = {BTN_CATALOG: do_catalog, BTN_ORDERS: do_my_orders, BTN_SUPPORT: do_support, BTN_ABOUT: do_about}
ADMIN_ACTIONS = {BTN_PRODUCTS: do_products, BTN_ADD: do_add, BTN_AORDERS: do_admin_orders,
                 BTN_USERS: do_admin_users, BTN_LEAVE: do_leave}


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def guarded(fn):
    @wraps(fn)
    def wrapper(m):
        uid = str(m.from_user.id)
        if is_banned(uid) and not is_admin(uid):
            bot.send_message(m.chat.id, "You are banned from using this bot.")
            return
        if rate_limited(uid):
            return
        track_user(m.from_user)
        return fn(m)
    return wrapper


@bot.message_handler(commands=["start", "menu"], chat_types=["private"])
@guarded
def cmd_start(m):
    uid = str(m.from_user.id)
    clear_state(uid)
    bot.send_message(m.chat.id, WELCOME_TEXT if not is_admin(uid) else "Admin panel", reply_markup=menu_for(uid))


@bot.message_handler(commands=["help"], chat_types=["private"])
@guarded
def cmd_help(m):
    text = ("/start or /menu: main menu\n/orders: your orders\n/support: contact the shop\n"
            "/cancel: cancel the current action")
    if is_admin(m.from_user.id):
        text += "\n\nAdmin: use the menu buttons to add products, manage orders and message customers."
    bot.send_message(m.chat.id, text)


@bot.message_handler(commands=["orders"], chat_types=["private"])
@guarded
def cmd_orders(m):
    do_my_orders(m)


@bot.message_handler(commands=["support"], chat_types=["private"])
@guarded
def cmd_support(m):
    do_support(m)


@bot.message_handler(commands=["cancel"], chat_types=["private"])
@guarded
def cmd_cancel(m):
    uid = str(m.from_user.id)
    clear_state(uid)
    bot.send_message(m.chat.id, "Cancelled.", reply_markup=menu_for(uid))


@bot.message_handler(commands=["admin"], chat_types=["private"])
@guarded
def cmd_admin(m):
    uid = str(m.from_user.id)
    if is_admin(uid):
        bot.send_message(m.chat.id, "Admin panel", reply_markup=menu_for(uid))
        return
    if not ADMIN_PASSWORD:
        bot.send_message(m.chat.id, "Admin login is disabled.")
        return
    recent = [t for t in LOGIN_FAILS[uid] if time.time() - t < LOGIN_ATTEMPTS[1]]
    if len(recent) >= LOGIN_ATTEMPTS[0]:
        bot.send_message(m.chat.id, "Too many wrong attempts. Try again in 10 minutes.")
        return
    clear_state(uid)
    LOGIN_PENDING.add(uid)
    bot.send_message(m.chat.id, "Enter the admin password:", reply_markup=types.ReplyKeyboardRemove())


# --------------------------------------------------------------------------
# Regular messages
# --------------------------------------------------------------------------
def handle_login(m, uid):
    LOGIN_PENDING.discard(uid)
    ok = (m.content_type == "text" and ADMIN_PASSWORD and
          hmac.compare_digest(m.text.strip().encode(), ADMIN_PASSWORD.encode()))
    try:
        bot.delete_message(m.chat.id, m.message_id)      # don't leave the password in the chat
    except Exception:
        pass
    if ok:
        with db.lock:
            if uid not in D["admins"]:
                D["admins"].append(uid)
                db.save()
        bot.send_message(m.chat.id, "You are now an admin.", reply_markup=menu_for(uid))
    else:
        LOGIN_FAILS[uid].append(time.time())
        bot.send_message(m.chat.id, "Wrong password. Send /admin to try again.", reply_markup=menu_for(uid))


def handle_add(m, uid):
    text = m.text or m.caption or ""
    if m.content_type not in ("text", "photo") or not text.strip():
        bot.send_message(m.chat.id, "Please send product lines as text (or a photo with the line as caption).")
        return
    photo = m.photo[-1].file_id if m.content_type == "photo" else None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if photo and len(lines) != 1:
        bot.send_message(m.chat.id, "With a photo, send exactly one product line in the caption.")
        return
    added, errors = [], []
    for i, line in enumerate(lines, 1):
        try:
            fields = parse_product_line(line)
        except ValueError as e:
            errors.append(f"line {i}: {e}")
            continue
        added.append(f"#{add_product(fields, photo)} {fields['name']} — {fmt_price(fields['price'])}")
    reply = []
    if added:
        reply.append("Added:\n" + "\n".join(added))
    if errors:
        reply.append("Skipped:\n" + "\n".join(errors))
    reply.append("Send more products, or press a menu button to finish.")
    bot.send_message(m.chat.id, "\n\n".join(reply))


EDIT_PROMPTS = {
    "name": "Send the new name.",
    "price": "Send the new price, e.g. 9.99",
    "description": "Send the new description (or - to clear it).",
    "stock": "Send the stock quantity (or - for unlimited).",
    "photo": "Send a new photo (or - to remove the current one).",
}


def apply_edit(p, field, m):
    text = m.text.strip() if m.content_type == "text" else None
    if field == "photo":
        if m.content_type == "photo":
            p["photo_id"] = m.photo[-1].file_id
        elif text == "-":
            p["photo_id"] = None
        else:
            raise ValueError("Send a photo, or - to remove the current one.")
        return
    if text is None:
        raise ValueError("Please send text.")
    if field == "name":
        if not text:
            raise ValueError("Name can't be empty.")
        p["name"] = clip(text, 100)
    elif field == "price":
        p["price"] = parse_price(text)
    elif field == "description":
        p["description"] = "" if text == "-" else clip(text, 800)
    elif field == "stock":
        p["stock"] = None if text in ("-", "") else parse_stock(text)


def handle_edit(m, uid):
    st = ADMIN_STATE[uid]
    with db.lock:
        p = D["products"].get(st["pid"])
        if not p:
            ADMIN_STATE.pop(uid, None)
            bot.send_message(m.chat.id, "That product no longer exists.")
            return
        try:
            apply_edit(p, st["field"], m)
        except ValueError as e:
            bot.send_message(m.chat.id, f"{e}\nTry again or /cancel.")
            return
        db.save()
    ADMIN_STATE.pop(uid, None)
    send_view(m.chat.id, view_admin_product(st["pid"]))


def handle_details(m, uid):
    pend = PENDING[uid]
    if pend["stage"] == "confirm":
        bot.send_message(m.chat.id, "Please press Confirm or Cancel on your order above.")
        return
    if m.content_type == "text":
        pend["comment"] = clip(m.text, 1000)
    elif m.content_type == "photo":
        pend["photo_id"] = m.photo[-1].file_id
        pend["comment"] = clip(m.caption or "", 1000)
    else:
        bot.send_message(m.chat.id, "Please send text or a photo, or press Skip.")
        return
    pend["stage"] = "confirm"
    send_view(m.chat.id, view_confirm(pend))


@bot.message_handler(content_types=["text", "photo", "document", "video", "voice", "audio",
                                    "sticker", "video_note", "animation"], chat_types=["private"])
@guarded
def on_message(m):
    uid = str(m.from_user.id)
    txt = m.text or ""

    if uid in LOGIN_PENDING:
        handle_login(m, uid)
        return

    action = USER_ACTIONS.get(txt) or (ADMIN_ACTIONS.get(txt) if is_admin(uid) else None)
    if action:                                  # a menu button always wins over any pending state
        clear_state(uid)
        action(m)
        return

    if is_admin(uid):
        st = ADMIN_STATE.get(uid)
        if st:
            (handle_add if st["action"] == "add" else handle_edit)(m, uid)
        elif uid in CURRENT_CHAT:
            chat = CURRENT_CHAT[uid]
            relay_to_user(m, chat["target"], chat["order"])
        else:
            bot.send_message(m.chat.id, "Use the menu buttons below.", reply_markup=menu_for(uid))
        return

    if uid in PENDING:
        handle_details(m, uid)
    elif uid in CURRENT_CHAT:
        relay_to_admins(m, uid, CURRENT_CHAT[uid]["order"])
    else:
        bot.send_message(m.chat.id, "Use the menu below to browse the catalog, or press Support to contact us.",
                         reply_markup=menu_for(uid))


# --------------------------------------------------------------------------
# Inline buttons
# --------------------------------------------------------------------------
CALLBACKS = {}


def cb(name, admin=False):
    def deco(fn):
        CALLBACKS[name] = (fn, admin)
        return fn
    return deco


@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    uid = str(call.from_user.id)
    action, *args = (call.data or "").split(":")
    call.answered = False
    entry = CALLBACKS.get(action)
    try:
        if entry is None:
            pass
        elif is_banned(uid) and not is_admin(uid):
            toast(call, "You are banned.", alert=True)
        elif entry[1] and not is_admin(uid):
            toast(call, "Admins only.", alert=True)
        else:
            track_user(call.from_user)
            entry[0](call, uid, *args)
    except Exception:
        log.exception("Callback %s failed", call.data)
        toast(call, "Something went wrong.", alert=True)
    if not call.answered:
        toast(call)


@cb("noop")
def cb_noop(call, uid):
    pass


# ---- customer side ----
@cb("cat")
def cb_cat(call, uid, page="0"):
    render(call, view_catalog(page))


@cb("p")
def cb_product(call, uid, pid):
    render(call, view_product(pid))


@cb("q")
def cb_qty(call, uid, pid, qty):
    render(call, view_qty(pid, qty))


@cb("buy")
def cb_buy(call, uid, pid, qty):
    p = D["products"].get(pid)
    if not p or not p["available"] or sold_out(p):
        toast(call, "This product is not available.", alert=True)
        return
    top = min(MAX_QTY, p["stock"] if p["stock"] is not None else MAX_QTY)
    CURRENT_CHAT.pop(uid, None)
    PENDING[uid] = {"pid": pid, "qty": max(1, min(int(qty), top)), "comment": "", "photo_id": None,
                    "stage": "details"}
    render(call, view_details_prompt(PENDING[uid]))


@cb("skip")
def cb_skip(call, uid):
    pend = PENDING.get(uid)
    if not pend:
        toast(call, "Nothing to continue.", alert=True)
        return
    pend["stage"] = "confirm"
    render(call, view_confirm(pend))


@cb("no")
def cb_no(call, uid):
    PENDING.pop(uid, None)
    render(call, ("Order cancelled.", None, None))


@cb("ok")
def cb_confirm(call, uid):
    pend = PENDING.get(uid)
    if not pend:
        toast(call, "Nothing to confirm.", alert=True)
        return
    order, error = create_order(uid, pend)
    if error:
        PENDING.pop(uid, None)
        render(call, (error, kb([("‹ Catalog", "cat:0")]), None))
        return
    PENDING.pop(uid, None)
    render(call, (f"✅ Order #{order['id']} placed!\n\n{order['name']} × {order['qty']}\n"
                  f"Total: {fmt_price(order['total'])}\n\nWe will contact you shortly.",
                  kb([("📦 View order", f"o:{order['id']}")]), None))
    notify_admins("🔔 New order\n\n" + order_text(order, admin=True), photo_id=order["photo_id"],
                  markup=kb([("Open", f"aov:{order['id']}"), ("💬 Message", f"reply:{uid}:{order['id']}")]))


@cb("mo")
def cb_my_orders(call, uid, page="0"):
    render(call, view_my_orders(uid, page))


@cb("o")
def cb_order(call, uid, oid):
    render(call, view_order(oid, admin=False, uid=uid))


@cb("oc")
def cb_cancel_order(call, uid, oid):
    o = D["orders"].get(oid)
    if not o or o["user_id"] != uid or o["status"] != "pending":
        toast(call, "This order can no longer be cancelled.", alert=True)
        return
    set_status(oid, "cancelled")
    render(call, view_order(oid, admin=False, uid=uid))
    notify_admins(f"❌ Order #{oid} was cancelled by the customer ({user_label(uid)}).")


@cb("chat")
def cb_chat(call, uid, oid="0"):
    order = None
    if oid != "0":
        o = D["orders"].get(oid)
        if not o or o["user_id"] != uid:
            toast(call, "Order not found.", alert=True)
            return
        order = oid
    PENDING.pop(uid, None)
    CURRENT_CHAT[uid] = {"order": order}
    text = "💬 You are now chatting with the shop" + (f" about order #{order}" if order else "") + \
           ". Write your message here."
    bot.send_message(call.message.chat.id, text, reply_markup=kb([("Close chat", "close")]))


@cb("close")
def cb_close(call, uid):
    CURRENT_CHAT.pop(uid, None)
    render(call, ("Chat closed.", None, None))


# ---- admin side ----
@cb("ap", admin=True)
def cb_ap(call, uid, page="0"):
    render(call, view_admin_products(page))


@cb("apv", admin=True)
def cb_apv(call, uid, pid):
    render(call, view_admin_product(pid))


@cb("apt", admin=True)
def cb_apt(call, uid, pid):
    with db.lock:
        p = D["products"].get(pid)
        if p:
            p["available"] = not p["available"]
            db.save()
    render(call, view_admin_product(pid))


@cb("apa", admin=True)
def cb_apa(call, uid):
    start_add(call.message.chat.id, uid)


@cb("ape", admin=True)
def cb_ape(call, uid, pid, field):
    if pid not in D["products"] or field not in EDIT_PROMPTS:
        toast(call, "Product not found.", alert=True)
        return
    clear_state(uid)
    ADMIN_STATE[uid] = {"action": "edit", "pid": pid, "field": field}
    bot.send_message(call.message.chat.id, EDIT_PROMPTS[field] + "\n/cancel to abort.")


@cb("apd", admin=True)
def cb_apd(call, uid, pid):
    p = D["products"].get(pid)
    if not p:
        toast(call, "Product not found.", alert=True)
        return
    render(call, (f"Delete “{p['name']}”? Existing orders are not affected.",
                  kb([("🗑 Yes, delete", f"apdy:{pid}"), ("Cancel", f"apv:{pid}")]), None))


@cb("apdy", admin=True)
def cb_apdy(call, uid, pid):
    with db.lock:
        D["products"].pop(pid, None)
        db.save()
    render(call, view_admin_products(0))


@cb("ao", admin=True)
def cb_ao(call, uid, flt="active", page="0"):
    render(call, view_admin_orders(flt if flt in ("active", "all") else "active", page))


@cb("aov", admin=True)
def cb_aov(call, uid, oid):
    render(call, view_order(oid, admin=True))


@cb("aos", admin=True)
def cb_aos(call, uid, oid, status):
    if status not in STATUS or status == "pending":
        toast(call, "Unknown status.", alert=True)
        return
    o = set_status(oid, status)
    if not o:
        toast(call, "Order not found.", alert=True)
        return
    messages = {"accepted": "✅ Your order #{} was accepted.", "in_progress": "🔵 Your order #{} is now in progress.",
                "completed": "🎉 Your order #{} is completed. Thank you!", "rejected": "⛔ Your order #{} was rejected.",
                "cancelled": "❌ Your order #{} was cancelled by the shop."}
    try:
        bot.send_message(int(o["user_id"]), messages[status].format(oid),
                         reply_markup=kb([("📦 View order", f"o:{oid}")]))
    except Exception as e:
        log.warning("Could not notify customer: %s", e)
    render(call, view_order(oid, admin=True))


@cb("aod", admin=True)
def cb_aod(call, uid, oid):
    with db.lock:
        o = D["orders"].get(oid)
        if o and o["status"] not in ACTIVE:
            del D["orders"][oid]
            db.save()
    render(call, view_admin_orders("active", 0))


@cb("au", admin=True)
def cb_au(call, uid, page="0"):
    render(call, view_admin_users(page))


@cb("auv", admin=True)
def cb_auv(call, uid, target):
    render(call, view_admin_user(target))


@cb("aub", admin=True)
def cb_aub(call, uid, target):
    if is_admin(target):
        toast(call, "You can't ban an admin.", alert=True)
        return
    with db.lock:
        if target not in D["banned"]:
            D["banned"].append(target)
            db.save()
    render(call, view_admin_user(target))


@cb("auu", admin=True)
def cb_auu(call, uid, target):
    with db.lock:
        if target in D["banned"]:
            D["banned"].remove(target)
            db.save()
    render(call, view_admin_user(target))


@cb("reply", admin=True)
def cb_reply(call, uid, target, oid="0"):
    ADMIN_STATE.pop(uid, None)
    CURRENT_CHAT[uid] = {"target": target, "order": None if oid == "0" else oid}
    text = f"💬 Chatting with {user_label(target)}" + (f" about order #{oid}" if oid != "0" else "") + \
           ". Everything you send now goes to the customer."
    bot.send_message(call.message.chat.id, text, reply_markup=kb([("Close chat", "close")]))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def run_web():
    from flask import Flask
    app = Flask(__name__)

    @app.route("/")
    @app.route("/health")
    def health():
        return "OK", 200

    app.run(host="0.0.0.0", port=PORT)


def main():
    log.info("%s started. Products: %d, orders: %d, admins: %d", SHOP_NAME,
             len(D["products"]), len(D["orders"]), len(admins()))
    if not admins() and not ADMIN_PASSWORD:
        log.warning("No admins configured: set ADMIN_PASSWORD or ADMIN_IDS in .env")
    try:
        bot.remove_webhook()
        bot.set_my_commands([types.BotCommand("start", "Main menu"), types.BotCommand("orders", "My orders"),
                             types.BotCommand("support", "Contact the shop"), types.BotCommand("help", "Help"),
                             types.BotCommand("admin", "Admin login"), types.BotCommand("cancel", "Cancel current action")])
    except Exception as e:
        log.warning("Startup call failed: %s", e)
    if ENABLE_WEB:
        threading.Thread(target=run_web, daemon=True).start()
    bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
    main()
