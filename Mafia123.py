# mafia_bot.py
# -*- coding: utf-8 -*-
"""
True Mafia Bot - O'zbekcha qiziqarli versiya
Admin paneli INLINE TUGMALAR bilan
DAYDI (Dyadya) ro'li qo'shildi - Ovoz berishda maxsus kuch
Render serveriga moslashtirilgan versiya
"""

import os
import json
import random
import logging
import threading
import time
from collections import Counter
from typing import Dict, Any, List, Optional

from telebot import TeleBot, types

# ============================ KONFIGURATSIYA ============================
TOKEN = os.getenv("MAFIA_BOT_TOKEN", "8216533427:AAEkuTATPEXPJPlfrhQ6n3NAINt5Mwpzu5c")
ADMIN_IDS = {int(os.getenv("MAFIA_ADMIN_ID", "7935854444"))} 
BOT_USERNAME = os.getenv("MAFIA_BOT_USERNAME", "mafiaganneBot")

DATA_DIR = "data"
PROFILES_FILE = os.path.join(DATA_DIR, "profiles.json")
GAMES_FILE = os.path.join(DATA_DIR, "games.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")

REGISTRATION_TIMEOUT = 60
MIN_PLAYERS = 3
DAY_TIMEOUT = 30
NIGHT_TIMEOUT = 60

PRICE_PER_DIAMOND = 3000
DEFAULT_PACKS = [1, 5, 10, 50, 100]

# Bot xabarlarini o'chirish uchun
bot_message_history: Dict[str, List[int]] = {}  # chat_id -> [message_ids]

# ============================ LOGGING ============================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("mafia_bot")

# ============================ BOT ============================
bot = TeleBot(TOKEN, parse_mode="HTML")

# ============================ GLOBAL STATE ============================
LOCK = threading.RLock()

profiles: Dict[str, Dict[str, Any]] = {}
games: Dict[str, Dict[str, Any]] = {}
history: List[Dict[str, Any]] = []
diamond_orders: Dict[str, Dict[str, Any]] = {}
waiting_for_custom_amount: set[int] = set()
waiting_for_check: Dict[int, str] = {}  
waiting_for_broadcast: Dict[int, str] = {}  # admin -> broadcast message
waiting_for_admin_add: Dict[int, Any] = {}  # admin -> waiting for user_id
waiting_for_admin_remove: Dict[int, bool] = {}  # admin -> waiting for user_id
timers: Dict[str, Dict[str, Optional[threading.Timer]]] = {}

# ============================ PERSISTENCE ============================
def ensure_data_dir() -> None:
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)

def default_serializer(obj):
    if isinstance(obj, set):
        return list(obj)
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

def save_json(path: str, data: Any) -> None:
    try:
        ensure_data_dir()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=default_serializer)
        os.replace(tmp, path)
    except Exception as e:
        logger.exception("save_json %s error: %s", path, e)

def load_json(path: str):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.exception("load_json %s error: %s", path, e)
    return None

def persist_profiles() -> None:
    with LOCK:
        save_json(PROFILES_FILE, profiles)

def persist_games() -> None:
    safe_games = {}
    with LOCK:
        for k, g in games.items():
            copyg = {}
            for kk, vv in g.items():
                if isinstance(vv, set):
                    copyg[kk] = list(vv)
                else:
                    copyg[kk] = vv
            safe_games[k] = copyg
        save_json(GAMES_FILE, safe_games)

def persist_history() -> None:
    with LOCK:
        save_json(HISTORY_FILE, history)

def persist_admins() -> None:
    with LOCK:
        save_json(ADMINS_FILE, list(ADMIN_IDS))

def persist_all() -> None:
    persist_profiles()
    persist_games()
    persist_history()
    persist_admins()

# initial load
with LOCK:
    profiles.update(load_json(PROFILES_FILE) or {})
    games.update(load_json(GAMES_FILE) or {})
    history = load_json(HISTORY_FILE) or []
    loaded_admins = load_json(ADMINS_FILE)
    if loaded_admins:
        ADMIN_IDS.update(set(loaded_admins))

# ============================ YORDAMCHI FUNKSIYALAR ============================
def uid_str(uid: int) -> str:
    return str(int(uid))

def cid_str(cid: int) -> str:
    return str(int(cid))

def get_username_obj(user) -> str:
    try:
        if getattr(user, "username", None):
            return f"@{user.username}"
        if getattr(user, "first_name", None):
            return user.first_name
        return str(user.id)
    except Exception:
        return str(getattr(user, "id", "unknown"))

def get_username_id(uid: int) -> str:
    try:
        ch = bot.get_chat(uid)
        if getattr(ch, "username", None):
            return f"@{ch.username}"
        if getattr(ch, "first_name", None):
            return ch.first_name
        return str(uid)
    except Exception:
        return str(uid)

def ensure_profile(uid: int, name: str = "") -> Dict[str, Any]:
    key = uid_str(uid)
    with LOCK:
        if key not in profiles:
            profiles[key] = {
                "name": name or str(uid),
                "money": 0,
                "diamonds": 0,
                "doctor_save_used": False,
                "guaranteed_active_role": False,
                "protection_active": False,
                "games_played": 0,
                "wins": 0,
            }
            persist_profiles()
        else:
            prof = profiles[key]
            prof.setdefault("money", 0)
            prof.setdefault("diamonds", 0)
            prof.setdefault("doctor_save_used", False)
            prof.setdefault("guaranteed_active_role", False)
            prof.setdefault("protection_active", False)
            prof.setdefault("games_played", 0)
            prof.setdefault("wins", 0)
    return profiles[key]

# YANGI: Bot xabarlarini saqlash va o'chirish funksiyalari
def add_bot_message_to_history(chat_id: int, message_id: int) -> None:
    """Bot yuborgan xabarlar ID sini saqlash"""
    key = cid_str(chat_id)
    with LOCK:
        if key not in bot_message_history:
            bot_message_history[key] = []
        bot_message_history[key].append(message_id)
        
        # Faqat oxirgi 50 ta xabarni saqlash
        if len(bot_message_history[key]) > 50:
            bot_message_history[key] = bot_message_history[key][-50:]

def cleanup_bot_messages(chat_id: int) -> None:
    """Botning barcha xabarlarini o'chirish"""
    key = cid_str(chat_id)
    message_ids = []
    
    with LOCK:
        if key in bot_message_history:
            message_ids = bot_message_history.pop(key, [])
    
    # Har bir xabarni o'chirish
    for msg_id in message_ids:
        try:
            safe_api(bot.delete_message, chat_id, msg_id)
        except Exception as e:
            # Agar xabar allaqachon o'chirilgan bo'lsa, xato berishi mumkin
            pass

def cleanup_old_bot_messages(chat_id: int, keep_last: int = 5) -> None:
    """Eski bot xabarlarini o'chirish, faqat oxirgi bir nechtasini saqlash"""
    key = cid_str(chat_id)
    to_delete = []  # Xatoni bartaraf etish uchun
    
    with LOCK:
        if key in bot_message_history and len(bot_message_history[key]) > keep_last:
            # Eski xabarlarni olish
            to_delete = bot_message_history[key][:-keep_last]
            # Faqat oxirgi keep_last ta xabarni saqlash
            bot_message_history[key] = bot_message_history[key][-keep_last:]
    
    # Eski xabarlarni o'chirish
    for msg_id in to_delete:
        try:
            safe_api(bot.delete_message, chat_id, msg_id)
        except Exception:
            pass

# YANGI: Obuna tekshirish funksiyalari
def check_user_subscribed(user_id: int) -> bool:
    """Foydalanuvchi botga /start bosganligini tekshirish"""
    try:
        # Foydalanuvchi profilida "money" maydoni borligini tekshirish
        # Agar profil mavjud bo'lsa, u /start bosgan deb hisoblanadi
        ensure_profile(user_id)
        return True
    except Exception:
        return False

# YANGI: Safe send funksiyasi - xabar yuborilganda uni saqlash
def safe_send_message(chat_id, text, **kwargs):
    """Xabar yuborish va uni saqlash"""
    sent_msg = safe_api(bot.send_message, chat_id, text, **kwargs)
    if sent_msg:
        add_bot_message_to_history(chat_id, sent_msg.message_id)
    return sent_msg

def safe_send_and_reply(chat_id, reply_to_msg_id, text, **kwargs):
    """Xabar yuborish va javob berish"""
    sent_msg = safe_api(bot.send_message, chat_id, text, reply_to_message_id=reply_to_msg_id, **kwargs)
    if sent_msg:
        add_bot_message_to_history(chat_id, sent_msg.message_id)
    return sent_msg

def safe_api(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        error_msg = str(e).lower()
        
        if "bot can't initiate conversation" in error_msg:
            logger.debug("Foydalanuvchi botga /start yozmagan. Xabar yuborish mumkin emas.")
            return None
        
        elif "query is too old" in error_msg or "query id is invalid" in error_msg:
            logger.debug("Callback query eskirgan yoki noto'g'ri ID.")
            return None
        
        elif "connection aborted" in error_msg or "connection reset" in error_msg:
            logger.debug("Internet aloqasi uzildi.")
            return None
        
        elif "read timed out" in error_msg or "timeout" in error_msg:
            logger.debug("Telegram API ga murojaat vaqti tugadi.")
            return None
        
        elif "api was unsuccessful" in error_msg:
            logger.debug(f"Telegram API xatosi: {e}")
            return None
        
        else:
            logger.debug(f"API call failed: {fn.__name__}, error: {e}")
            return None

def safe_answer_callback(cb_query, text=None, show_alert=False):
    try:
        if text:
            return safe_api(bot.answer_callback_query, cb_query.id, text, show_alert=show_alert)
        else:
            return safe_api(bot.answer_callback_query, cb_query.id)
    except Exception as e:
        logger.debug(f"answer_callback failed: {e}")
        return None

# ============================ INLINE TUGMALAR (ADMIN PANEL) ============================
def admin_panel_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎮 O'yin boshqarish", callback_data="admin_games"),
        types.InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="admin_users")
    )
    kb.add(
        types.InlineKeyboardButton("💎 To'lovlar", callback_data="admin_payments"),
        types.InlineKeyboardButton("📢 Xabar yuborish", callback_data="admin_broadcast")
    )
    kb.add(
        types.InlineKeyboardButton("⚙️ Sozlamalar", callback_data="admin_settings"),
        types.InlineKeyboardButton("📊 Statistika", callback_data="admin_stats")
    )
    kb.add(
        types.InlineKeyboardButton("➕ Admin qo'shish", callback_data="admin_add"),
        types.InlineKeyboardButton("➖ Admin olib tashlash", callback_data="admin_remove")
    )
    return kb

def admin_games_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    with LOCK:
        active_games = [(cid, g) for cid, g in games.items() if g.get("state") == "started"]
    
    if active_games:
        for cid, game in active_games[:5]:
            chat_title = f"Chat {cid}"
            try:
                chat = bot.get_chat(int(cid))
                chat_title = chat.title[:20] if chat.title else f"Chat {cid}"
            except:
                pass
            
            kb.add(types.InlineKeyboardButton(
                f"⏹ {chat_title}", 
                callback_data=f"admin_endgame:{cid}"
            ))
    
    kb.add(
        types.InlineKeyboardButton("🔄 Faol o'yinlar", callback_data="admin_games"),
        types.InlineKeyboardButton("🔙 Orqaga", callback_data="admin_back")
    )
    return kb

def admin_users_markup(page: int = 0) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    with LOCK:
        user_list = list(profiles.items())
    
    items_per_page = 5
    start = page * items_per_page
    end = start + items_per_page
    
    for uid, prof in user_list[start:end]:
        name = prof.get("name", "Noma'lum")[:15]
        kb.add(types.InlineKeyboardButton(
            f"👤 {name} | 💰{prof.get('money',0)} | 💎{prof.get('diamonds',0)}",
            callback_data=f"admin_user_detail:{uid}"
        ))
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton("⬅️ Oldingi", callback_data=f"admin_users:{page-1}"))
    
    if end < len(user_list):
        nav_buttons.append(types.InlineKeyboardButton("Keyingi ➡️", callback_data=f"admin_users:{page+1}"))
    
    if nav_buttons:
        kb.add(*nav_buttons)
    
    kb.add(types.InlineKeyboardButton("🔙 Orqaga", callback_data="admin_back"))
    return kb

def admin_payments_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    with LOCK:
        pending_orders = [o for o in diamond_orders.values() if o.get("status") == "pending"]
    
    if pending_orders:
        for order in pending_orders[:5]:
            user_id = order.get("user_id")
            username = get_username_id(user_id)[:15]
            kb.add(types.InlineKeyboardButton(
                f"💰 {username} | {order.get('count',0)}💎", 
                callback_data=f"admin_payment:{user_id}"
            ))
    else:
        kb.add(types.InlineKeyboardButton("📭 Kutilayotgan to'lovlar yo'q", callback_data="none"))
    
    kb.add(
        types.InlineKeyboardButton("🔄 Yangilash", callback_data="admin_payments"),
        types.InlineKeyboardButton("🔙 Orqaga", callback_data="admin_back")
    )
    return kb

def admin_user_detail_markup(uid: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("💰 Pul qo'shish", callback_data=f"admin_add_money:{uid}"),
        types.InlineKeyboardButton("💎 Olmos qo'shish", callback_data=f"admin_add_diamonds:{uid}")
    )
    kb.add(
        types.InlineKeyboardButton("📊 Statistika", callback_data=f"admin_user_stats:{uid}"),
        types.InlineKeyboardButton("🔙 Orqaga", callback_data="admin_users")
    )
    return kb

# ============================ QIZIQARLI XABARLAR ============================
def funny_start_message(name: str) -> str:
    messages = [
        f"🎭 Salom {name}! True Mafia botiga xush kelibsiz!",
        f"😎 Voy {name}! Mafia o'ynashni xohlaysizmi?",
        f"👋 {name}! Qo'rqmayman desangiz, o'yinga qo'shiling!"
    ]
    return random.choice(messages)

def funny_game_start_message() -> str:
    messages = [
        "🎲 *MAFIA O'YINI BOSHLANDI!*",
        "🔥 *O'YIN BOSHLANMOQDA!*",
        "🎪 *SIRLI O'YIN BOSHLANDI!*"
    ]
    return random.choice(messages)

def funny_player_joined_message(name: str) -> str:
    messages = [
        f"😎 {name} o'yinga qo'shildi!",
        f"👤 {name} safimizga qo'shildi!",
        f"🎯 {name} o'yinga kirildi!"
    ]
    return random.choice(messages)

def funny_role_messages(role: str) -> str:
    role_messages = {
        "🤵🏻 Дон": [
            "😈 *TABRIKLAYMIZ!* Siz — MAFIA DONSIZ!\n\nVazifangiz:\n• Tunda odamlarni 'yo'q qilsh'\n• Kunduzi esa 'men begunohman' deb o'tirish"
        ],
        "💉 Доктор": [
            "💉 *SIZ — DOKTORSIZ!*\n\n🩺 Har kecha bitta odamni davolaysiz\n❗ Lekin faqat 1 marta qutqarish huquqingiz bor!"
        ],
        "🕵️ Комиссар": [
            "🕵️ *SIZ — KOMISSARSIZ!*\n\n🔍 Tunda bitta odamni tekshirasiz\n🤫 Lekin topganingizni hammaga aytmaysiz"
        ],
        "👨🏼 Мирный житель": [
            "🙂 *SIZ — ODDIY FUQAROSIZ!*\n\n🗣 Gapiring, fikr bildiring\n🤔 O'ylang va tahlil qiling\n😵 Va tirik qolishga harakat qiling!"
        ],
        "👴 Daydi": [
            "👴 *SIZ — DAYDISIZ!*\n\n📢 Sizning ovozingiz maxsus kuchga ega:\n"
            "• Agar siz kimnidir O'LDIRISHGA ovoz bersangiz, faqat sizning tanlovingiz amal qiladi\n"
            "• Agar siz kimnidir O'LDIRMASLIKKA ovoz bersangiz, u odam o'lib bo'lmaydi\n\n"
            "⚠️ Eslatma: Sizning ovozingiz boshqalarning o'zaro ovozlaridan ustun!"
        ]
    }
    return random.choice(role_messages.get(role, ["Sizning rol: " + role]))

def funny_night_message() -> str:
    messages = [
        "🌙 *TUN TUSHDI...*",
        "🌑 *QORONG'U TUN...*",
        "⭐ *YULDUZLI TUN...*"
    ]
    return random.choice(messages)

def funny_day_message(victim_name: str = None) -> str:
    if victim_name:
        messages = [
            f"☀️ *ERTALAB...*\n\n☠️ Yomon xabar — {victim_name} kechani omon o'ta olmadi",
            f"🌅 *SABAH...*\n\n💀 {victim_name} o'ldi!"
        ]
    else:
        messages = [
            "☀️ *ERTALAB...*\n\n😮 Ajab! Bugun hamma tirik!",
            "🌞 *YORQIN KUN...*\n\n🎊 Hamma tirik va sog'!"
        ]
    return random.choice(messages)

def funny_vote_message(voter: str, target: str) -> str:
    messages = [
        f"🗳 {voter} ovoz berdi!\n👀 Kimga? {target}ga!",
        f"📢 {voter}: 'Men {target}ga ishonmayman!'",
        f"🎤 {voter}ning qo'li ko'tarildi!\nMaqsad: {target}"
    ]
    return random.choice(messages)

def funny_execution_message(victim: str, role: str) -> str:
    messages = [
        f"⚖️ *HUKM CHIQDI!*\n\n😬 {victim} qatl qilindi!\nU aslida: {role} edi!",
        f"🔨 *JAZO AMALGA OSHIRILDI!*\n\n👋 Xayr, {victim}!\n🌹 Siz {role} edingiz...",
        f"💥 *PORTLASH!*\n\n😵 {victim} yo'qoldi!\n🎭 Rol: {role}"
    ]
    return random.choice(messages)

def funny_victory_message(winner: str) -> str:
    if winner == "Мирные жители":
        messages = [
            "🎉 *TABRIKLAYMIZ!* FUQAROLAR YUTDI!",
            "🏆 *G'ALABA!* TINCH AHOLI YUTDI!",
            "✨ *YORQIN KELAJAK!* MAFIYA MAG'LUB BO'LDI!"
        ]
    else:
        messages = [
            "💀 *MAFIA G'ALABA QOZONDI!*",
            "👑 *QORONG'U HUKMRONLIK!* MAFIA YUTDI!",
            "⚫ *SIYOSIY INQILOB!* MAFIYA HOKIMIYATNI QO'LGA KIRITDI!"
        ]
    return random.choice(messages)

# ============================ ASOSIY KOMANDALAR ============================
@bot.message_handler(commands=['start'])
def cmd_start(msg):
    try:
        ensure_profile(msg.from_user.id, get_username_obj(msg.from_user))
        prof = profiles[uid_str(msg.from_user.id)]
        
        if msg.chat.type == "private":
            welcome_text = funny_start_message(prof["name"])
            safe_api(bot.send_message, msg.from_user.id, welcome_text)
            
            main_kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
            main_kb.add("🎮 O'ynash", "👤 Mening profilim")
            main_kb.add("💎 Olmoslar", "ℹ️ Yordam")
            
            safe_api(bot.send_message, msg.from_user.id, 
                    "👇 Quyidagi tugmalardan birini tanlang:", 
                    reply_markup=main_kb)
            
            add_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("➕ Guruhga qo'shish", url=add_url))
            safe_api(bot.send_message, msg.from_user.id, 
                    "🎭 Do'stlaringiz bilan o'ynash uchun meni guruhga qo'shing:", 
                    reply_markup=kb)
        else:
            safe_send_and_reply(msg.chat.id, msg.message_id,
                "🎮 Men - True Mafia botiman!\n\n"
                "📋 Komandalar:\n"
                "/startgame - O'yin boshlash\n"
                "/begin - Ro'yxat to'lsa boshlash\n"
                "/endgame - O'yinni to'xtatish\n\n"
                "⚠️ O'ynash uchun avval menga /start yozing!")
    except Exception as e:
        logger.exception("start failed: %s", e)

@bot.message_handler(commands=['admin'])
def admin_cmd(msg):
    uid = msg.from_user.id
    if uid not in ADMIN_IDS:
        safe_send_and_reply(msg.chat.id, msg.message_id, "🚫 Bu buyruq faqat adminlar uchun!")
        return
    
    with LOCK:
        active_games = sum(1 for g in games.values() if g.get("state") == "started")
        pending_orders = sum(1 for o in diamond_orders.values() if o.get("status") == "pending")
    
    admin_text = (
        "👑 *ADMIN PANELI*\n\n"
        "Kerakli bo'limni tanlang 👇\n\n"
        "📊 Statistika:\n"
        f"• Foydalanuvchilar: {len(profiles)}\n"
        f"• Faol o'yinlar: {active_games}\n"
        f"• Kutilayotgan to'lovlar: {pending_orders}\n"
        f"• Adminlar: {len(ADMIN_IDS)}"
    )
    
    safe_api(bot.send_message, uid, admin_text, reply_markup=admin_panel_markup())

# ============================ ADMIN CALLBACK HANDLERS ============================
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith('admin_'))
def admin_callback_handler(call):
    uid = call.from_user.id
    if uid not in ADMIN_IDS:
        safe_answer_callback(call, "🚫 Siz admin emassiz!", show_alert=True)
        return
    
    data = call.data
    
    if data == "admin_back":
        with LOCK:
            active_games = sum(1 for g in games.values() if g.get("state") == "started")
            pending_orders = sum(1 for o in diamond_orders.values() if o.get("status") == "pending")
        
        admin_text = (
            "👑 *ADMIN PANELI*\n\n"
            "Kerakli bo'limni tanlang 👇\n\n"
            "📊 Statistika:\n"
            f"• Foydalanuvchilar: {len(profiles)}\n"
            f"• Faol o'yinlar: {active_games}\n"
            f"• Kutilayotgan to'lovlar: {pending_orders}\n"
            f"• Adminlar: {len(ADMIN_IDS)}"
        )
        
        safe_api(bot.edit_message_text, admin_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=admin_panel_markup())
        safe_answer_callback(call)
    
    elif data == "admin_games":
        active_games = sum(1 for g in games.values() if g.get("state") == "started")
        text = f"🎮 *FAOL O'YINLAR: {active_games} ta*\n\n"
        
        if active_games > 0:
            text += "Quyidagi o'yinlarni to'xtatishingiz mumkin:"
        else:
            text += "Hozircha faol o'yinlar yo'q"
        
        safe_api(bot.edit_message_text, text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=admin_games_markup())
        safe_answer_callback(call)
    
    elif data == "admin_users":
        text = f"👥 *FOYDALANUVCHILAR: {len(profiles)} ta*\n\n"
        text += "Foydalanuvchilar ro'yxati:"
        
        safe_api(bot.edit_message_text, text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=admin_users_markup())
        safe_answer_callback(call)
    
    elif data.startswith("admin_users:"):
        page = int(data.split(":")[1])
        text = f"👥 *FOYDALANUVCHILAR - {page+1}-sahifa*\n\n"
        text += "Foydalanuvchilar ro'yxati:"
        
        safe_api(bot.edit_message_text, text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=admin_users_markup(page))
        safe_answer_callback(call)
    
    elif data.startswith("admin_user_detail:"):
        user_id = data.split(":")[1]
        prof = ensure_profile(int(user_id))
        
        text = (
            f"👤 *FOYDALANUVCHI TAFSILOTLARI*\n\n"
            f"📛 Ism: {prof['name']}\n"
            f"🆔 ID: {user_id}\n"
            f"💰 Pul: {prof['money']} so'm\n"
            f"💎 Olmoslar: {prof['diamonds']} ta\n"
            f"🛡 Himoya: {'🟢 Aktiv' if prof['protection_active'] else '🔴 O\'chirilgan'}\n"
            f"🎭 Aktiv rol: {'✅ Kafolat' if prof['guaranteed_active_role'] else '❌ Yo\'q'}\n\n"
            f"Quyidagi amallarni bajarishingiz mumkin:"
        )
        
        safe_api(bot.edit_message_text, text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=admin_user_detail_markup(user_id))
        safe_answer_callback(call)
    
    elif data == "admin_payments":
        with LOCK:
            pending = sum(1 for o in diamond_orders.values() if o.get("status") == "pending")
        
        text = f"💎 *KUTILAYOTGAN TO'LOVLAR: {pending} ta*\n\n"
        
        if pending > 0:
            text += "Tasdiqlash uchun tugmani bosing:"
        else:
            text += "Hozircha kutilayotgan to'lovlar yo'q"
        
        safe_api(bot.edit_message_text, text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=admin_payments_markup())
        safe_answer_callback(call)
    
    elif data.startswith("admin_payment:"):
        user_id = int(data.split(":")[1])
        
        with LOCK:
            order_id = next((oid for oid, o in diamond_orders.items() 
                           if o["user_id"] == user_id and o["status"] == "pending"), None)
            order = diamond_orders.get(order_id)
        
        if not order:
            safe_answer_callback(call, "⚠️ To'lov topilmadi!", show_alert=True)
            return
        
        username = get_username_id(user_id)
        text = (
            f"💰 *TO'LOVNI BOSHQARISH*\n\n"
            f"👤 Foydalanuvchi: {username}\n"
            f"🆔 ID: {user_id}\n"
            f"💎 Olmoslar: {order.get('count', 0)} ta\n"
            f"💵 Summa: {order.get('price', 0)} so'm\n\n"
            f"Nima qilamiz?"
        )
        
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"admin_confirm:{user_id}"),
            types.InlineKeyboardButton("❌ Bekor qilish", callback_data=f"admin_cancel:{user_id}")
        )
        kb.add(types.InlineKeyboardButton("🔙 Orqaga", callback_data="admin_payments"))
        
        safe_api(bot.edit_message_text, text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=kb)
        safe_answer_callback(call)
    
    elif data.startswith("admin_confirm:"):
        user_id = int(data.split(":")[1])
        
        with LOCK:
            order_id = next((oid for oid, o in diamond_orders.items() 
                           if o["user_id"] == user_id and o["status"] == "pending"), None)
            order = diamond_orders.get(order_id)
            
            if order:
                prof = ensure_profile(user_id, get_username_id(user_id))
                prof["diamonds"] += order["count"]
                waiting_for_check.pop(user_id, None)
                diamond_orders.pop(order_id, None)
                persist_profiles()
        
        safe_api(bot.send_message, user_id,
                f"🎉 *TO'LOVINGIZ TASDIQLANDI!*\n\n"
                f"💎 Sizga {order['count']} ta olmos qo'shildi!\n"
                f"💰 Jami olmoslar: {profiles.get(uid_str(user_id), {}).get('diamonds', 0)} ta\n\n"
                f"🤝 Rahmat! Yana buyurtma berishingiz mumkin!")
        
        safe_api(bot.edit_message_text, "✅ To'lov tasdiqlandi!",
                call.message.chat.id,
                call.message.message_id)
        
        threading.Timer(2, lambda: bot.edit_message_text(
            "👑 *ADMIN PANELI*\n\nTo'lov muvaffaqiyatli tasdiqlandi!",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=admin_panel_markup()
        )).start()
        
        safe_answer_callback(call, "✅ To'lov tasdiqlandi!")
    
    elif data.startswith("admin_cancel:"):
        user_id = int(data.split(":")[1])
        
        with LOCK:
            order_id = next((oid for oid, o in diamond_orders.items() 
                           if o["user_id"] == user_id and o["status"] == "pending"), None)
            if order_id:
                diamond_orders.pop(order_id, None)
                waiting_for_check.pop(user_id, None)
        
        safe_api(bot.send_message, user_id,
                "❌ *TO'LOVINGIZ BEKOR QILINDI*\n\n"
                "Afsuski, to'lovingiz tasdiqlanmadi.\n"
                "Qayta urinib ko'ring yoki admin bilan bog'laning.")
        
        safe_api(bot.edit_message_text, "❌ To'lov bekor qilindi!",
                call.message.chat.id,
                call.message.message_id)
        
        threading.Timer(2, lambda: bot.edit_message_text(
            "👑 *ADMIN PANELI*\n\nTo'lov bekor qilindi!",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=admin_panel_markup()
        )).start()
        
        safe_answer_callback(call, "❌ To'lov bekor qilindi!")
    
    elif data == "admin_broadcast":
        waiting_for_broadcast[uid] = True
        safe_api(bot.edit_message_text,
                "📢 *HAMMAGA XABAR YUBORISH*\n\n"
                "Yubormoqchi bo'lgan xabaringizni yozing:",
                call.message.chat.id,
                call.message.message_id)
        safe_answer_callback(call)
    
    elif data == "admin_add":
        waiting_for_admin_add[uid] = True
        safe_api(bot.edit_message_text,
                "➕ *ADMIN QO'SHISH*\n\n"
                "Qo'shmoqchi bo'lgan adminning ID raqamini yozing:",
                call.message.chat.id,
                call.message.message_id)
        safe_answer_callback(call)
    
    elif data == "admin_remove":
        waiting_for_admin_remove[uid] = True
        safe_api(bot.edit_message_text,
                "➖ *ADMIN OLIB TASHLASH*\n\n"
                "Olib tashlamoqchi bo'lgan adminning ID raqamini yozing:",
                call.message.chat.id,
                call.message.message_id)
        safe_answer_callback(call)
    
    elif data.startswith("admin_endgame:"):
        chat_id = int(data.split(":")[1])
        
        with LOCK:
            game = games.get(cid_str(chat_id))
        
        if game and game.get("state") == "started":
            send_final_stats_and_cleanup(chat_id, "Admin tomonidan to'xtatildi")
            text = "✅ O'yin muvaffaqiyatli to'xtatildi!"
        else:
            text = "⚠️ Faol o'yin topilmadi"
        
        safe_api(bot.edit_message_text, text,
                call.message.chat.id,
                call.message.message_id)
        
        threading.Timer(2, lambda: bot.edit_message_text(
            "👑 *ADMIN PANELI*",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=admin_panel_markup()
        )).start()
        
        safe_answer_callback(call, text)
    
    elif data == "admin_stats":
        with LOCK:
            total_players = len(profiles)
            total_money = sum(p.get("money", 0) for p in profiles.values())
            total_diamonds = sum(p.get("diamonds", 0) for p in profiles.values())
            active_games = sum(1 for g in games.values() if g.get("state") == "started")
            pending_orders = sum(1 for o in diamond_orders.values() if o.get("status") == "pending")
        
        text = (
            "📊 *BOT STATISTIKASI*\n\n"
            f"👥 Foydalanuvchilar: {total_players} ta\n"
            f"💰 Umumiy pul: {total_money} so'm\n"
            f"💎 Umumiy olmoslar: {total_diamonds} ta\n"
            f"🎮 Faol o'yinlar: {active_games} ta\n"
            f"⏳ Kutilayotgan to'lovlar: {pending_orders} ta\n"
            f"👑 Adminlar: {len(ADMIN_IDS)} ta\n\n"
            "📈 Ma'lumotlar real vaqtda yangilanadi"
        )
        
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔄 Yangilash", callback_data="admin_stats"))
        kb.add(types.InlineKeyboardButton("🔙 Orqaga", callback_data="admin_back"))
        
        safe_api(bot.edit_message_text, text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=kb)
        safe_answer_callback(call)
    
    elif data == "admin_settings":
        text = (
            "⚙️ *BOT SOZLAMALARI*\n\n"
            "📋 Hozirgi sozlamalar:\n"
            f"• Minimal o'yinchilar: {MIN_PLAYERS} ta\n"
            f"• Kun vaqti: {DAY_TIMEOUT} soniya\n"
            f"• Tun vaqti: {NIGHT_TIMEOUT} soniya\n"
            f"• Ro'yxat vaqti: {REGISTRATION_TIMEOUT} soniya\n"
            f"• Olmos narxi: {PRICE_PER_DIAMOND} so'm\n\n"
            "🛠 Sozlamalar tez orada o'zgartiriladi..."
        )
        
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 Orqaga", callback_data="admin_back"))
        
        safe_api(bot.edit_message_text, text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=kb)
        safe_answer_callback(call)
    
    elif data.startswith("admin_add_money:"):
        user_id = data.split(":")[1]
        waiting_for_admin_add[uid] = {"type": "money", "target": user_id}
        safe_api(bot.edit_message_text,
                f"💰 *PUL QO'SHISH*\n\n"
                f"{get_username_id(int(user_id))} hisobiga qancha pul qo'shmoqchisiz?\n"
                f"Summani butun son bilan kiriting:",
                call.message.chat.id,
                call.message.message_id)
        safe_answer_callback(call)
    
    elif data.startswith("admin_add_diamonds:"):
        user_id = data.split(":")[1]
        waiting_for_admin_add[uid] = {"type": "diamonds", "target": user_id}
        safe_api(bot.edit_message_text,
                f"💎 *OLMOS QO'SHISH*\n\n"
                f"{get_username_id(int(user_id))} hisobiga qancha olmos qo'shmoqchisiz?\n"
                f"Sonini kiriting:",
                call.message.chat.id,
                call.message.message_id)
        safe_answer_callback(call)
    
    else:
        safe_answer_callback(call, "⚠️ Noma'lum buyruq!")

# ============================ ADMIN TEXT HANDLERS ============================
@bot.message_handler(func=lambda m: m.from_user.id in waiting_for_broadcast)
def handle_broadcast_message(msg):
    uid = msg.from_user.id
    if uid not in ADMIN_IDS:
        return
    
    if uid in waiting_for_broadcast:
        waiting_for_broadcast.pop(uid, None)
        
        broadcast_msg = msg.text
        sent_count = 0
        failed_count = 0
        
        with LOCK:
            user_ids = list(profiles.keys())
        
        for user_key in user_ids:
            try:
                safe_api(bot.send_message, int(user_key),
                        f"📢 *BOTDAN MUHIM XABAR:*\n\n{broadcast_msg}")
                sent_count += 1
            except Exception:
                failed_count += 1
        
        admin_text = (
            f"📢 *XABAR YUBORILDI!*\n\n"
            f"✅ Muvaffaqiyatli: {sent_count} ta\n"
            f"❌ Xato: {failed_count} ta\n\n"
            f"Xabaringiz {sent_count} ta foydalanuvchiga yetkazildi."
        )
        
        safe_api(bot.send_message, uid, admin_text, reply_markup=admin_panel_markup())

@bot.message_handler(func=lambda m: m.from_user.id in waiting_for_admin_add and isinstance(waiting_for_admin_add[m.from_user.id], bool))
def handle_admin_add(msg):
    uid = msg.from_user.id
    if uid not in ADMIN_IDS:
        return
    
    if uid in waiting_for_admin_add and waiting_for_admin_add[uid] is True:
        waiting_for_admin_add.pop(uid, None)
        
        try:
            new_admin_id = int(msg.text)
            
            with LOCK:
                if new_admin_id in ADMIN_IDS:
                    safe_api(bot.send_message, uid,
                            f"⚠️ {get_username_id(new_admin_id)} allaqachon admin!",
                            reply_markup=admin_panel_markup())
                    return
                
                ADMIN_IDS.add(new_admin_id)
                persist_admins()
            
            try:
                safe_api(bot.send_message, new_admin_id,
                        "🎉 *SIZ ADMIN BO'LDINGIZ!*\n\n"
                        "Endi sizda botni boshqarish huquqi bor.\n"
                        "/admin buyrug'i orqali panelga kirishingiz mumkin.")
            except:
                pass
            
            safe_api(bot.send_message, uid,
                    f"✅ {get_username_id(new_admin_id)} admin qo'shildi!",
                    reply_markup=admin_panel_markup())
            
        except ValueError:
            safe_api(bot.send_message, uid,
                    "⚠️ ID raqam noto'g'ri! Qayta urinib ko'ring.",
                    reply_markup=admin_panel_markup())

@bot.message_handler(func=lambda m: m.from_user.id in waiting_for_admin_add and isinstance(waiting_for_admin_add[m.from_user.id], dict))
def handle_admin_add_value(msg):
    uid = msg.from_user.id
    if uid not in ADMIN_IDS:
        return
    
    if uid in waiting_for_admin_add and isinstance(waiting_for_admin_add[uid], dict):
        data = waiting_for_admin_add.pop(uid)
        action_type = data["type"]
        target_user_id = int(data["target"])
        
        try:
            amount = int(msg.text)
            if amount <= 0:
                safe_api(bot.send_message, uid,
                        "⚠️ Summa musbat bo'lishi kerak!",
                        reply_markup=admin_panel_markup())
                return
            
            with LOCK:
                prof = ensure_profile(target_user_id)
                
                if action_type == "money":
                    prof["money"] += amount
                    action_name = "pul"
                    emoji = "💰"
                elif action_type == "diamonds":
                    prof["diamonds"] += amount
                    action_name = "olmos"
                    emoji = "💎"
                else:
                    safe_api(bot.send_message, uid,
                            "⚠️ Noma'lum amal turi!",
                            reply_markup=admin_panel_markup())
                    return
                
                persist_profiles()
            
            try:
                safe_api(bot.send_message, target_user_id,
                        f"{emoji} *ADMIN YORDAMI!*\n\n"
                        f"Sizning hisobingizga {amount} {action_name} qo'shildi!\n"
                        f"💰 Jami: {prof['money']} so'm\n"
                        f"💎 Jami: {prof['diamonds']} olmos")
            except:
                pass
            
            safe_api(bot.send_message, uid,
                    f"✅ {get_username_id(target_user_id)} hisobiga {amount} {action_name} qo'shildi!",
                    reply_markup=admin_panel_markup())
            
        except ValueError:
            safe_api(bot.send_message, uid,
                    "⚠️ Noto'g'ri format! Faqat raqam kiriting.",
                    reply_markup=admin_panel_markup())

@bot.message_handler(func=lambda m: m.from_user.id in waiting_for_admin_remove)
def handle_admin_remove(msg):
    uid = msg.from_user.id
    if uid not in ADMIN_IDS:
        return
    
    if uid in waiting_for_admin_remove:
        waiting_for_admin_remove.pop(uid, None)
        
        try:
            remove_admin_id = int(msg.text)
            
            with LOCK:
                if remove_admin_id not in ADMIN_IDS:
                    safe_api(bot.send_message, uid,
                            f"⚠️ {get_username_id(remove_admin_id)} admin emas!",
                            reply_markup=admin_panel_markup())
                    return
                
                if remove_admin_id == uid:
                    safe_api(bot.send_message, uid,
                            "⚠️ O'zingizni olib tashlay olmaysiz!",
                            reply_markup=admin_panel_markup())
                    return
                
                ADMIN_IDS.discard(remove_admin_id)
                persist_admins()
            
            try:
                safe_api(bot.send_message, remove_admin_id,
                        "❌ *SIZ ADMINLIKDAN OLIB TASHLANDINGIZ!*\n\n"
                        "Endi sizda botni boshqarish huquqi yo'q.")
            except:
                pass
            
            safe_api(bot.send_message, uid,
                    f"✅ {get_username_id(remove_admin_id)} adminlikdan olib tashlandi!",
                    reply_markup=admin_panel_markup())
            
        except ValueError:
            safe_api(bot.send_message, uid,
                    "⚠️ ID raqam noto'g'ri! Qayta urinib ko'ring.",
                    reply_markup=admin_panel_markup())

# ============================ OLMOS SOTIB OLISH FUNKSIYALARI ============================
def show_order_confirmation(uid: int, order_id: str) -> None:
    with LOCK:
        order = diamond_orders.get(order_id)
    
    if not order:
        safe_api(bot.send_message, uid, "⚠️ <b>Buyurtma topilmadi!</b>")
        return
    
    text = (
        f"💎 <b>Siz {order['count']} ta olmos sotib olmoqchisiz</b>\n\n"
        f"💰 Narxi: {order['price']} so'm\n\n"
        "💳 <b>To'lov kartasi:</b>\n"
        "<code>4073 4200 4285 8328</code>\n"
        "<i>ILHOMJON SUPIJANOV</i>\n\n"
        "💡 <b>Yo'riqnoma:</b>\n"
        "1. Yuqoridagi karta raqamiga to'lov qiling\n"
        "2. Chekni (foto yoki fayl) yuboring\n"
        "3. Keyin '✅ Tasdiqlash' tugmasini bosing\n\n"
        "❌ '❌ Bekor qilish' tugmasini bossangiz, buyurtma bekor qilinadi"
    )
    
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Tasdiqlash", callback_data="confirm_order"),
        types.InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_order")
    )
    
    safe_api(bot.send_message, uid, text, reply_markup=kb)

def profile_reply_markup(uid: Optional[int] = None) -> types.ReplyKeyboardMarkup:
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    
    if uid:
        prof = ensure_profile(uid)
        
        if prof["diamonds"] > 0:
            m.add("💠 Olmos ishlatish")
        
        if prof["money"] >= 100 and not prof["protection_active"]:
            m.add("🛡 Himoya sotib olish (100 so'm)")
    
    m.add("💎 Olmos sotib olish")
    m.add("🏠 Bosh menyu")
    
    return m

@bot.callback_query_handler(func=lambda c: c.data and (c.data.startswith("buy_") or c.data in ["buy_custom", "confirm_order", "cancel_order", "profile_back"]))
def buy_callback_handler(call):
    uid = call.from_user.id
    data = call.data
    
    try:
        if data == "profile_back":
            safe_answer_callback(call)
            cmd_profile(types.Message(
                message_id=call.message.message_id,
                from_user=call.from_user,
                date=int(time.time()),
                chat=types.Chat(id=uid, type="private"),
                content_type="text",
                json_string="",
                options={}
            ))
            return
        
        if data == "buy_custom":
            waiting_for_custom_amount.add(uid)
            safe_api(bot.send_message, uid, 
                    "✏️ <b>Iltimos, olmos sonini kiriting (butun son):</b>\n"
                    "Masalan: 7, 15, 25")
            safe_answer_callback(call, "📝 Sonni xabarda yuboring")
            return

        if data.startswith("buy_"):
            try:
                cnt = int(data.split("_", 1)[1])
                if cnt <= 0:
                    safe_answer_callback(call, "❌ Noto'g'ri son!", show_alert=True)
                    return
                    
                with LOCK:
                    order_id = f"{uid}_{int(time.time())}"
                    diamond_orders[order_id] = {
                        "user_id": uid,
                        "count": cnt,
                        "price": cnt * PRICE_PER_DIAMOND,
                        "status": "new",
                        "created_at": int(time.time())
                    }
                    waiting_for_check[uid] = order_id
                
                show_order_confirmation(uid, order_id)
                safe_answer_callback(call, "✅ Paket tanlandi!")
                
            except ValueError:
                safe_answer_callback(call, "❌ Noto'g'ri format!", show_alert=True)
            return

        if data == "confirm_order":
            with LOCK:
                order_id = waiting_for_check.get(uid)
                order = diamond_orders.get(order_id)
                
                if not order:
                    safe_answer_callback(call, "❌ Buyurtma topilmadi!", show_alert=True)
                    return
                    
                if order["status"] != "new":
                    safe_answer_callback(call, "❌ Buyurtma allaqachon ko'rib chiqilgan!", show_alert=True)
                    return
                    
                order["status"] = "pending"
                order["confirmed_at"] = int(time.time())
            
            user_name = get_username_id(uid)
            for admin_id in ADMIN_IDS:
                try:
                    admin_text = (
                        f"🛒 <b>YANGI OLMOS BUYURTMASI!</b>\n\n"
                        f"👤 <b>Foydalanuvchi:</b> {user_name}\n"
                        f"🆔 <b>ID:</b> {uid}\n"
                        f"💎 <b>Olmoslar:</b> {order['count']} ta\n"
                        f"💰 <b>Summa:</b> {order['price']} so'm\n"
                        f"⏰ <b>Vaqt:</b> {time.strftime('%H:%M:%S')}\n\n"
                        f"✅ Tasdiqlash: <code>/confirm {uid}</code>\n"
                        f"❌ Bekor qilish: <code>/cancel {uid}</code>"
                    )
                    safe_api(bot.send_message, admin_id, admin_text)
                except Exception:
                    logger.exception(f"Admin {admin_id} ga xabar yuborishda xato")
            
            safe_answer_callback(call, "✅ Buyurtma adminlarga yuborildi!")
            safe_api(bot.send_message, uid, 
                    "📤 <b>Buyurtmangiz adminlarga yuborildi!</b>\n\n"
                    "💰 <b>To'lov qilganingizdan so'ng, chekni (foto yoki fayl) yuboring.</b>\n"
                    "⏳ Adminlar tez orada javob beradi.")
            return

        if data == "cancel_order":
            with LOCK:
                order_id = waiting_for_check.pop(uid, None)
                if order_id:
                    diamond_orders.pop(order_id, None)
            
            safe_answer_callback(call, "❌ Buyurtma bekor qilindi!")
            safe_api(bot.send_message, uid, 
                    "❌ <b>Buyurtma bekor qilindi!</b>\n\n"
                    "💎 Olmos sotib olish uchun qayta urinib ko'ring.",
                    reply_markup=profile_reply_markup(uid=uid))
            return

    except Exception as e:
        logger.exception(f"buy_callback_handler xatosi: {e}")
        safe_answer_callback(call, "❌ Xatolik yuz berdi!", show_alert=True)

@bot.message_handler(func=lambda m: m.chat.type == "private" and m.from_user.id in waiting_for_custom_amount)
def handle_custom_amount(message):
    user_id = message.from_user.id
    if user_id not in waiting_for_custom_amount:
        return
    
    waiting_for_custom_amount.discard(user_id)
    text = message.text.strip()
    
    try:
        count = int(text)
        if count <= 0:
            safe_api(bot.send_message, user_id, "❗ <b>Iltimos, musbat son kiriting.</b>")
            return
            
        with LOCK:
            order_id = f"{user_id}_{int(time.time())}"
            diamond_orders[order_id] = {
                "user_id": user_id,
                "count": count,
                "price": count * PRICE_PER_DIAMOND,
                "status": "new",
                "created_at": int(time.time())
            }
            waiting_for_check[user_id] = order_id
        
        show_order_confirmation(user_id, order_id)
        
    except ValueError:
        safe_api(bot.send_message, user_id, 
                "❗ <b>Noto'g'ri format!</b>\n"
                "Iltimos, faqat raqam kiriting.\n"
                "Masalan: <code>15</code> yoki <code>25</code>")

@bot.message_handler(content_types=["photo", "document"], func=lambda m: m.chat.type == "private" and m.from_user.id in waiting_for_check)
def handle_check(msg):
    uid = msg.from_user.id
    
    with LOCK:
        order_id = waiting_for_check.get(uid)
        order = diamond_orders.get(order_id)
    
    if not order:
        safe_api(bot.send_message, uid, "⚠️ <b>Buyurtma topilmadi yoki muddati o'tgan!</b>")
        return
    
    if order.get("status") != "pending":
        safe_api(bot.send_message, uid, "⚠️ <b>Buyurtma allaqachon ko'rib chiqilgan!</b>")
        return
    
    check_content = None
    if msg.photo:
        check_content = msg.photo[-1]
        file_type = "photo"
    elif msg.document:
        check_content = msg.document
        file_type = "document"
    else:
        safe_api(bot.send_message, uid, "⚠️ <b>Iltimos, faqat rasm yoki fayl yuboring!</b>")
        return
    
    check_id = check_content.file_id
    
    user_name = get_username_id(uid)
    caption = (
        f"🧾 <b>YANGI CHEK KELDI!</b>\n\n"
        f"👤 <b>Foydalanuvchi:</b> {user_name}\n"
        f"🆔 <b>ID:</b> {uid}\n"
        f"💎 <b>Olmoslar:</b> {order['count']} ta\n"
        f"💰 <b>Summa:</b> {order['price']} so'm\n\n"
        f"✅ Tasdiqlash: <code>/confirm {uid}</code>\n"
        f"❌ Bekor qilish: <code>/cancel {uid}</code>"
    )
    
    sent_to_admins = False
    for admin_id in ADMIN_IDS:
        try:
            if file_type == "photo":
                safe_api(bot.send_photo, admin_id, check_id, caption=caption)
            else:
                safe_api(bot.send_document, admin_id, check_id, caption=caption)
            sent_to_admins = True
        except Exception:
            logger.exception(f"Chekni admin {admin_id} ga yuborishda xato")
    
    if sent_to_admins:
        safe_api(bot.send_message, uid,
                "📸 <b>Chek qabul qilindi!</b>\n\n"
                "✅ Chek adminlarga yuborildi.\n"
                "⏳ To'lov tekshirilgandan so'ng, olmoslar hisobingizga qo'shiladi.\n"
                "🗓️ Taxminiy vaqt: 5-30 daqiqa")
    else:
        safe_api(bot.send_message, uid,
                "❌ <b>Xatolik yuz berdi!</b>\n\n"
                "Chekni adminlarga yuborib bo'lmadi.\n"
                "Iltimos, keyinroq qayta urinib ko'ring.")
    
    with LOCK:
        if order_id in diamond_orders:
            diamond_orders[order_id]["check_id"] = check_id
            diamond_orders[order_id]["check_type"] = file_type
            diamond_orders[order_id]["check_sent_at"] = int(time.time())

@bot.message_handler(commands=["confirm"])
def admin_confirm_order(message):
    if message.from_user.id not in ADMIN_IDS:
        safe_send_and_reply(message.chat.id, message.message_id, "🚫 <b>Bu buyruq faqat adminlar uchun!</b>")
        return
    
    parts = message.text.split()
    if len(parts) != 2:
        safe_send_and_reply(message.chat.id, message.message_id, 
                "📝 <b>Foydalanish:</b>\n"
                "<code>/confirm &lt;user_id&gt;</code>\n\n"
                "Masalan: <code>/confirm 123456789</code>")
        return
    
    try:
        user_id = int(parts[1])
    except ValueError:
        safe_send_and_reply(message.chat.id, message.message_id, "❌ <b>Noto'g'ri ID format!</b>\nID faqat raqamlardan iborat bo'lishi kerak.")
        return
    
    with LOCK:
        order_id = next(
            (oid for oid, o in diamond_orders.items() 
             if o["user_id"] == user_id and o.get("status") == "pending"),
            None
        )
        
        if not order_id:
            safe_send_and_reply(message.chat.id, message.message_id, 
                    f"❌ <b>{get_username_id(user_id)} uchun kutilayotgan buyurtma topilmadi!</b>\n"
                    "Yoki buyurtma allaqachon tasdiqlangan/bekor qilingan.")
            return
        
        order = diamond_orders[order_id]
        
        prof = ensure_profile(user_id, get_username_id(user_id))
        prof["diamonds"] += order["count"]
        
        waiting_for_check.pop(user_id, None)
        diamond_orders.pop(order_id, None)
        
        persist_profiles()
    
    try:
        safe_api(bot.send_message, user_id,
                f"🎉 <b>TO'LOVINGIZ TASDIQLANDI!</b>\n\n"
                f"💎 <b>{order['count']} ta olmos</b> hisobingizga qo'shildi!\n"
                f"💰 <b>Jami olmoslar:</b> {prof['diamonds']} ta\n\n"
                f"🤝 <b>Rahmat! O'yinda omad!</b>\n\n"
                f"💡 Keyingi o'yinda faqat aktiv rol olasiz!")
    except Exception:
        logger.exception(f"Foydalanuvchi {user_id} ga xabar yuborishda xato")
    
    username = get_username_id(user_id)
    safe_send_and_reply(message.chat.id, message.message_id,
            f"✅ <b>{username} uchun to'lov tasdiqlandi!</b>\n\n"
            f"💎 Olmoslar: {order['count']} ta\n"
            f"💰 Summa: {order['price']} so'm\n"
            f"👤 Foydalanuvchi: {username}\n"
            f"🆔 ID: {user_id}")

@bot.message_handler(commands=["cancel"])
def admin_cancel_order(message):
    if message.from_user.id not in ADMIN_IDS:
        safe_send_and_reply(message.chat.id, message.message_id, "🚫 <b>Bu buyruq faqat adminlar uchun!</b>")
        return
    
    parts = message.text.split()
    if len(parts) != 2:
        safe_send_and_reply(message.chat.id, message.message_id,
                "📝 <b>Foydalanish:</b>\n"
                "<code>/cancel &lt;user_id&gt;</code>\n\n"
                "Masalan: <code>/cancel 123456789</code>")
        return
    
    try:
        user_id = int(parts[1])
    except ValueError:
        safe_send_and_reply(message.chat.id, message.message_id, "❌ <b>Noto'g'ri ID format!</b>")
        return
    
    with LOCK:
        order_id = next(
            (oid for oid, o in diamond_orders.items() 
             if o["user_id"] == user_id and o.get("status") == "pending"),
            None
        )
        
        if order_id:
            diamond_orders.pop(order_id, None)
            waiting_for_check.pop(user_id, None)
            
            try:
                safe_api(bot.send_message, user_id,
                        "❌ <b>BUYURTMANGIZ BEKOR QILINDI!</b>\n\n"
                        "Afsuski, to'lovingiz tasdiqlanmadi.\n"
                        "Sabablari:\n"
                        "• Chek aniq ko'rinmaydi\n"
                        "• To'lov summasi to'g'ri emas\n"
                        "• Boshqa texnik sabablar\n\n"
                        "💡 Agar xato bo'lsa, admin bilan bog'laning.")
            except Exception:
                pass
            
            username = get_username_id(user_id)
            safe_send_and_reply(message.chat.id, message.message_id,
                    f"❌ <b>{username} uchun buyurtma bekor qilindi!</b>\n"
                    f"🆔 ID: {user_id}")
        else:
            safe_send_and_reply(message.chat.id, message.message_id,
                    f"⚠️ <b>{get_username_id(user_id)} uchun kutilayotgan buyurtma topilmadi!</b>")

# ============================ O'YIN FUNKSIYALARI (DAYDI RO'LI BILAN) ============================
@bot.message_handler(commands=['startgame'])
def startgame_cmd(message):
    if message.chat.type not in ("group", "supergroup"):
        safe_send_and_reply(message.chat.id, message.message_id, "⚠️ Bu buyruq faqat guruhlar uchun!")
        return
    
    # Eski bot xabarlarini tozalash
    cleanup_old_bot_messages(message.chat.id, keep_last=3)
    
    chat_id = message.chat.id
    key = cid_str(chat_id)
    
    with LOCK:
        if key in games and games[key].get("state") == "started":
            safe_send_and_reply(message.chat.id, message.message_id, "😱 *O'YIN BOSHLANGAN!*\n\nYangi o'yin boshlash uchun avval buni tugating! ⏹️")
            return
        
        games[key] = {
            "state": "waiting",
            "players": [],
            "roles": {},
            "alive": [],
            "phase": None,
            "votes": {},
            "night_kill": None,
            "doctor_save": None,
            "join_msg_id": None,
            "vote_msg_id": None,
            "kill_count": {},
            "started_at": None,
            "current_night_msgs": [],
            "phase_start_time": None,
            "daydi_power_used": False,
            "chat_allowed": True,  # Chat ochiq yoki yopiq
            "bot_messages": [],  # Bot xabarlarini saqlash
        }
        persist_games()
    
    start_text = funny_game_start_message()
    
    join_kb = types.InlineKeyboardMarkup()
    join_kb.add(types.InlineKeyboardButton("🕹️ O'yinga qo'shilish", callback_data="join_game"))
    
    sent = safe_send_message(chat_id, start_text, reply_markup=join_kb)
    
    if sent:
        with LOCK:
            games[key]["join_msg_id"] = sent.message_id
            games[key]["bot_messages"].append(sent.message_id)
            persist_games()
    
    start_registration_timer(chat_id)

def start_registration_timer(chat_id: int):
    def timer_func():
        begin_game_by_chat(chat_id, auto=True)
    
    with LOCK:
        key = cid_str(chat_id)
        if key not in timers:
            timers[key] = {}
        
        if "registration" in timers[key]:
            timers[key]["registration"].cancel()
        
        timer = threading.Timer(REGISTRATION_TIMEOUT, timer_func)
        timer.start()
        timers[key]["registration"] = timer

def cancel_registration_timer(chat_id: int):
    with LOCK:
        key = cid_str(chat_id)
        if key in timers and "registration" in timers[key]:
            timers[key]["registration"].cancel()
            timers[key].pop("registration", None)

def start_phase_timer(chat_id: int, timeout: int, callback_func):
    def timer_func():
        callback_func(chat_id)
    
    with LOCK:
        key = cid_str(chat_id)
        if key not in timers:
            timers[key] = {}
        
        if "phase" in timers[key]:
            timers[key]["phase"].cancel()
        
        timer = threading.Timer(timeout, timer_func)
        timer.start()
        timers[key]["phase"] = timer

def cancel_phase_timer(chat_id: int):
    with LOCK:
        key = cid_str(chat_id)
        if key in timers and "phase" in timers[key]:
            timers[key]["phase"].cancel()
            timers[key].pop("phase", None)

def update_registration_message(chat_id: int) -> None:
    key = cid_str(chat_id)
    with LOCK:
        game = games.get(key)
        if not game:
            return
        players = list(game.get("players", []))
        msg_id = game.get("join_msg_id")
    
    if not players:
        text = "🎲 *RO'YXAT BOSHLANDI!*\n\nHali hech kim qo'shilmadi...\nBirinchi bo'ling! 🏃‍♂️"
    else:
        text = f"🎲 *RO'YXATDA {len(players)} TA O'YINCHI!*\n\n"
        text += "📋 Ro'yxat:\n"
        for i, uid in enumerate(players, 1):
            text += f"{i}. {get_username_id(uid)}\n"
        
        if len(players) < MIN_PLAYERS:
            text += f"\n⏳ Yana {MIN_PLAYERS - len(players)} kishi kerak!"
        else:
            text += "\n✅ O'yinni boshlash mumkin!\n/begin yoki 60 soniya kuting..."
    
    join_kb = types.InlineKeyboardMarkup()
    join_kb.add(types.InlineKeyboardButton("🕹️ O'yinga qo'shilish", callback_data="join_game"))
    
    try:
        if msg_id:
            safe_api(bot.edit_message_text, text, chat_id, msg_id, reply_markup=join_kb)
        else:
            sent = safe_send_message(chat_id, text, reply_markup=join_kb)
            if sent:
                with LOCK:
                    games[key]["join_msg_id"] = sent.message_id
                    games[key]["bot_messages"].append(sent.message_id)
                    persist_games()
    except Exception:
        logger.exception("update_registration_message failed")

# YANGI: Obuna tekshirish bilan o'yinga qo'shilish
@bot.callback_query_handler(func=lambda c: c.data == "join_game")
def join_game_callback(call):
    try:
        chat_id = call.message.chat.id
        uid = call.from_user.id
        key = cid_str(chat_id)
        
        with LOCK:
            if key not in games or games[key]["state"] != "waiting":
                safe_answer_callback(call, "❌ Ro'yxat yopilgan!")
                return
            
            game = games[key]
            if uid in game["players"]:
                safe_answer_callback(call, "✅ Siz allaqachon ro'yxatdasiz!")
                return
        
        # Obuna tekshirish
        if not check_user_subscribed(uid):
            # Obuna bo'lmagan bo'lsa, botga o'tkazish
            safe_answer_callback(call, "⚠️ Avval botga /start bosing!", show_alert=True)
            
            # Maxsus tugma yaratish
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("🤖 Botga o'tish", url=f"https://t.me/{BOT_USERNAME}?start=join_{chat_id}"))
            
            try:
                safe_api(bot.send_message, uid,
                        "🎮 *O'YINGA QO'SHILISH UCHUN* 🤖\n\n"
                        "Botga /start bosib ro'yxatdan o'ting!\n"
                        "Keyin quyidagi tugmani bosing va avtomatik qo'shiling.",
                        reply_markup=kb)
            except Exception:
                logger.exception(f"Foydalanuvchi {uid} ga xabar yuborishda xato")
            
            return
        
        # Obuna bo'lgan bo'lsa, qo'shilish
        with LOCK:
            game["players"].append(uid)
            game["kill_count"][uid_str(uid)] = 0
            ensure_profile(uid, get_username_obj(call.from_user))
            persist_games()
        
        join_text = funny_player_joined_message(get_username_obj(call.from_user))
        safe_answer_callback(call, "✅ Qo'shildingiz!")
        
        try:
            safe_api(bot.send_message, uid, f"✅ *Siz {call.message.chat.title} guruhidagi o'yinga qo'shildingiz!*\n\nRolingizni tez orada olasiz... 🎭")
        except Exception:
            pass
        
        update_registration_message(chat_id)
        
    except Exception as e:
        logger.exception("join_game_callback failed: %s", e)

# YANGI: /start bilan kelgan foydalanuvchini avtomatik qo'shish
@bot.message_handler(func=lambda m: m.text and m.text.startswith('/start join_'))
def handle_auto_join_after_start(msg):
    """Botga /start bosgandan keyin avtomatik o'yinga qo'shilish"""
    if msg.chat.type != "private":
        return
    
    uid = msg.from_user.id
    text = msg.text.strip()
    
    # Chat ID ni olish
    try:
        chat_id_str = text.split('join_')[1]
        chat_id = int(chat_id_str)
    except (IndexError, ValueError):
        return
    
    key = cid_str(chat_id)
    
    with LOCK:
        game = games.get(key)
        if not game or game.get("state") != "waiting":
            safe_api(bot.send_message, uid, "⚠️ O'yin ro'yxati allaqachon yopilgan yoki o'yin boshlangan!")
            return
        
        if uid in game["players"]:
            safe_api(bot.send_message, uid, "✅ Siz allaqachon ro'yxatdasiz!")
            return
        
        # Avtomatik qo'shilish
        game["players"].append(uid)
        game["kill_count"][uid_str(uid)] = 0
        ensure_profile(uid, get_username_obj(msg.from_user))
        persist_games()
    
    safe_api(bot.send_message, uid, 
            f"✅ *Siz avtomatik ravishda guruh o'yinga qo'shildingiz!*\n\n"
            f"🎮 Rolingizni tez orada olasiz...")
    
    # Guruhda xabar
    join_text = funny_player_joined_message(get_username_obj(msg.from_user))
    safe_send_message(chat_id, join_text)
    
    update_registration_message(chat_id)

def begin_game_by_chat(chat_id: int, auto: bool = False) -> None:
    key = cid_str(chat_id)
    with LOCK:
        game = games.get(key)
        if not game or game.get("state") != "waiting":
            if not auto:
                safe_send_message(chat_id, "⚠️ *RO'YXAT YO'Q!*\n\n/startgame yozib ro'yxatni boshlang!")
            return
        
        if len(game["players"]) < MIN_PLAYERS and not auto:
            safe_send_message(chat_id, 
                    f"❌ *O'YINCHILAR YETARLI EMAS!*\n\n{MIN_PLAYERS - len(game['players'])} kishi ko'proq kerak!")
            return

        cancel_registration_timer(chat_id)
        players = list(game["players"])
        
        num_special = min(3, len(players))
        roles_list = ["🤵🏻 Дон", "💉 Доктор", "🕵️ Комиссар"][:num_special]
        
        if len(players) >= 5:
            roles_list.append("👴 Daydi")
            num_special = min(4, len(players))
        
        guaranteed = [p for p in players if profiles.get(uid_str(p), {}).get("guaranteed_active_role")]
        random.shuffle(guaranteed)
        assigned: Dict[int, str] = {}
        available_special = roles_list.copy()
        
        for p in guaranteed:
            if not available_special:
                break
            assigned[p] = available_special.pop(0)
            profiles[uid_str(p)]["guaranteed_active_role"] = False
        
        remaining = [p for p in players if p not in assigned]
        random.shuffle(remaining)
        remaining_roles = available_special + ["👨🏼 Мирный житель"] * (len(remaining) - len(available_special))
        random.shuffle(remaining_roles)
        
        for p, r in zip(remaining, remaining_roles):
            assigned[p] = r
        
        game["roles"] = {uid_str(p): assigned[p] for p in assigned}
        game["state"] = "started"
        game["alive"] = players.copy()
        game["phase"] = "night_mafia"
        game["votes"] = {}
        game["night_kill"] = None
        game["doctor_save"] = None
        game["kill_count"] = {uid_str(p): 0 for p in players}
        game["started_at"] = int(time.time())
        game["phase_start_time"] = int(time.time())
        game["current_night_msgs"] = []
        game["daydi_power_used"] = False
        game["chat_allowed"] = True
        
        # Har bir o'yinchiga roli haqida xabar
        for p in players:
            role = assigned.get(p, "👨🏼 Мирный житель")
            try:
                role_text = funny_role_messages(role)
                safe_api(bot.send_message, p, role_text)
                ensure_profile(p, get_username_id(p))
                profiles[uid_str(p)]["doctor_save_used"] = False
            except Exception:
                logger.exception("failed send role to %s", p)
        
        persist_profiles()
        persist_games()
    
    # Guruhga start xabari
    players_list = "\n".join([f"{i}. {get_username_id(p)}" for i, p in enumerate(players, 1)])
    start_text = (
        "🎭 *O'YIN BOSHLANDI!*\n\n"
        "🤫 Har bir o'yinchi o'z rolini oldi!\n"
        "🌙 Birinchi tun boshlanmoqda...\n\n"
        "👥 O'yinchilar:\n" + players_list + "\n\n"
        "🎮 Omad! 🍀"
    )
    
    sent = safe_send_message(chat_id, start_text)
    if sent:
        with LOCK:
            games[key]["bot_messages"].append(sent.message_id)
            persist_games()
    
    send_mafia_vote(chat_id)

@bot.message_handler(commands=['begin'])
def begin_cmd(message):
    if message.chat.type not in ("group", "supergroup"):
        safe_send_and_reply(message.chat.id, message.message_id, "⚠️ Bu buyruq faqat guruhlar uchun!")
        return
    begin_game_by_chat(message.chat.id)

# ============================ O'YIN BOSQICHLARI (DAYDI BILAN) ============================
def send_mafia_vote(chat_id: int) -> None:
    key = cid_str(chat_id)
    
    night_text = funny_night_message()
    sent = safe_send_message(chat_id, night_text)
    if sent:
        with LOCK:
            if key in games:
                games[key]["bot_messages"].append(sent.message_id)
    
    with LOCK:
        game = games.get(key)
        if not game:
            return
        
        roles = game.get("roles", {})
        alive = list(game.get("alive", []))
        mafia = [int(uid) for uid, r in roles.items() if "Дон" in r and int(uid) in alive]
    
    if not mafia:
        with LOCK:
            games[key]["phase"] = "night_doctor"
            games[key]["phase_start_time"] = int(time.time())
            persist_games()
        send_doctor_save(chat_id)
        return
    
    # Har bir mafia uchun ovoz berish tugmalari
    for m in mafia:
        kb = types.InlineKeyboardMarkup(row_width=1)
        for t in alive:
            if t == m:
                continue
            kb.add(types.InlineKeyboardButton(f"🎯 {get_username_id(t)}", callback_data=f"mafia_kill:{t}"))
        
        mafia_text = (
            "😈 *MAFIA — TANLOV VAQTI!*\n\n"
            "💀 Kimni 'yo'q qilmoqchisiz?\n"
            "🤔 O'ylab ko'ring — qaroringiz muhim!"
        )
        
        safe_api(bot.send_message, m, mafia_text, reply_markup=kb)
    
    start_phase_timer(chat_id, NIGHT_TIMEOUT, night_timeout)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("mafia_kill:"))
def mafia_kill_callback(call):
    try:
        voter = call.from_user.id
        target = int(call.data.split(":", 1)[1])
        chat_id = call.message.chat.id
        key = cid_str(chat_id)
        
        with LOCK:
            game = games.get(key)
            if not game or game.get("state") != "started":
                safe_answer_callback(call, "⚠️ O'yin faol emas!")
                return
            
            if game.get("phase") != "night_mafia":
                safe_answer_callback(call, "⚠️ Mafia tanlov vaqti emas!")
                return
            
            roles = game.get("roles", {})
            voter_role = roles.get(uid_str(voter))
            if not voter_role or "Дон" not in voter_role:
                safe_answer_callback(call, "⚠️ Siz mafia emassiz!")
                return
            
            game["night_kill"] = target
            game["phase"] = "night_doctor"
            game["phase_start_time"] = int(time.time())
            persist_games()
        
        safe_answer_callback(call, f"✅ {get_username_id(target)} ni tanladingiz!")
        send_doctor_save(chat_id)
        
    except Exception as e:
        logger.exception("mafia_kill_callback failed: %s", e)

def send_doctor_save(chat_id: int) -> None:
    key = cid_str(chat_id)
    
    with LOCK:
        game = games.get(key)
        if not game:
            return
        
        roles = game.get("roles", {})
        alive = list(game.get("alive", []))
        doctors = [int(uid) for uid, r in roles.items() if "Доктор" in r and int(uid) in alive]
    
    if not doctors:
        with LOCK:
            games[key]["phase"] = "night_comissar"
            games[key]["phase_start_time"] = int(time.time())
            persist_games()
        send_comissar_check(chat_id)
        return
    
    # Har bir doktor uchun tugmalar
    for d in doctors:
        kb = types.InlineKeyboardMarkup(row_width=1)
        for t in alive:
            kb.add(types.InlineKeyboardButton(f"🩺 {get_username_id(t)}", callback_data=f"doctor_save:{t}"))
        
        doctor_text = (
            "💉 *DOKTOR — QUTQARISH VAQTI!*\n\n"
            "🛡 Kimni himoya qilmoqchisiz?\n"
            "💊 Bir kishini davolashingiz mumkin!"
        )
        
        safe_api(bot.send_message, d, doctor_text, reply_markup=kb)
    
    start_phase_timer(chat_id, NIGHT_TIMEOUT, night_timeout)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("doctor_save:"))
def doctor_save_callback(call):
    try:
        voter = call.from_user.id
        target = int(call.data.split(":", 1)[1])
        chat_id = call.message.chat.id
        key = cid_str(chat_id)
        
        with LOCK:
            game = games.get(key)
            if not game or game.get("state") != "started":
                safe_answer_callback(call, "⚠️ O'yin faol emas!")
                return
            
            if game.get("phase") != "night_doctor":
                safe_answer_callback(call, "⚠️ Doktor tanlov vaqti emas!")
                return
            
            roles = game.get("roles", {})
            voter_role = roles.get(uid_str(voter))
            if not voter_role or "Доктор" not in voter_role:
                safe_answer_callback(call, "⚠️ Siz doktor emassiz!")
                return
            
            prof = ensure_profile(voter)
            if prof["doctor_save_used"]:
                safe_answer_callback(call, "⚠️ Siz allaqachon qutqardingiz!")
                return
            
            game["doctor_save"] = target
            prof["doctor_save_used"] = True
            game["phase"] = "night_comissar"
            game["phase_start_time"] = int(time.time())
            persist_games()
            persist_profiles()
        
        safe_answer_callback(call, f"✅ {get_username_id(target)} ni qutqardingiz!")
        send_comissar_check(chat_id)
        
    except Exception as e:
        logger.exception("doctor_save_callback failed: %s", e)

def send_comissar_check(chat_id: int) -> None:
    key = cid_str(chat_id)
    
    with LOCK:
        game = games.get(key)
        if not game:
            return
        
        roles = game.get("roles", {})
        alive = list(game.get("alive", []))
        comissars = [int(uid) for uid, r in roles.items() if "Комиссар" in r and int(uid) in alive]
    
    if not comissars:
        with LOCK:
            games[key]["phase"] = "day"
            games[key]["phase_start_time"] = int(time.time())
            persist_games()
        start_day(chat_id)
        return
    
    # Har bir komissar uchun tugmalar
    for c in comissars:
        kb = types.InlineKeyboardMarkup(row_width=1)
        for t in alive:
            if t == c:
                continue
            kb.add(types.InlineKeyboardButton(f"🔍 {get_username_id(t)}", callback_data=f"comissar_check:{t}"))
        
        comissar_text = (
            "🕵️ *KOMISSAR — TEKSHIRISH VAQTI!*\n\n"
            "👀 Kimni tekshirmoqchisiz?\n"
            "🎭 Rolni aniqlang va haqiqatni oching!"
        )
        
        safe_api(bot.send_message, c, comissar_text, reply_markup=kb)
    
    start_phase_timer(chat_id, NIGHT_TIMEOUT, night_timeout)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("comissar_check:"))
def comissar_check_callback(call):
    try:
        voter = call.from_user.id
        target = int(call.data.split(":", 1)[1])
        chat_id = call.message.chat.id
        key = cid_str(chat_id)
        
        with LOCK:
            game = games.get(key)
            if not game or game.get("state") != "started":
                safe_answer_callback(call, "⚠️ O'yin faol emas!")
                return
            
            if game.get("phase") != "night_comissar":
                safe_answer_callback(call, "⚠️ Komissar tanlov vaqti emas!")
                return
            
            roles = game.get("roles", {})
            voter_role = roles.get(uid_str(voter))
            if not voter_role or "Комиссар" not in voter_role:
                safe_answer_callback(call, "⚠️ Siz komissar emassiz!")
                return
            
            target_role = roles.get(uid_str(target), "👨🏼 Мирный житель")
            game["phase"] = "day"
            game["phase_start_time"] = int(time.time())
            persist_games()
        
        safe_answer_callback(call, f"✅ {get_username_id(target)} ning roli: {target_role}")
        start_day(chat_id)
        
    except Exception as e:
        logger.exception("comissar_check_callback failed: %s", e)

def night_timeout(chat_id: int) -> None:
    key = cid_str(chat_id)
    with LOCK:
        game = games.get(key)
        if not game:
            return
        
        if game.get("phase") == "night_mafia" and game.get("night_kill") is None:
            alive = game.get("alive", [])
            if alive:
                mafia_players = [uid for uid in alive if "Дон" in game["roles"].get(uid_str(uid), "")]
                if mafia_players:
                    possible_targets = [uid for uid in alive if uid not in mafia_players]
                    if possible_targets:
                        game["night_kill"] = random.choice(possible_targets)
        
        game["phase"] = "day"
        game["phase_start_time"] = int(time.time())
        persist_games()
    
    start_day(chat_id)

def start_day(chat_id: int) -> None:
    key = cid_str(chat_id)
    with LOCK:
        game = games.get(key)
        if not game:
            return
        
        victim = game.get("night_kill")
        saved = game.get("doctor_save")
        prevented_by_protection = False
        
        if victim is not None:
            vic_prof = ensure_profile(victim, get_username_id(victim))
            if vic_prof.get("protection_active"):
                prevented_by_protection = True
                vic_prof["protection_active"] = False
                persist_profiles()
        
        if victim is not None and victim != saved and not prevented_by_protection:
            if victim in game["alive"]:
                game["alive"].remove(victim)
                day_text = f"☠️ *KECHASI O'LDI:* {get_username_id(victim)}\n\nKim qildi? Nima uchun? 🤔"
                sent = safe_send_message(chat_id, day_text)
                if sent:
                    game["bot_messages"].append(sent.message_id)
            else:
                day_text = funny_day_message()
                sent = safe_send_message(chat_id, day_text)
                if sent:
                    game["bot_messages"].append(sent.message_id)
        else:
            if prevented_by_protection:
                day_text = "☀️ *ERTALAB...*\n\n🎉 Hamma tirik!\n🛡 Kimdir himoya qildi!"
            else:
                day_text = funny_day_message()
            sent = safe_send_message(chat_id, day_text)
            if sent:
                game["bot_messages"].append(sent.message_id)
        
        game["night_kill"] = None
        game["doctor_save"] = None
        game["phase"] = "day"
        game["votes"] = {}
        game["phase_start_time"] = int(time.time())
        game["chat_allowed"] = True  # Kun davomida chat ochiq
        alive_now = list(game.get("alive", []))
        persist_games()
    
    # Tirik o'yinchilar ro'yxati
    alive_list = "\n".join([f"{i}. {get_username_id(p)}" for i, p in enumerate(alive_now, 1)]) or "—"
    day_info = (
        f"🏙️ *KUN BOSHLANDI!*\n\n"
        f"👥 Tiriklar ({len(alive_now)} ta):\n{alive_list}\n\n"
        f"🗣 Muhokama qiling! (Chat ochiq)\n⏳ Vaqt: {DAY_TIMEOUT} soniya"
    )
    
    sent = safe_send_message(chat_id, day_info)
    if sent:
        with LOCK:
            if key in games:
                games[key]["bot_messages"].append(sent.message_id)
                persist_games()
    
    send_day_vote_buttons(chat_id)
    start_phase_timer(chat_id, DAY_TIMEOUT, day_timeout)

def send_day_vote_buttons(chat_id: int) -> None:
    key = cid_str(chat_id)
    with LOCK:
        game = games.get(key)
        if not game:
            return
        alive = list(game.get("alive", []))
    
    if not alive:
        return
    
    kb = types.InlineKeyboardMarkup(row_width=2)
    for p in alive:
        kb.add(types.InlineKeyboardButton(f"🗳️ {get_username_id(p)}", callback_data=f"vote:{p}"))
    
    vote_text = (
        "⚖️ *OVOZ BERISH VAQTI!*\n\n"
        "Kim shubhali?\nKim haqiqatni yashirayapti?\n\n"
        "👇 Tanlang va boshlang!"
    )
    
    sent = safe_send_message(chat_id, vote_text, reply_markup=kb)
    if sent:
        with LOCK:
            games[key]["vote_msg_id"] = sent.message_id
            games[key]["bot_messages"].append(sent.message_id)
            persist_games()

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("vote:"))
def vote_handler(call):
    try:
        voter = call.from_user.id
        target = int(call.data.split(":", 1)[1])
        chat_id = call.message.chat.id
        key = cid_str(chat_id)
        
        with LOCK:
            game = games.get(key)
            if not game or game.get("state") != "started":
                safe_answer_callback(call, "⚠️ O'yin faol emas!")
                return
            
            if game.get("phase") != "day":
                safe_answer_callback(call, "⚠️ Ovoz berish vaqti emas!")
                return
            
            if voter not in game["alive"]:
                safe_answer_callback(call, "☠️ Siz o'liksiz! Ovoz bera olmaysiz!")
                return
            
            if target not in game.get("alive", []):
                safe_answer_callback(call, "⚠️ Bu o'yinchi o'lik!")
                return
            
            game["votes"][uid_str(voter)] = target
            persist_games()
        
        vote_text = funny_vote_message(get_username_id(voter), get_username_id(target))
        safe_answer_callback(call, "✅ Ovozingiz qabul qilindi!")
        sent = safe_send_message(chat_id, vote_text)
        if sent:
            with LOCK:
                if key in games:
                    games[key]["bot_messages"].append(sent.message_id)
                    persist_games()
        
    except Exception as e:
        logger.exception("vote_handler failed: %s", e)

def day_timeout(chat_id: int) -> None:
    key = cid_str(chat_id)
    with LOCK:
        game = games.get(key)
        if not game:
            return
        
        # Chatni yopish
        game["chat_allowed"] = False
        
        votes = game.get("votes", {})
        alive = list(game.get("alive", []))
        
        # Daydi ni aniqlash
        daydi_player = None
        daydi_vote = None
        roles = game.get("roles", {})
        for uid_str_voter, target in votes.items():
            voter = int(uid_str_voter)
            if roles.get(uid_str_voter) == "👴 Daydi":
                daydi_player = voter
                daydi_vote = target
                break
        
        # Daydi kuchi ishlatilmagan bo'lsa
        if daydi_player and not game.get("daydi_power_used", False):
            game["daydi_power_used"] = True
            
            # Daydi o'z ovozini bergan odamni tanlash
            if daydi_vote in alive:
                victim = daydi_vote
                
                if victim in alive:
                    alive.remove(victim)
                    game["alive"] = alive
                    
                    victim_role = game["roles"].get(uid_str(victim), "👨🏼 Мирный житель")
                    execution_text = f"👴 *DAYDI KUCHI ISHLATILDI!*\n\n{funny_execution_message(get_username_id(victim), victim_role)}"
                    sent = safe_send_message(chat_id, execution_text)
                    if sent:
                        game["bot_messages"].append(sent.message_id)
                    
                    # G'alaba tekshiruvi
                    check_victory(chat_id)
                    return
        
        # Agar Daydi kuchi ishlatilmagan yoki Daydi yo'q bo'lsa, oddiy ovoz hisoblash
        vote_counts = Counter(votes.values())
        if vote_counts:
            max_votes = max(vote_counts.values())
            candidates = [uid for uid, count in vote_counts.items() if count == max_votes]
            
            if len(candidates) == 1 and max_votes >= 1:
                victim = candidates[0]
                if victim in alive:
                    alive.remove(victim)
                    game["alive"] = alive
                    
                    victim_role = game["roles"].get(uid_str(victim), "👨🏼 Мирный житель")
                    execution_text = funny_execution_message(get_username_id(victim), victim_role)
                    sent = safe_send_message(chat_id, execution_text)
                    if sent:
                        game["bot_messages"].append(sent.message_id)
                    
                    check_victory(chat_id)
                    return
        
        sent = safe_send_message(chat_id, "🤝 *HECH KIM O'LMAYDI!*\n\nOvozlar teng bo'ldi yoki kam!")
        if sent:
            game["bot_messages"].append(sent.message_id)
        
        game["phase"] = "night_mafia"
        game["phase_start_time"] = int(time.time())
        game["votes"] = {}
        persist_games()
    
    send_mafia_vote(chat_id)

def check_victory(chat_id: int) -> None:
    key = cid_str(chat_id)
    with LOCK:
        game = games.get(key)
        if not game:
            return
        
        alive = list(game.get("alive", []))
        roles = game.get("roles", {})
        
        # Mafiya soni
        mafia_count = sum(1 for uid in alive if "Дон" in roles.get(uid_str(uid), ""))
        
        # Tinch fuqarolar soni
        civilian_count = len(alive) - mafia_count
        
        # G'alaba tekshiruvi
        if mafia_count == 0:
            for uid in alive:
                if "Дон" not in roles.get(uid_str(uid), ""):
                    prof = ensure_profile(uid)
                    prof["money"] += 20
                    prof["games_played"] = prof.get("games_played", 0) + 1
                    prof["wins"] = prof.get("wins", 0) + 1
            persist_profiles()
            send_final_stats_and_cleanup(chat_id, "Мирные жители")
        
        elif mafia_count >= civilian_count:
            for uid in alive:
                if "Дон" in roles.get(uid_str(uid), ""):
                    prof = ensure_profile(uid)
                    prof["money"] += 10
                    prof["games_played"] = prof.get("games_played", 0) + 1
                    prof["wins"] = prof.get("wins", 0) + 1
            persist_profiles()
            send_final_stats_and_cleanup(chat_id, "Мафия")
        
        else:
            game["phase"] = "night_mafia"
            game["phase_start_time"] = int(time.time())
            game["votes"] = {}
            persist_games()
            send_mafia_vote(chat_id)

def send_final_stats_and_cleanup(chat_id: int, winner: str) -> None:
    key = cid_str(chat_id)
    with LOCK:
        game = games.get(key)
        if not game:
            return
        
        players = list(game.get("players", []))
        roles = dict(game.get("roles", {}))
        alive_now = set(game.get("alive", []))
        started = game.get("started_at") or int(time.time())
        bot_messages = list(game.get("bot_messages", []))
    
    victory_text = funny_victory_message(winner)
    
    stats_text = "\n📊 *O'YIN STATISTIKASI:*\n\n"
    for p in players:
        role = roles.get(uid_str(p), "Noma'lum")
        status = "✅ TIRIK" if p in alive_now else "☠️ O'LIK"
        stats_text += f"• {get_username_id(p)} — {role} — {status}\n"
    
    took = int(time.time()) - started
    mins = took // 60
    secs = took % 60
    stats_text += f"\n⏱ Davomiylik: {mins} daqiqa {secs} soniya\n"
    stats_text += f"🏆 G'olib: {winner}\n\n"
    stats_text += "🎮 Keyingi o'yinga tayyormisiz? /startgame"
    
    sent = safe_send_message(chat_id, victory_text + stats_text)
    
    # 10 soniyadan so'ng bot xabarlarini o'chirish
    threading.Timer(10, lambda: cleanup_game_messages(chat_id, bot_messages)).start()
    
    # Tarixga qo'shish va o'yinni tozalash
    with LOCK:
        history.append({
            "chat_id": chat_id,
            "finished_at": int(time.time()),
            "winner": winner,
            "players": players,
            "roles": roles,
        })
        persist_history()
        games.pop(key, None)
        persist_games()
    
    cancel_phase_timer(chat_id)
    cancel_registration_timer(chat_id)

def cleanup_game_messages(chat_id: int, message_ids: List[int]) -> None:
    """O'yin tugagandan so'ng bot xabarlarini o'chirish"""
    for msg_id in message_ids:
        try:
            safe_api(bot.delete_message, chat_id, msg_id)
        except Exception:
            # Agar xabar allaqachon o'chirilgan bo'lsa, xato berishi mumkin
            pass
    
    # Global tarixdan ham o'chirish
    key = cid_str(chat_id)
    with LOCK:
        if key in bot_message_history:
            bot_message_history.pop(key, None)

# ============================ PROFIL VA DO'KON ============================
@bot.message_handler(commands=['profile'])
def cmd_profile(msg):
    if msg.chat.type != "private":
        safe_send_and_reply(msg.chat.id, msg.message_id, "👤 *Profil shaxsiy chatda!*\n\nMenga yozing: @True_mafia_kawai_bot")
        return
    
    uid = msg.from_user.id
    prof = ensure_profile(uid, get_username_obj(msg.from_user))
    
    status_emoji = "🛡️ AKTIV" if prof["protection_active"] else "❌ O'CHIRILGAN"
    role_guarantee = "✅ Kafolatlangan" if prof["guaranteed_active_role"] else "❌ Yo'q"
    
    profile_text = (
        f"👤 *{prof['name']} PROFILI*\n\n"
        f"💰 Pul: {prof['money']} so'm\n"
        f"💎 Olmoslar: {prof['diamonds']} ta\n"
        f"🛡 Himoya: {status_emoji}\n"
        f"🎭 Faqat aktiv rol: {role_guarantee}\n\n"
        f"📊 Statistika:\n"
        f"🎮 O'ynalgan o'yinlar: {prof.get('games_played', 0)}\n"
        f"🏆 G'alabalar: {prof.get('wins', 0)}\n"
        f"📈 G'alaba foizi: {round(prof.get('wins', 0) / max(prof.get('games_played', 1), 1) * 100, 1)}%\n\n"
        f"👇 Nima qilamiz?"
    )
    
    safe_api(bot.send_message, uid, profile_text, reply_markup=profile_reply_markup(uid=uid))

@bot.message_handler(func=lambda m: m.chat.type == "private" and m.text == "💠 Olmos ishlatish")
def use_diamond(msg):
    uid = msg.from_user.id
    prof = ensure_profile(uid, get_username_obj(msg.from_user))
    
    with LOCK:
        if prof["diamonds"] <= 0:
            safe_api(bot.send_message, uid, "😕 *OLMOSLARINGIZ YO'Q!*\n\nDo'konga boring va sotib oling! 🛒")
            return
        
        prof["diamonds"] -= 1
        prof["guaranteed_active_role"] = True
        persist_profiles()
    
    diamond_text = (
        "✨ *OLMOS ISHLATILDI!*\n\n"
        "🎭 Keyingi o'yinda sizga faol rol:\n"
        "🤵 Mafia, 💉 Doktor yoki 🕵️ Komissar!\n\n"
        "😎 Omad tilaymiz!"
    )
    
    safe_api(bot.send_message, uid, diamond_text, reply_markup=profile_reply_markup(uid=uid))

@bot.message_handler(func=lambda m: m.chat.type == "private" and m.text.startswith("🛡 Himoya sotib olish"))
def use_money_for_protection(msg):
    uid = msg.from_user.id
    prof = ensure_profile(uid, get_username_obj(msg.from_user))
    
    with LOCK:
        if prof["protection_active"]:
            safe_api(bot.send_message, uid, "🛡️ *SIZDA ALLAQACHON HIMOYA BOR!*\n\nBir marta ishlata olasiz!")
            return
        
        if prof["money"] < 100:
            safe_api(bot.send_message, uid, 
                    f"💰 *PUL YETARLI EMAS!*\n\n"
                    f"100 so'm kerak, sizda {prof['money']} so'm bor.\n"
                    f"O'yinda g'alaba qilib pul ishlang!")
            return
        
        prof["money"] -= 100
        prof["protection_active"] = True
        persist_profiles()
    
    protection_text = (
        "✅ *HIMOYA SOTIB OLINDI!*\n\n"
        "🛡 Endi siz bir kecha xavfsizsiz!\n"
        "😈 Mafia sizga tegolmaydi!\n\n"
        "⚠️ Faqat BIR MARTA ishlaydi!"
    )
    
    safe_api(bot.send_message, uid, protection_text, reply_markup=profile_reply_markup(uid=uid))

# ============================ BOSH MENYU TUGMALARI ============================
@bot.message_handler(func=lambda m: m.chat.type == "private" and m.text == "🎮 O'ynash")
def private_play(msg):
    play_text = (
        "🎮 *O'YIN BOSHLASH UCHUN:*\n\n"
        "1. Meni guruhga qo'shing\n"
        "2. Guruhda /startgame yozing\n"
        "3. Do'stlaringizni chaqiring!\n\n"
        "🎭 Ko'proq odam - ko'proq qiziqarli!"
    )
    
    add_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Guruhga qo'shish", url=add_url))
    
    safe_api(bot.send_message, msg.from_user.id, play_text, reply_markup=kb)

@bot.message_handler(func=lambda m: m.chat.type == "private" and m.text == "👤 Mening profilim")
def private_profile(msg):
    cmd_profile(msg)

@bot.message_handler(func=lambda m: m.chat.type == "private" and m.text == "💎 Olmoslar")
def diamonds_menu(msg):
    uid = msg.from_user.id
    prof = ensure_profile(uid, get_username_obj(msg.from_user))
    
    diamond_text = (
        "💎 *OLMOS DO'KONI*\n\n"
        "✨ Olmoslar bilan:\n"
        "• Faqat aktiv rol olasiz!\n"
        "• O'yinda ustunlik qilasiz!\n"
        "• Do'stlaringizni hayratda qoldirasiz! 😎\n\n"
        "👇 Paketni tanlang yoki o'zingiz kiritin:"
    )
    
    kb = types.InlineKeyboardMarkup(row_width=2)
    for cnt in DEFAULT_PACKS:
        price = cnt * PRICE_PER_DIAMOND
        kb.add(types.InlineKeyboardButton(f"{cnt} 💎 — {price} so'm", callback_data=f"buy_{cnt}"))
    kb.add(types.InlineKeyboardButton("✏️ O'zim kiritaman", callback_data="buy_custom"))
    kb.add(types.InlineKeyboardButton("🔙 Orqaga", callback_data="profile_back"))
    
    safe_api(bot.send_message, uid, diamond_text, reply_markup=kb)

@bot.message_handler(func=lambda m: m.chat.type == "private" and m.text == "ℹ️ Yordam")
def private_help_button(msg):
    help_text = (
        "❓ *TRUE MAFIA — YORDAM*\n\n"
        "🎮 *O'YIN QOIDALARI:*\n"
        "1. Mafia - tun bilan odam o'ldiradi\n"
        "2. Doktor - tun bilan odamni saqlaydi\n"
        "3. Komissar - tun bilan tekshiradi\n"
        "4. Tinch fuqarolar - mafiyani topadi\n"
        "5. Daydi - ovoz berishda maxsus kuch\n\n"
        "📋 *KOMANDALAR:*\n"
        "/start - Botni ishga tushirish\n"
        "/startgame - O'yin boshlash (guruhda)\n"
        "/begin - Ro'yxat to'lsa boshlash\n"
        "/endgame - O'yinni to'xtatish\n"
        "/profile - Shaxsiy profil\n\n"
        "💰 *IQTISODIYOT:*\n"
        "• G'alaba qilgan tinch fuqarolar: +20 so'm\n"
        "• G'alaba qilgan mafia: +10 so'm\n"
        "• Himoya: 100 so'm (bir marta)\n"
        "• Olmos: 3000 so'm (aktiv rol kafolati)\n\n"
        "🎭 Omad! 🍀"
    )
    
    safe_api(bot.send_message, msg.from_user.id, help_text)

@bot.message_handler(func=lambda m: m.chat.type == "private" and m.text == "🏠 Bosh menyu")
def back_to_main(msg):
    main_text = "🏠 *BOSH MENYU*\n\nKerakli bo'limni tanlang:"
    
    main_kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    main_kb.add("🎮 O'ynash", "👤 Mening profilim")
    main_kb.add("💎 Olmoslar", "ℹ️ Yordam")
    
    safe_api(bot.send_message, msg.from_user.id, main_text, reply_markup=main_kb)

# ============================ BOSHQA KOMANDALAR ============================
@bot.message_handler(commands=['endgame'])
def endgame_cmd(message):
    if message.chat.type not in ("group", "supergroup"):
        safe_send_and_reply(message.chat.id, message.message_id, "⚠️ Bu buyruq faqat guruhlar uchun!")
        return
    
    chat_id = message.chat.id
    key = cid_str(chat_id)
    
    with LOCK:
        game = games.get(key)
    
    if not game or game.get("state") != "started":
        safe_send_and_reply(message.chat.id, message.message_id, "⚠️ Faol o'yin yo'q!")
        return
    
    if message.from_user.id not in ADMIN_IDS:
        safe_send_and_reply(message.chat.id, message.message_id, "🚫 Faqat admin o'yinni to'xtata oladi!")
        return
    
    send_final_stats_and_cleanup(chat_id, "O'yin to'xtatildi")
    safe_send_and_reply(message.chat.id, message.message_id, "✅ O'yin to'xtatildi!")

# ============================ BOSHLANG'ICH YUKLASH ============================
def startup_restore() -> None:
    ensure_data_dir()
    changed = False
    with LOCK:
        for cid, g in list(games.items()):
            if g.get("state") == "started":
                try:
                    safe_send_message(int(cid), 
                            "⚠️ *BOT QAYTA ISHGA TUSHDI!*\n\nO'yin to'xtatildi. Yangi o'yin boshlang! 🔄")
                except Exception:
                    pass
                g["state"] = "waiting"
                g["phase"] = None
                g["phase_start_time"] = None
                changed = True
        if changed:
            persist_games()

startup_restore()

# ============================ WEBHOOK APPY ============================
from flask import Flask, request

app = Flask(__name__)

@app.route('/')
def home():
    return """
    <h1>🎭 True Mafia Bot</h1>
    <p>O'zbekcha mafia o'yin boti</p>
    <p>Bot faol!</p>
    <p>Bog'lanish: @True_mafia_kawai_bot</p>
    """

# Webhook endpoint
@app.route('/' + TOKEN, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    return ''

# Webhookni o'rnatish
@app.route('/setwebhook')
def set_webhook():
    webhook_url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME', 'localhost:10000')}/{TOKEN}"
    bot.remove_webhook()
    bot.set_webhook(url=webhook_url)
    return f"✅ Webhook o'rnatildi!<br>URL: {webhook_url}"

# ============================ ISHGA TUSHIRISH ============================
if __name__ == "__main__":
    print("🎭 True Mafia Bot ishga tushdi...")
    print("🤖 O'zbekcha versiya")
    print("👑 Admin panel: /admin")
    print("🎮 O'yin: /startgame (guruhda)")
    print(f"💰 Olmos narxi: {PRICE_PER_DIAMOND} so'm")
    print("👴 Yangi rol: Daydi - ovoz berishda maxsus kuch")
    print("🧹 Faqat bot xabarlarini tozalash faol")
    print("🤖 Obuna tekshirish faol")
    
    if TOKEN == "REPLACE_ME" or not BOT_USERNAME:
        logger.error("⚠️ MAFIA_BOT_TOKEN va MAFIA_BOT_USERNAME o'rnating!")
    
    # Render muhitini tekshirish
    import os
    if os.environ.get('RENDER'):
        print("🌐 Webhook rejimida ishlaydi (Render server)")
        # Flask serverini ishga tushirish
        port = int(os.environ.get('PORT', 10000))
        app.run(host='0.0.0.0', port=port, debug=False)
    else:
        print("🔄 Polling rejimida ishlaydi (lokal)")
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except KeyboardInterrupt:
            print("🛑 Foydalanuvchi tomonidan to'xtatildi")
        except Exception as e:
            logger.exception("❌ Xato: %s", e)