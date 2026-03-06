#!/usr/bin/env python3
"""Forex Signal Bot — v4"""

import asyncio
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

BOT_TOKEN       = "8612612451:AAE7dMyGwf1Ddigz23Ygeop5ubh1nkrm6M8"
TWELVE_DATA_KEY = "55dae6924d864941b1ab27052b0871ef"
ALLOWED_USERS   = {544863362}  # @chief_man_33

MSK = ZoneInfo("Europe/Moscow")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Счётчик запросов ──────────────────────────────────────────────────────────
request_counter = {"date": date.today(), "count": 0}
DAILY_LIMIT = 800

def use_request() -> bool:
    today = date.today()
    if request_counter["date"] != today:
        request_counter["date"] = today
        request_counter["count"] = 0
    if request_counter["count"] >= DAILY_LIMIT:
        return False
    request_counter["count"] += 1
    return True

def requests_left() -> int:
    if request_counter["date"] != date.today():
        return DAILY_LIMIT
    return max(0, DAILY_LIMIT - request_counter["count"])

# ── Счётчик результатов ───────────────────────────────────────────────────────
stats: dict = {}  # {chat_id: {"win": 0, "loss": 0}}

def get_stats(chat_id: int) -> dict:
    if chat_id not in stats:
        stats[chat_id] = {"win": 0, "loss": 0}
    return stats[chat_id]

# ── Пары (по алфавиту) ────────────────────────────────────────────────────────
ALL_PAIRS = [
    "AUD/CAD", "AUD/CHF", "AUD/JPY", "AUD/USD",
    "CAD/CHF", "CAD/JPY",
    "CHF/JPY",
    "EUR/AUD", "EUR/CAD", "EUR/CHF", "EUR/GBP", "EUR/JPY", "EUR/USD",
    "GBP/AUD", "GBP/CAD", "GBP/CHF", "GBP/JPY", "GBP/USD",
    "USD/CAD", "USD/CHF", "USD/JPY",
]

DEFAULT_SETTINGS = {
    "expiry":       15,
    "scan_every":   15,
    "hour_from":    11,
    "hour_to":      23,
    "min_score":    75,
    "active_pairs": ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD",
                     "EUR/GBP", "GBP/JPY", "EUR/JPY", "USD/CHF"],
}

user_settings: dict = {}
signal_history: dict = {}  # {chat_id: [records]}

def get_settings(chat_id: int) -> dict:
    if chat_id not in user_settings:
        user_settings[chat_id] = DEFAULT_SETTINGS.copy()
        user_settings[chat_id]["active_pairs"] = DEFAULT_SETTINGS["active_pairs"].copy()
    return user_settings[chat_id]

def is_allowed(update: Update) -> bool:
    return update.effective_user.id in ALLOWED_USERS

def is_trading_time(settings: dict) -> bool:
    now = datetime.now(MSK)
    if now.weekday() >= 5:
        return False
    return settings["hour_from"] <= now.hour < settings["hour_to"]

def get_interval(expiry: int) -> str:
    if expiry <= 5:   return "1min"
    if expiry <= 10:  return "3min"
    if expiry <= 15:  return "5min"
    if expiry <= 30:  return "15min"
    if expiry <= 60:  return "30min"
    return "1h"

def minutes_to_next_period(scan_every: int) -> int:
    now     = datetime.now(MSK)
    minutes = now.hour * 60 + now.minute
    elapsed = minutes % scan_every
    return scan_every - elapsed

def should_scan_now(scan_every: int) -> bool:
    now     = datetime.now(MSK)
    minutes = now.hour * 60 + now.minute
    elapsed = minutes % scan_every
    return elapsed <= max(1, scan_every // 10)

# ── Данные ────────────────────────────────────────────────────────────────────
def fetch_forex_data(from_sym: str, to_sym: str, expiry: int) -> list | None:
    if not use_request():
        return None
    symbol   = f"{from_sym}/{to_sym}"
    interval = get_interval(expiry)
    url = (
        "https://api.twelvedata.com/time_series"
        f"?symbol={symbol}&interval={interval}&outputsize=50"
        f"&apikey={TWELVE_DATA_KEY}"
    )
    try:
        r    = requests.get(url, timeout=10)
        data = r.json()
        if data.get("status") == "error" or "values" not in data:
            return None
        candles = []
        for v in reversed(data["values"]):
            candles.append({
                "time":  v["datetime"],
                "open":  float(v["open"]),
                "high":  float(v["high"]),
                "low":   float(v["low"]),
                "close": float(v["close"]),
            })
        return candles
    except Exception as e:
        log.error(f"Ошибка {symbol}: {e}")
        return None

# ── Индикаторы ────────────────────────────────────────────────────────────────
def ema(values: list, period: int) -> list:
    result, k = [], 2 / (period + 1)
    for i, v in enumerate(values):
        result.append(v if i == 0 else v * k + result[-1] * (1 - k))
    return result

def rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1: return 50.0
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, period+1)]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, period+1)]
    ag, al = sum(gains)/period, sum(losses)/period
    return 100.0 if al == 0 else 100 - 100/(1 + ag/al)

def macd(closes: list) -> tuple:
    if len(closes) < 26: return 0.0, 0.0, 0.0
    e12 = ema(closes, 12); e26 = ema(closes, 26)
    ml  = [a-b for a,b in zip(e12, e26)]
    sig = ema(ml, 9)
    return ml[-1], sig[-1], ml[-1]-sig[-1]

def bollinger(closes: list, period: int = 20) -> tuple:
    if len(closes) < period:
        c = closes[-1]; return c*1.001, c, c*0.999
    w = closes[-period:]; m = sum(w)/period
    s = (sum((x-m)**2 for x in w)/period)**0.5
    return m+2*s, m, m-2*s

def stochastic(candles: list, period: int = 14) -> float:
    if len(candles) < period: return 50.0
    w = candles[-period:]
    hi, lo = max(c["high"] for c in w), min(c["low"] for c in w)
    cl = candles[-1]["close"]
    return 50.0 if hi == lo else (cl-lo)/(hi-lo)*100

def find_levels(candles: list, lookback: int = 20) -> tuple:
    if len(candles) < lookback + 2: return [], []
    window = candles[-(lookback+2):-1]
    supports, resistances = [], []
    for i in range(1, len(window)-1):
        if window[i]["low"]  < window[i-1]["low"]  and window[i]["low"]  < window[i+1]["low"]:
            supports.append(window[i]["low"])
        if window[i]["high"] > window[i-1]["high"] and window[i]["high"] > window[i+1]["high"]:
            resistances.append(window[i]["high"])
    return supports, resistances

def near_level(price: float, levels: list, threshold_pct: float = 0.001) -> bool:
    return any(abs(price - lvl) / lvl < threshold_pct for lvl in levels)

# ── Анализ ────────────────────────────────────────────────────────────────────
def analyze_pair(pair: str, candles: list, min_score: int) -> dict | None:
    if len(candles) < 35: return None
    closes = [c["close"] for c in candles]
    price  = closes[-1]
    vc, vp = 0, 0

    e9, e33 = ema(closes, 9), ema(closes, 33)
    if   e9[-1] > e33[-1] and e9[-2] <= e33[-2]: vc += 25
    elif e9[-1] < e33[-1] and e9[-2] >= e33[-2]: vp += 25
    elif e9[-1] > e33[-1]:                        vc += 10
    else:                                          vp += 10

    supports, resistances = find_levels(candles)
    at_support    = near_level(price, supports)
    at_resistance = near_level(price, resistances)
    if at_support:    vc += 20
    if at_resistance: vp += 20

    rv = rsi(closes)
    if   rv < 30: vc += 20
    elif rv > 70: vp += 20
    elif rv < 45: vc += 8
    elif rv > 55: vp += 8

    _, _, hist = macd(closes)
    _, _, ph   = macd(closes[:-1])
    if   hist > 0 and ph <= 0: vc += 20
    elif hist < 0 and ph >= 0: vp += 20
    elif hist > 0:             vc += 8
    else:                      vp += 8

    upper, _, lower = bollinger(closes)
    if   price <= lower: vc += 15
    elif price >= upper: vp += 15

    st = stochastic(candles)
    if   st < 20: vc += 15
    elif st > 80: vp += 15

    total = vc + vp
    if total == 0: return None
    direction = "CALL" if vc > vp else "PUT"
    score = int(max(vc, vp)/total*100)
    if score < min_score: return None

    stars = "⭐⭐⭐⭐⭐" if score >= 90 else "⭐⭐⭐⭐" if score >= 80 else "⭐⭐⭐"
    return {
        "pair": pair, "direction": direction, "score": score,
        "stars": stars, "price": price,
        "at_level": at_support or at_resistance,
        "rsi": rv, "stoch": st,
    }

# ── Форматирование сигнала (компактно) ────────────────────────────────────────
def format_signal(sig: dict, expiry: int) -> str:
    now   = datetime.now(MSK).strftime("%H:%M МСК")
    arrow = "🟢 CALL ▲" if sig["direction"] == "CALL" else "🔴 PUT ▼"
    return (
        f"*{sig['pair']}* | {arrow}\n"
        f"⏱ Экспирация: *{expiry} мин*\n"
        f"{sig['stars']} Оценка: *{sig['score']}%*\n"
        f"💰 `{sig['price']:.5f}` | {now}"
    )

# ── Форматирование результата ─────────────────────────────────────────────────
def format_result(rec: dict, current_price: float, won: bool) -> str:
    diff_p = (current_price - rec["price"]) / rec["price"] * 100
    icon   = "✅ ЗАШЁЛ" if won else "❌ НЕ ЗАШЁЛ"
    return (
        f"{icon} | *{rec['pair']}*\n"
        f"Вход: `{rec['price']:.5f}` → `{current_price:.5f}` ({diff_p:+.3f}%)"
    )

# ── Проверка результатов ──────────────────────────────────────────────────────
async def check_pending_results(bot, chat_id: int, pair: str, current_price: float):
    """Проверяет только те сигналы у которых истекла экспирация"""
    records = signal_history.get(chat_id, [])
    now     = datetime.now(MSK)
    st      = get_stats(chat_id)

    for rec in records:
        if rec.get("done"): continue
        if rec["pair"] != pair: continue

        # Проверяем только если прошло >= expiry минут
        elapsed = (now - rec["time"]).total_seconds() / 60
        if elapsed < rec["expiry"]: continue

        entry     = rec["price"]
        direction = rec["direction"]
        diff_pct  = (current_price - entry) / entry * 100

        if direction == "CALL":
            won = diff_pct > 0.01
            lost= diff_pct < -0.01
        else:
            won = diff_pct < -0.01
            lost= diff_pct > 0.01

        if won or lost:
            rec["done"] = True
            if won: st["win"] += 1
            else:   st["loss"] += 1
            total = st["win"] + st["loss"]
            winrate = int(st["win"] / total * 100) if total > 0 else 0
            result_text = format_result(rec, current_price, won)
            await bot.send_message(
                chat_id=chat_id,
                text=f"{result_text}\n📊 ✅{st['win']} ❌{st['loss']} | {winrate}%",
                reply_to_message_id=rec.get("message_id"),
                parse_mode="Markdown"
            )

# ── Сканирование ──────────────────────────────────────────────────────────────
async def do_scan(msg, settings: dict, chat_id: int = None, bot=None, silent_if_empty: bool = False):
    found = 0
    for pair in settings["active_pairs"]:
        if requests_left() == 0:
            await msg.reply_text("⚠️ Лимит 800 запросов исчерпан. Продолжу завтра.")
            break
        fs, ts  = pair.split("/")
        candles = fetch_forex_data(fs, ts, settings["expiry"])
        if not candles:
            await asyncio.sleep(8); continue

        current_price = candles[-1]["close"]

        # Проверка результатов прошлых сигналов по этой паре
        if chat_id and bot:
            await check_pending_results(bot, chat_id, pair, current_price)

        sig = analyze_pair(pair, candles, settings["min_score"])
        if sig:
            found += 1
            sent = await msg.reply_text(format_signal(sig, settings["expiry"]), parse_mode="Markdown")
            if chat_id:
                if chat_id not in signal_history:
                    signal_history[chat_id] = []
                signal_history[chat_id].append({
                    "pair":       pair,
                    "direction":  sig["direction"],
                    "price":      sig["price"],
                    "expiry":     settings["expiry"],
                    "time":       datetime.now(MSK),
                    "done":       False,
                    "message_id": sent.message_id if sent else None,
                })
                signal_history[chat_id] = signal_history[chat_id][-30:]

        await asyncio.sleep(8)

    if found == 0 and not silent_if_empty:
        st    = get_stats(chat_id) if chat_id else {"win":0,"loss":0}
        total = st["win"] + st["loss"]
        wr    = int(st["win"]/total*100) if total > 0 else 0
        await msg.reply_text(
            f"🔕 Сигналов нет\n"
            f"💾 Запросов: *{requests_left()}/800*\n"
            f"📊 Счёт: ✅{st['win']} ❌{st['loss']} | {wr}%",
            parse_mode="Markdown"
        )
    return found

async def auto_scan(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id  = ctx.job.chat_id
    settings = get_settings(chat_id)
    if not is_trading_time(settings):
        return
    if requests_left() == 0:
        return
    log.info(f"Авто-скан {chat_id}")

    # Отправляем в чат через bot
    class FakeMsg:
        async def reply_text(self, text, **kwargs):
            return await ctx.bot.send_message(chat_id=chat_id, text=text, **kwargs)

    await do_scan(FakeMsg(), settings, chat_id=chat_id, bot=ctx.bot)

# ── Постоянное меню ───────────────────────────────────────────────────────────
MAIN_KEYBOARD = ReplyKeyboardMarkup([
    ["📡 Разовая проверка", "⚙️ Настройки"],
    ["▶️ Подписаться",      "⏹ Отписаться"],
    ["📊 Статус",           "❓ Помощь"],
], resize_keyboard=True)

# ── Команды ───────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    s   = get_settings(update.effective_chat.id)
    now = datetime.now(MSK)
    wd  = "⛔ Выходной" if now.weekday() >= 5 else "✅ Рабочий день"
    ih  = "✅ В торговых часах" if is_trading_time(s) else "💤 Вне часов"
    st  = get_stats(update.effective_chat.id)
    total = st["win"] + st["loss"]
    wr  = int(st["win"]/total*100) if total > 0 else 0
    await update.message.reply_text(
        f"🤖 *Forex Signal Bot v4*\n\n"
        f"{wd} | {ih}\n"
        f"⏱ *{s['expiry']} мин* | TF: *{get_interval(s['expiry'])}* | каждые *{s['scan_every']} мин*\n"
        f"🕐 *{s['hour_from']}:00–{s['hour_to']}:00 МСК*\n"
        f"🎯 Скор: *{s['min_score']}%* | Пар: *{len(s['active_pairs'])}*\n"
        f"💾 Запросов: *{requests_left()}/800*\n"
        f"📊 Счёт: ✅{st['win']} ❌{st['loss']} | {wr}%",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    chat_id = update.effective_chat.id
    s       = get_settings(chat_id)
    now     = datetime.now(MSK)
    jobs    = ctx.job_queue.get_jobs_by_name(str(chat_id))
    sub     = "✅ Активна" if jobs else "❌ Не активна"
    wd      = "⛔ Выходной" if now.weekday() >= 5 else "✅ Рабочий день"
    ih      = "✅ Торговые часы" if is_trading_time(s) else "💤 Вне часов"
    nxt     = minutes_to_next_period(s["scan_every"])
    st      = get_stats(chat_id)
    total   = st["win"] + st["loss"]
    wr      = int(st["win"]/total*100) if total > 0 else 0
    open_s  = len([r for r in signal_history.get(chat_id, []) if not r.get("done")])

    kb = [[InlineKeyboardButton("🔄 Сбросить счёт", callback_data="reset_stats")]]
    await (update.message or update.callback_query.message).reply_text(
        f"📊 *Статус*\n\n"
        f"🖥 Сервер: *✅ Работает*\n"
        f"📡 Подписка: *{sub}*\n"
        f"📅 {wd} | {ih}\n"
        f"⏰ До скана: *~{nxt} мин*\n\n"
        f"⏱ *{s['expiry']} мин* | TF: *{get_interval(s['expiry'])}*\n"
        f"🔄 Каждые: *{s['scan_every']} мин*\n"
        f"🕐 *{s['hour_from']}:00–{s['hour_to']}:00 МСК*\n"
        f"📊 Пар: *{len(s['active_pairs'])}* | Скор: *{s['min_score']}%*\n\n"
        f"💾 Запросов: *{requests_left()}/800*\n"
        f"📋 Открытых сигналов: *{open_s}*\n"
        f"🏆 Счёт: ✅{st['win']} ❌{st['loss']} | *{wr}%*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    msg     = update.message or update.callback_query.message
    chat_id = update.effective_chat.id
    s       = get_settings(chat_id)
    for job in ctx.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    if should_scan_now(s["scan_every"]):
        first_in, first_msg = 5, "Первый скан через ~5 сек"
    else:
        first_in = minutes_to_next_period(s["scan_every"]) * 60
        first_msg = f"Первый скан через ~{minutes_to_next_period(s['scan_every'])} мин"
    ctx.job_queue.run_repeating(auto_scan, interval=s["scan_every"]*60,
                                first=first_in, chat_id=chat_id, name=str(chat_id))
    await msg.reply_text(
        f"✅ *Подписка активирована*\n\n"
        f"🔄 Каждые *{s['scan_every']} мин*\n"
        f"🕐 *{s['hour_from']}:00–{s['hour_to']}:00 МСК*, без выходных\n"
        f"⏱ *{s['expiry']} мин* | TF: *{get_interval(s['expiry'])}*\n"
        f"⏰ {first_msg}",
        parse_mode="Markdown",
    )

async def cmd_unsubscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    chat_id = update.effective_chat.id
    for job in ctx.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    await (update.message or update.callback_query.message).reply_text("❌ Автосигналы отключены.")

async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    chat_id = update.effective_chat.id
    s       = get_settings(chat_id)
    msg     = update.message or update.callback_query.message
    kb = [
        [InlineKeyboardButton("⏱ Экспирация", callback_data="set_expiry")],
        [InlineKeyboardButton("🔄 Период сканирования", callback_data="set_period")],
        [InlineKeyboardButton("🕐 Торговые часы", callback_data="set_hours")],
        [InlineKeyboardButton("🎯 Минимальный скор", callback_data="set_score")],
        [InlineKeyboardButton("📊 Выбор пар", callback_data="set_pairs")],
    ]
    await msg.reply_text(
        f"⚙️ *Настройки*\n\n"
        f"⏱ *{s['expiry']} мин* → TF: *{get_interval(s['expiry'])}*\n"
        f"🔄 Каждые: *{s['scan_every']} мин*\n"
        f"🕐 *{s['hour_from']}:00–{s['hour_to']}:00 МСК*\n"
        f"🎯 Скор: *{s['min_score']}%*\n"
        f"📊 Пар: *{len(s['active_pairs'])}* из {len(ALL_PAIRS)}\n"
        f"💾 Запросов: *{requests_left()}/800*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await (update.message or update.callback_query.message).reply_text(
        "📖 *Forex Signal Bot v4*\n\n"
        "*Индикаторы:* EMA 9/33, S/R уровни, RSI, MACD, Bollinger, Stochastic\n\n"
        "*TF по экспирации:*\n"
        "5м→1м | 10м→3м | 15м→5м | 30м→15м | 60м→30м\n\n"
        "*Результат* проверяется ровно через время экспирации\n"
        "*Счётчик* ✅/❌ ведётся автоматически\n\n"
        "*/start* — главное меню\n"
        "*/status* — статус\n"
        "*/subscribe* — автосигналы\n"
        "*/unsubscribe* — отключить\n"
        "*/settings* — настройки\n\n"
        "⚠️ _Не финансовый совет._",
        parse_mode="Markdown",
    )

# ── Обработчик меню ───────────────────────────────────────────────────────────
async def menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    text = update.message.text
    if   text == "📊 Статус":           await cmd_status(update, ctx)
    elif text == "⚙️ Настройки":        await cmd_settings(update, ctx)
    elif text == "▶️ Подписаться":       await cmd_subscribe(update, ctx)
    elif text == "⏹ Отписаться":        await cmd_unsubscribe(update, ctx)
    elif text == "❓ Помощь":            await cmd_help(update, ctx)
    elif text == "📡 Разовая проверка":
        kb = [[InlineKeyboardButton(f"{v} мин", callback_data=f"quick_{v}") for v in [5,10,15,30]],
              [InlineKeyboardButton(f"{v} мин", callback_data=f"quick_{v}") for v in [45,60]]]
        await update.message.reply_text(
            "📡 *Разовая проверка*\nВыбери экспирацию:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

# ── Inline кнопки ─────────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_allowed(update): return
    chat_id = update.effective_chat.id
    s       = get_settings(chat_id)
    data    = q.data

    if   data == "reset_stats":
        stats[chat_id] = {"win": 0, "loss": 0}
        await q.message.reply_text("🔄 Счёт сброшен: ✅0 ❌0")
    elif data == "subscribe":      await cmd_subscribe(update, ctx)
    elif data == "unsubscribe":    await cmd_unsubscribe(update, ctx)
    elif data == "settings_menu":  await cmd_settings(update, ctx)
    elif data == "help":           await cmd_help(update, ctx)

    elif data.startswith("quick_"):
        expiry = int(data.split("_")[1])
        temp   = s.copy(); temp["expiry"] = expiry
        temp["active_pairs"] = s["active_pairs"].copy()
        await q.message.reply_text(
            f"🔍 *Разовая проверка* | {expiry} мин | TF: {get_interval(expiry)}\n"
            f"💾 Запросов: {requests_left()}",
            parse_mode="Markdown"
        )
        await do_scan(q.message, temp, chat_id=chat_id, bot=ctx.bot)

    elif data == "set_expiry":
        kb = [[InlineKeyboardButton(f"{v} мин → {get_interval(v)}", callback_data=f"expiry_{v}")]
              for v in [5, 10, 15, 30, 60]]
        await q.message.reply_text("⏱ Выбери экспирацию:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("expiry_"):
        s["expiry"] = int(data.split("_")[1])
        await q.message.reply_text(
            f"✅ *{s['expiry']} мин* → TF: *{get_interval(s['expiry'])}*\nПерезапусти: /subscribe",
            parse_mode="Markdown")

    elif data == "set_period":
        kb = [[InlineKeyboardButton(f"{v} мин", callback_data=f"period_{v}") for v in [5,10,15,30]]]
        await q.message.reply_text("🔄 Как часто сканировать?", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("period_"):
        s["scan_every"] = int(data.split("_")[1])
        await q.message.reply_text(
            f"✅ Каждые *{s['scan_every']} мин*\nПерезапусти: /subscribe",
            parse_mode="Markdown")

    elif data == "set_hours":
        kb = [
            [InlineKeyboardButton("11:00–23:00 (весь день)", callback_data="hours_11_23")],
            [InlineKeyboardButton("16:00–20:00 (NY сессия)", callback_data="hours_16_20")],
            [InlineKeyboardButton("10:00–14:00 (Лондон)",    callback_data="hours_10_14")],
            [InlineKeyboardButton("10:00–23:00 (максимум)",  callback_data="hours_10_23")],
        ]
        await q.message.reply_text("🕐 Торговые часы (МСК):", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("hours_"):
        parts = data.split("_")
        s["hour_from"], s["hour_to"] = int(parts[1]), int(parts[2])
        await q.message.reply_text(
            f"✅ *{s['hour_from']}:00–{s['hour_to']}:00 МСК*\nПерезапусти: /subscribe",
            parse_mode="Markdown")

    elif data == "set_score":
        kb = [[InlineKeyboardButton(f"{v}%", callback_data=f"score_{v}") for v in [70,75,80,85]]]
        await q.message.reply_text(
            "🎯 Минимальный скор:\n_(выше = меньше, но лучше)_",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("score_"):
        s["min_score"] = int(data.split("_")[1])
        await q.message.reply_text(f"✅ Скор: *{s['min_score']}%*", parse_mode="Markdown")

    elif data == "set_pairs":
        kb = []
        for pair in ALL_PAIRS:
            icon = "✅" if pair in s["active_pairs"] else "⬜"
            kb.append([InlineKeyboardButton(f"{icon} {pair}", callback_data=f"toggle_{pair}")])
        kb.append([
            InlineKeyboardButton("✅ Выбрать все",  callback_data="pairs_all"),
            InlineKeyboardButton("⬜ Убрать все",   callback_data="pairs_none"),
        ])
        kb.append([InlineKeyboardButton(f"💾 Готово ({len(s['active_pairs'])} пар)", callback_data="pairs_done")])
        await q.message.reply_text("📊 Выбери пары:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("toggle_"):
        pair = data[7:]
        if pair in s["active_pairs"]: s["active_pairs"].remove(pair)
        else: s["active_pairs"].append(pair)
        kb = []
        for p in ALL_PAIRS:
            icon = "✅" if p in s["active_pairs"] else "⬜"
            kb.append([InlineKeyboardButton(f"{icon} {p}", callback_data=f"toggle_{p}")])
        kb.append([
            InlineKeyboardButton("✅ Выбрать все", callback_data="pairs_all"),
            InlineKeyboardButton("⬜ Убрать все",  callback_data="pairs_none"),
        ])
        kb.append([InlineKeyboardButton(f"💾 Готово ({len(s['active_pairs'])} пар)", callback_data="pairs_done")])
        await q.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(kb))

    elif data == "pairs_all":
        s["active_pairs"] = ALL_PAIRS.copy()
        kb = []
        for p in ALL_PAIRS:
            kb.append([InlineKeyboardButton(f"✅ {p}", callback_data=f"toggle_{p}")])
        kb.append([
            InlineKeyboardButton("✅ Выбрать все", callback_data="pairs_all"),
            InlineKeyboardButton("⬜ Убрать все",  callback_data="pairs_none"),
        ])
        kb.append([InlineKeyboardButton(f"💾 Готово ({len(s['active_pairs'])} пар)", callback_data="pairs_done")])
        await q.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(kb))

    elif data == "pairs_none":
        s["active_pairs"] = []
        kb = []
        for p in ALL_PAIRS:
            kb.append([InlineKeyboardButton(f"⬜ {p}", callback_data=f"toggle_{p}")])
        kb.append([
            InlineKeyboardButton("✅ Выбрать все", callback_data="pairs_all"),
            InlineKeyboardButton("⬜ Убрать все",  callback_data="pairs_none"),
        ])
        kb.append([InlineKeyboardButton("💾 Готово (0 пар)", callback_data="pairs_done")])
        await q.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(kb))

    elif data == "pairs_done":
        await q.message.reply_text(
            f"✅ Активных пар: *{len(s['active_pairs'])}*\n{', '.join(sorted(s['active_pairs']))}",
            parse_mode="Markdown")

# ── Запуск ────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("subscribe",   cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("settings",    cmd_settings))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))
    log.info("🤖 Бот v4 запущен.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
