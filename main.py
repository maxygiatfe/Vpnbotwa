import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

logging.basicConfig(level=logging.INFO)

# ───────────────────────── НАСТРОЙКИ (через переменные окружения) ─────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BRAND = os.getenv("BRAND_NAME", "Quack VPN")
TAGLINE = os.getenv("TAGLINE", "Ныряй в интернет без забот")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/telegram")
TERMS_URL = os.getenv("TERMS_URL", "https://telegra.ph/")
PRIVACY_URL = os.getenv("PRIVACY_URL", "https://telegra.ph/")
SUB_BASE = os.getenv("SUB_BASE", "https://sub.example.com")  # база для фейковой ссылки подписки
DB_PATH = os.getenv("DB_PATH", "bot.db")  # на Railway лучше "/data/bot.db" + Volume
PORT = int(os.getenv("PORT", "8080"))
DEBUG = os.getenv("DEBUG", "1") == "1"  # показывает тестовые кнопки (добавить устройство)

_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL") or (f"https://{_domain}/app" if _domain else "")

BASE_DIR = Path(__file__).parent
BANNERS_DIR = BASE_DIR / "banners"  # положи сюда start.jpg, sub.jpg, pay.jpg, devices.jpg, ref.jpg, info.jpg

TRIAL_DAYS = 3
MAX_DEVICES = 5
REF_PERCENT = 50

PLANS = {
    "1": {"title": "1 Месяц", "days": 30, "price": 199, "icon": "🙎"},
    "3": {"title": "3 Месяца (Выгодно)", "days": 90, "price": 499, "icon": "💎"},
    "6": {"title": "6 Месяцев", "days": 180, "price": 899, "icon": "⭐"},
    "12": {"title": "12 Месяцев", "days": 365, "price": 1699, "icon": "♾"},
}

# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0,
    sub_until INTEGER DEFAULT 0, sub_token TEXT, ref_by INTEGER, created INTEGER);
CREATE TABLE IF NOT EXISTS payments(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan TEXT, amount INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS devices(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT, ts INTEGER);
"""
)
db.commit()


def get_user(uid: int, username: str | None = None, ref_by: int | None = None) -> sqlite3.Row:
    row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if row is None:
        if ref_by == uid:
            ref_by = None
        if ref_by and not db.execute("SELECT 1 FROM users WHERE id=?", (ref_by,)).fetchone():
            ref_by = None
        now = int(time.time())
        db.execute(
            "INSERT INTO users(id, username, sub_until, sub_token, ref_by, created) VALUES(?,?,?,?,?,?)",
            (uid, username, now + TRIAL_DAYS * 86400, secrets.token_urlsafe(12), ref_by, now),
        )
        db.commit()
    elif username != row["username"]:
        db.execute("UPDATE users SET username=? WHERE id=?", (username, uid))
        db.commit()
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def days_left(user) -> int:
    left = user["sub_until"] - int(time.time())
    return max(0, math.ceil(left / 86400)) if left > 0 else 0


def devices_of(uid: int):
    return db.execute("SELECT * FROM devices WHERE user_id=? ORDER BY id", (uid,)).fetchall()


def sub_link(user) -> str:
    return f"{SUB_BASE}/{user['sub_token']}"


def activate(uid: int, plan_key: str):
    plan = PLANS[plan_key]
    user = get_user(uid)
    now = int(time.time())
    start = max(now, user["sub_until"])
    db.execute("UPDATE users SET sub_until=? WHERE id=?", (start + plan["days"] * 86400, uid))
    db.execute(
        "INSERT INTO payments(user_id, plan, amount, ts) VALUES(?,?,?,?)",
        (uid, plan_key, plan["price"], now),
    )
    # реферальный бонус: 50% от срока купленной подписки
    first_purchase = db.execute("SELECT COUNT(*) c FROM payments WHERE user_id=?", (uid,)).fetchone()["c"] == 1
    ref = user["ref_by"]
    bonus = 0
    if ref and first_purchase:
        bonus = plan["days"] * REF_PERCENT // 100
        r = get_user(ref)
        base = max(now, r["sub_until"])
        db.execute("UPDATE users SET sub_until=? WHERE id=?", (base + bonus * 86400, ref))
    db.commit()
    return ref, bonus


# ───────────────────────── ТЕКСТЫ И КЛАВИАТУРЫ ─────────────────────────
def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text, cb=None, url=None, webapp=None, style=None) -> InlineKeyboardButton:
    kwargs = {"text": text}
    if cb:
        kwargs["callback_data"] = cb
    if url:
        kwargs["url"] = url
    if webapp:
        kwargs["web_app"] = WebAppInfo(url=webapp)
    if style:  # "success" = зелёная, "primary" = синяя, "danger" = красная (Bot API 9.4+)
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def app_btn(text: str, style=None) -> InlineKeyboardButton:
    if WEBAPP_URL:
        return btn(text, webapp=WEBAPP_URL, style=style)
    return btn(text, cb="noapp", style=style)


BACK = [btn("‹ Назад", cb="menu")]


def main_kb():
    return kb(
        [
            [app_btn("Открыть приложение", style="success")],
            [btn("🔒 Моя подписка", cb="sub", style="success")],
            [btn("👥 Реф система", cb="ref"), btn("ℹ️ Информация", cb="info")],
            [btn("❓ Поддержка", url=SUPPORT_URL)],
        ]
    )


def main_text():
    return f"<b>{BRAND} - {TAGLINE}</b>"


def sub_text(user, uname):
    d = days_left(user)
    n = len(devices_of(user["id"]))
    status = "✅ Активна" if d > 0 else "❌ Истекла"
    t = (
        f"🔒 <b>Подписка {uname}</b>\n\n"
        f"❕ <b>Статус:</b> {status}\n"
        f"🕓 <b>Срок подписки: {d} дн.</b>\n\n"
        f"📱 <b>Устройства: {n} / {MAX_DEVICES}</b>\n\n"
        f"🔗 <b>Ссылка на подписку:</b>\n<code>{sub_link(user)}</code>"
    )
    if d == 0:
        t += "\n\n❕ <b>Подписка закончилась! Продлите, чтобы вернуть доступ.</b>"
    elif d <= 3:
        t += "\n\n❕ <b>Подписка скоро истечёт! Продлите, чтобы не потерять доступ.</b>"
    return t


def sub_kb():
    return kb(
        [
            [app_btn("🔗 Подключиться", style="success")],
            [btn("⌛ Продлить подписку", cb="pay", style="success")],
            [btn("📟 Мои устройства", cb="devices")],
            [btn("⭐ История платежей", cb="history")],
            BACK,
        ]
    )


def pay_kb():
    rows = [
        [btn(f"{p['icon']} {p['title']} — {p['price']}₽", cb=f"plan:{k}", style="success" if k == "3" else None)]
        for k, p in PLANS.items()
    ]
    rows.append([btn("‹ Назад", cb="sub")])
    return kb(rows)


def devices_text(user):
    n = len(devices_of(user["id"]))
    return (
        "📟 <b>Управление устройствами</b>\n\n"
        f"ℹ️ <b>Подключено устройств: {n} / {MAX_DEVICES}</b>\n\n"
        "✨ Устройства появятся автоматически после подключения через ссылку в разделе «Подписка»."
    )


def devices_kb(user):
    rows = []
    for d in devices_of(user["id"]):
        rows.append([btn(f"🗑 {d['name']}", cb=f"devdel:{d['id']}")])
    if DEBUG:
        rows.append([btn("➕ [тест] Добавить устройство", cb="devadd")])
    rows.append([btn("‹ Назад", cb="sub")])
    return kb(rows)


def ref_text(user, bot_username):
    cnt = db.execute("SELECT COUNT(*) c FROM users WHERE ref_by=?", (user["id"],)).fetchone()["c"]
    return (
        "👥 <b>Реферальная программа</b>\n\n"
        "🔗 <b>Ваша ссылка для друзей:</b>\n"
        f"https://t.me/{bot_username}?start=ref_{user['id']}\n\n"
        f"<blockquote>Когда ваш друг купит подписку, вы получите {REF_PERCENT}% от его срока!\n"
        "Например: друг купил 30 дней — вы получите +15 дней</blockquote>\n\n"
        f"<b>Вы пригласили друзей: {cnt}</b>"
    )


def info_text():
    return (
        "Мы используем быстрые протоколы с открытым исходным кодом. Серверы с портами 10 Гбит/с.\n\n"
        "Не ведём журналы и не храним историю посещений — в отличие от многих бесплатных сервисов. "
        "Никакие данные о вас не собираем и не передаём.\n\n"
        f"Подписка по умолчанию на {MAX_DEVICES} устройств (можно докупить). "
        "Поддерживаем Android, Windows, iOS, Linux и macOS.\n\n"
        f'<a href="{TERMS_URL}">Пользовательское соглашение</a> и <a href="{PRIVACY_URL}">Политика конфиденциальности</a>.'
    )


def info_kb():
    return kb([[btn("🛟 Техническая поддержка", url=SUPPORT_URL)], BACK])


# ───────────────────────── ОТПРАВКА ЭКРАНОВ (с баннерами) ─────────────────────────
file_ids: dict[str, str] = {}


def find_banner(key: str):
    for ext in ("jpg", "jpeg", "png"):
        p = BANNERS_DIR / f"{key}.{ext}"
        if p.exists():
            return p
    return None


async def show(event, key: str, text: str, markup: InlineKeyboardMarkup):
    """Показывает экран. Если есть banners/<key>.jpg — с картинкой, иначе просто текст."""
    is_cb = isinstance(event, CallbackQuery)
    msg = event.message if is_cb else event
    banner = find_banner(key)
    try:
        if banner:
            media = file_ids.get(key) or FSInputFile(banner)
            if is_cb and msg.photo:
                res = await msg.edit_media(InputMediaPhoto(media=media, caption=text), reply_markup=markup)
            else:
                if is_cb:
                    try:
                        await msg.delete()
                    except TelegramBadRequest:
                        pass
                res = await msg.answer_photo(media, caption=text, reply_markup=markup)
            if isinstance(res, Message) and res.photo:
                file_ids[key] = res.photo[-1].file_id
        else:
            if is_cb:
                if msg.photo:
                    await msg.edit_caption(caption=text, reply_markup=markup)
                else:
                    await msg.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
            else:
                await msg.answer(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


# ───────────────────────── ХЕНДЛЕРЫ БОТА ─────────────────────────
dp = Dispatcher()


def uname(u) -> str:
    return f"@{u.username}" if u.username else u.full_name


@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    ref = None
    if command.args and command.args.startswith("ref_"):
        try:
            ref = int(command.args[4:])
        except ValueError:
            pass
    get_user(message.from_user.id, message.from_user.username, ref)
    await show(message, "start", main_text(), main_kb())


@dp.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery):
    await show(call, "start", main_text(), main_kb())
    await call.answer()


@dp.callback_query(F.data == "noapp")
async def cb_noapp(call: CallbackQuery):
    await call.answer("Mini App ещё не подключено: задай WEBAPP_URL (см. README)", show_alert=True)


@dp.callback_query(F.data == "sub")
async def cb_sub(call: CallbackQuery):
    u = get_user(call.from_user.id, call.from_user.username)
    await show(call, "sub", sub_text(u, uname(call.from_user)), sub_kb())
    await call.answer()


@dp.callback_query(F.data == "pay")
async def cb_pay(call: CallbackQuery):
    await show(call, "pay", "🏪 <b>Выберите период подписки</b>", pay_kb())
    await call.answer()


@dp.callback_query(F.data.startswith("plan:"))
async def cb_plan(call: CallbackQuery):
    key = call.data.split(":")[1]
    p = PLANS[key]
    text = (
        f"🧾 <b>Заказ: {p['title']}</b>\n"
        f"Срок: {p['days']} дн.\nСумма: <b>{p['price']}₽</b>\n\n"
        "⚠️ Это заглушка: реальная оплата не подключена."
    )
    await show(
        call,
        "pay",
        text,
        kb([[btn("✅ Оплатить (тест)", cb=f"buy:{key}", style="success")], [btn("‹ Назад", cb="pay")]]),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(call: CallbackQuery):
    key = call.data.split(":")[1]
    get_user(call.from_user.id, call.from_user.username)
    ref, bonus = activate(call.from_user.id, key)
    if ref and bonus:
        try:
            await call.bot.send_message(ref, f"🎉 Ваш друг купил подписку — вам начислено +{bonus} дн.!")
        except Exception:
            pass
    u = get_user(call.from_user.id)
    await show(call, "sub", sub_text(u, uname(call.from_user)), sub_kb())
    await call.answer("Подписка продлена ✅")


@dp.callback_query(F.data == "history")
async def cb_history(call: CallbackQuery):
    rows = db.execute(
        "SELECT * FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 10", (call.from_user.id,)
    ).fetchall()
    if rows:
        lines = [
            f"• {time.strftime('%d.%m.%Y', time.localtime(r['ts']))} — {PLANS[r['plan']]['title']} — {r['amount']}₽"
            for r in rows
        ]
        text = "⭐ <b>История платежей</b>\n\n" + "\n".join(lines)
    else:
        text = "⭐ <b>История платежей</b>\n\nПлатежей пока нет."
    await show(call, "pay", text, kb([[btn("‹ Назад", cb="sub")]]))
    await call.answer()


@dp.callback_query(F.data == "devices")
async def cb_devices(call: CallbackQuery):
    u = get_user(call.from_user.id, call.from_user.username)
    await show(call, "devices", devices_text(u), devices_kb(u))
    await call.answer()


@dp.callback_query(F.data == "devadd")
async def cb_devadd(call: CallbackQuery):
    u = get_user(call.from_user.id)
    n = len(devices_of(u["id"]))
    if n >= MAX_DEVICES:
        await call.answer("Лимит устройств достигнут", show_alert=True)
        return
    db.execute(
        "INSERT INTO devices(user_id, name, ts) VALUES(?,?,?)",
        (u["id"], f"Устройство {n + 1} ({secrets.token_hex(2)})", int(time.time())),
    )
    db.commit()
    await show(call, "devices", devices_text(u), devices_kb(u))
    await call.answer()


@dp.callback_query(F.data.startswith("devdel:"))
async def cb_devdel(call: CallbackQuery):
    did = int(call.data.split(":")[1])
    db.execute("DELETE FROM devices WHERE id=? AND user_id=?", (did, call.from_user.id))
    db.commit()
    u = get_user(call.from_user.id)
    await show(call, "devices", devices_text(u), devices_kb(u))
    await call.answer("Устройство удалено")


@dp.callback_query(F.data == "ref")
async def cb_ref(call: CallbackQuery):
    u = get_user(call.from_user.id, call.from_user.username)
    me = await call.bot.get_me()
    await show(call, "ref", ref_text(u, me.username), kb([BACK]))
    await call.answer()


@dp.callback_query(F.data == "info")
async def cb_info(call: CallbackQuery):
    await show(call, "info", info_text(), info_kb())
    await call.answer()


# ───────────────────────── MINI APP (заглушка) ─────────────────────────
def check_init_data(init_data: str):
    """Проверка подписи Telegram WebApp initData. Возвращает dict пользователя или None."""
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        got_hash = data.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got_hash):
            return None
        return json.loads(data["user"])
    except Exception:
        return None


async def web_index(request):
    return web.Response(text=f"{BRAND} bot is running")


async def web_app(request):
    return web.FileResponse(BASE_DIR / "webapp.html")


async def web_me(request):
    tg_user = check_init_data(request.headers.get("X-Init-Data", ""))
    if not tg_user:
        return web.json_response({"error": "unauthorized"}, status=401)
    u = get_user(tg_user["id"], tg_user.get("username"))
    d = days_left(u)
    return web.json_response(
        {
            "brand": BRAND,
            "active": d > 0,
            "days": d,
            "until": time.strftime("%d.%m.%Y", time.localtime(u["sub_until"])),
            "link": sub_link(u),
            "devices": len(devices_of(u["id"])),
            "max_devices": MAX_DEVICES,
            "balance": u["balance"],
        }
    )


async def start_web():
    app = web.Application()
    app.router.add_get("/", web_index)
    app.router.add_get("/app", web_app)
    app.router.add_get("/api/me", web_me)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Web server on :%s, Mini App URL: %s", PORT, WEBAPP_URL or "(не задан)")


# ───────────────────────── ЗАПУСК ─────────────────────────
async def main():
    if not BOT_TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=True)
    if WEBAPP_URL:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="VPN App", web_app=WebAppInfo(url=WEBAPP_URL)))
    await start_web()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
