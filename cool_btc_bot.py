from __future__ import annotations

"""
cool_btc_bot.py — BTC-бот с коллективным интервалом оповещений
──────────────────────────────────────────────────────────────
• Все, кто нажал «Subscribe», получают уведомления, когда цена BTC смещается
  минимум на N $, где N задаётся самими пользователями через кнопку
  «Interval ⚙️» (можно выбрать 50/100/200/500 или ввести своё число).
• Подписчики и «последняя отправленная цена» хранятся в btc_bot_state.json.
• Глобальный интервал хранится в btc_collab.json (ключ "interval").
• Русскоязычные логи выводятся в консоль и файл bot.log.
• Требует python-telegram-bot ≥ 21 и requests.

Изменения:
  1. Добавлен helper fmt_num() / fprice() для удобочитаемого форматирования
     чисел с разделителем тысяч (пробел).
  2. Все сообщения с суммами теперь используют эти функции.
  3. Исправлено двойное уведомление о смене интервала: теперь
     участник-инициатор получает подтверждение через edit_text(),
     а остальным подписчикам уходит отдельное сообщение.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ──────────────── Файлы и константы ────────────────
STATE_FILE  = Path("btc_bot_state.json")      # {uid: last_price}
COLLAB_FILE = Path("btc_collab.json")         # {"interval": N}
LOG_FILE    = Path("bot.log")
POLL_SEC    = 5                                # опрос Binance, сек
DEFAULT_INT = 200                              # шаг по умолчанию
CHOICES     = [50, 100, 200, 500]              # быстрый выбор

# ──────────────── Токен бота ────────────────
try:
    import token_1 as _t  # type: ignore
    TOKEN = getattr(_t, "TOKEN", None)
except ModuleNotFoundError:
    TOKEN = None
TOKEN = TOKEN or os.getenv("TELEGRAM_BOT_TOKEN") or "PUT_YOUR_TOKEN_HERE"
if TOKEN.startswith("PUT_"):
    sys.exit("⚠️  Укажите TELEGRAM_BOT_TOKEN или token_1.py")

# ──────────────── Логирование ────────────────
FMT = "%(_asctime)s | %(levelname)-8s | %(message)s".replace("_", "")
root = logging.getLogger(); root.setLevel(logging.INFO)
sh = logging.StreamHandler(sys.stdout); sh.setFormatter(logging.Formatter(FMT)); root.addHandler(sh)
fh = logging.FileHandler(LOG_FILE, encoding="utf-8"); fh.setFormatter(logging.Formatter(FMT)); root.addHandler(fh)
for name in ("telegram", "httpx", "apscheduler"):
    logging.getLogger(name).setLevel(logging.WARNING)
log = logging.getLogger("bot")

# ──────────────── Глобальное состояние ────────────────
_subs: Dict[int, float] = {}   # uid → last_notified_price
_interval: int = DEFAULT_INT

# ──────────────── Форматирование чисел ────────────────

def fmt_num(n: float) -> str:
    """Возвращает число с пробелом-разделителем тысяч и 2 знаками после запятой."""
    return f"{n:,.2f}".replace(",", " ")

def fprice(n: float) -> str:
    """Удобочитаемая цена с $ на конце."""
    return f"{fmt_num(n)}$"

# ──────────────── Helpers JSON ────────────────

def read_json(path: Path, default: Any):
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except Exception as e:
        log.error("Не смог прочитать %s: %s", path, e); return default

def write_json(path: Path, data: Any):
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        tmp.replace(path)
    except Exception as e:
        log.error("Не смог записать %s: %s", path, e)

# ──────────────── HTTP session with retry ────────────────
_session: requests.Session | None = None

def get_http_session() -> requests.Session:
    """Создаёт/возвращает requests.Session с keep-alive и автоматическим Retry."""
    global _session
    if _session is None:
        _session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        _session.mount("https://", HTTPAdapter(max_retries=retry))
        _session.headers.update({"User-Agent": "CoolBTCBot/1.0"})
    return _session

# ──────────────── Binance API ────────────────
FAIL_LIMIT = 5; _fail_seq = 0

def get_price() -> float | None:
    global _fail_seq
    session = get_http_session()
    try:
        r = session.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=5,
        )
        r.raise_for_status()
        p = float(r.json()["price"])
        _fail_seq = 0
        log.info("Цена BTC: %.2f$", p)
        return p
    except (ValueError, KeyError) as e:
        _fail_seq += 1
        log.error("Неверный JSON Binance (%d): %s", _fail_seq, e)
    except requests.RequestException as e:
        _fail_seq += 1
        log.warning("HTTP-ошибка Binance (%d): %s", _fail_seq, e)
    if _fail_seq >= FAIL_LIMIT:
        log.error("Достигнут предел %d ошибок Binance API", FAIL_LIMIT)
    return None

# ──────────────── Telegram UI ────────────────
WELCOME = (
    "👋 <b>BTC-бот</b>\n\n"
    "• <b>Subscribe</b> — получать алерты.\n"
    "• <b>Interval ⚙️</b> — задать общий шаг.\n\n"
    "Текущий интервал: <b>{}</b> $."
)

def main_kbd(subscribed: bool) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if subscribed:
        rows.append([
            InlineKeyboardButton("✅ Подписка", callback_data="noop"),
            InlineKeyboardButton("Отписаться", callback_data="unsub"),
        ])
    else:
        rows.append([InlineKeyboardButton("Subscribe", callback_data="sub")])
    rows.append([InlineKeyboardButton("Interval ⚙️", callback_data="interval")])
    rows.append([InlineKeyboardButton("Price", callback_data="price")])
    return InlineKeyboardMarkup(rows)

# ──────────────── Команды ────────────────
async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_chat.id
    await u.message.reply_html(WELCOME.format(_interval), reply_markup=main_kbd(uid in _subs))

async def cmd_price(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = get_price()
    await u.message.reply_text(f"BTC = {fprice(p)}" if p else "⛔ Цена недоступна.")

# ──────────────── Inline кнопки ────────────────
async def cb_btn(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query; await q.answer()
    uid, data = q.message.chat_id, q.data
    global _interval

    # ——— Быстрый просмотр цены ———
    if data == "price":
        p = get_price()
        return await q.message.reply_text(
            f"BTC = {fprice(p)}" if p else "⛔ Цена недоступна."
        )

    # ——— Подписка / Отписка ———
    if data == "sub":
        p = get_price()
        if p is None:
            return await q.message.reply_text("⛔ Цена недоступна. Попробуйте позже.")
        _subs[uid] = p; write_json(STATE_FILE, _subs)
        log.info("UID %d подписался (%.2f$)", uid, p)
        return await q.message.edit_text("✅ Подписка оформлена.", reply_markup=main_kbd(True))

    if data == "unsub":
        if _subs.pop(uid, None):
            write_json(STATE_FILE, _subs); log.info("UID %d отписался", uid)
        return await q.message.edit_text("❎ Подписка отменена.", reply_markup=main_kbd(False))

    # ——— Настройка интервала ———
    if data == "interval":
        rows = [[InlineKeyboardButton(str(n), callback_data=f"step_{n}") for n in CHOICES],
                [InlineKeyboardButton("Ввести…", callback_data="step_custom")]]
        return await q.message.reply_text(
            f"Текущий интервал: {_interval}$", reply_markup=InlineKeyboardMarkup(rows))

    if data.startswith("step_"):
        if data == "step_custom":
            ctx.user_data["await_int"] = True
            return await q.message.reply_text("Введите новый интервал (целое число $):")
        val = int(data.split("_", 1)[1])
        txt = await change_interval(val, uid, ctx.bot, notify_changer=False)
        return await q.message.edit_text(txt, reply_markup=main_kbd(uid in _subs))

# ──────────────── Текстовые сообщения ────────────────
async def msg_text(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.pop("await_int", False):
        return
    try:
        val = int(u.message.text)
        if val <= 0:
            raise ValueError
    except ValueError:
        return await u.message.reply_text("❌ Нужно положительное целое число.")
    txt = await change_interval(val, u.effective_chat.id, ctx.bot, notify_changer=False)
    await u.message.reply_text(txt)

# ──────────────── Изменение интервала ────────────────
async def change_interval(val: int, by_uid: int, bot, notify_changer: bool = False) -> str:
    """Меняет интервал и оповещает подписчиков.

    notify_changer=False — не слать broadcast инициатору, он получит edit_text/reply.
    """
    global _interval
    old = _interval; _interval = val; write_json(COLLAB_FILE, {"interval": _interval})
    log.info("Интервал %d → %d (uid=%d)", old, val, by_uid)
    txt = f"⚙️ Интервал изменён: {old} → {val} $"
    for uid in list(_subs):
        if uid == by_uid and not notify_changer:
            continue
        try:
            await bot.send_message(uid, txt)
        except TelegramError:
            log.warning("Не смог оповестить %d", uid)
    return txt

# ──────────────── Watcher ────────────────
async def watcher(app: Application):
    """Фоновый цикл: опрашивает цену и рассылает алерты."""
    log.info("Watcher запущен (%d с)", POLL_SEC)
    while True:
        try:
            p = get_price()
            if p is not None:
                step = _interval
                for uid, last in list(_subs.items()):
                    if abs(p - last) >= step:
                        diff = p - last
                        sym = "🚀↑" if diff > 0 else "🔻↓"
                        diff_fmt = f"{diff:+,.2f}".replace(",", " ")
                        txt = f"{sym} BTC {fprice(p)}  (Δ {diff_fmt}$, шаг {step}$)"
                        try:
                            await app.bot.send_message(uid, txt)
                            _subs[uid] = p
                        except TelegramError as te:
                            log.error("Ошибка отправки %d: %s", uid, te)
                write_json(STATE_FILE, _subs)
        except Exception as e:
            log.exception("Необработанная ошибка в watcher: %s", e)
        await asyncio.sleep(POLL_SEC)

# ──────────────── Инициализация ────────────────
async def _post_init(app: Application):
    app.create_task(watcher(app))

def build_app() -> Application:
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CallbackQueryHandler(cb_btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_text))
    return app

# ──────────────── Main ────────────────

def main():
    global _subs, _interval
    _subs = read_json(STATE_FILE, {})
    _interval = read_json(COLLAB_FILE, {"interval": DEFAULT_INT}).get("interval", DEFAULT_INT)

    app = build_app()
    log.info("Старт бота. Подписчиков: %d | шаг: %d$", len(_subs), _interval)
    try:
        app.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — остановка…")
    finally:
        if '_session' in globals() and _session is not None:
            _session.close()
        log.info("Выключено в %s", datetime.now().strftime("%d.%m.%Y %H:%M:%S"))

if __name__ == "__main__":
    main()
