#!/usr/bin/env python3
"""Forex Signal Bot — v2"""

import asyncio
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
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

def get_settings(chat_id: int) -> dict:
    if chat_id not in user_settings:
        user_settings[chat_id] = DEFAULT_SETTINGS.copy()
        user_settings[chat_id]["active_pairs"] = DEFAULT_SETTINGS["active_pairs"].copy()
    return user_settings[chat_id]

def is_allowed(update: Update) -> bool:
    return update.effective_user.id in ALLOWED_USERS

def is_trading_time(settings: dict) -> bool:
    now = datetime.now(MSK)
    if now.weekday() >= 5:   # сб=5, вс=6
        return False
    return settings["hour_from"] <= now.hour < settings["hour_to"]

def get_interval(expiry: int) -> str:
    half = expiry // 2
    if half <= 1:  return "1min"
    if half <= 5:  return "5min"
    if half <= 15: return "15min"
    if half <= 30: return "30min"
    return "1h"

# ── Данные ────────────────────────────────────────────────────────────────────
def fetch_forex_data(from_sym: str, to_sym: str, expiry: int) -> list | None:
    if not use_request():
        log.warning("Лимит запросов исчерпан!")
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
            log.warning(f"Нет данных {symbol}: {data.get('message','')}")
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

# ── Уровни поддержки/сопротивления (swing high/low) ──────────────────────────
def find_levels(candles: list, lookback: int = 20) -> tuple[list, list]:
    """Находит swing high и swing low за последние lookback свечей"""
    if len(candles) < lookback + 2:
        return [], []
    window   = candles[-(lookback+2):-1]
    supports    = []
    resistances = []
    for i in range(1, len(window)-1):
        # Swing low — локальный минимум
        if window[i]["low"] < window[i-1]["low"] and window[i]["low"] < window[i+1]["low"]:
            supports.append(window[i]["low"])
        # Swing high — локальный максимум
        if window[i]["high"] > window[i-1]["high"] and window[i]["high"] > window[i+1]["high"]:
            resistances.append(window[i]["high"])
    return supports, resistances

def near_level(price: float, levels: list, threshold_pct: float = 0.001) -> bool:
    """Проверяет близость цены к уровню (порог 0.1%)"""
    for lvl in levels:
        if abs(price - lvl) / lvl < threshold_pct:
            return True
    return False

# ── Анализ пары ───────────────────────────────────────────────────────────────
def analyze_pair(pair: str, candles: list, min_score: int) -> dict | None:
    if len(candles) < 35: return None
    closes = [c["close"] for c in candles]
    price  = closes[-1]
    vc, vp, details = 0, 0, []

    # EMA 9/33
    e9  = ema(closes, 9)
    e33 = ema(closes, 33)
    if   e9[-1] > e33[-1] and e9[-2] <= e33[-2]: vc += 25; details.append("📈 EMA 9/33 кросс вверх")
    elif e9[-1] < e33[-1] and e9[-2] >= e33[-2]: vp += 25; details.append("📉 EMA 9/33 кросс вниз")
    elif e9[-1] > e33[-1]:                        vc += 10; details.append("📈 EMA 9/33 тренд вверх")
    else:                                          vp += 10; details.append("📉 EMA 9/33 тренд вниз")

    # Уровни поддержки/сопротивления
    supports, resistances = find_levels(candles)
    at_support    = near_level(price, supports)
    at_resistance = near_level(price, resistances)
    if at_support:
        vc += 20; details.append(f"🟩 Цена у уровня поддержки ({price:.5f})")
    if at_resistance:
        vp += 20; details.append(f"🟥 Цена у уровня сопротивления ({price:.5f})")

    # RSI
    rv = rsi(closes)
    if   rv < 30: vc += 20; details.append(f"💚 RSI перепродан ({rv:.1f})")
    elif rv > 70: vp += 20; details.append(f"🔴 RSI перекуплен ({rv:.1f})")
    elif rv < 45: vc += 8;  details.append(f"↗️ RSI бычий ({rv:.1f})")
    elif rv > 55: vp += 8;  details.append(f"↘️ RSI медвежий ({rv:.1f})")

    # MACD
    _, _, hist = macd(closes)
    _, _, ph   = macd(closes[:-1])
    if   hist > 0 and ph <= 0: vc += 20; details.append("📊 MACD пересёк вверх")
    elif hist < 0 and ph >= 0: vp += 20; details.append("📊 MACD пересёк вниз")
    elif hist > 0:             vc += 8;  details.append("📊 MACD положительный")
    else:                      vp += 8;  details.append("📊 MACD отрицательный")

    # Bollinger
    upper, _, lower = bollinger(closes)
    if   price <= lower: vc += 15; details.append("🎯 Цена у нижней полосы Боллинджера")
    elif price >= upper: vp += 15; details.append("🎯 Цена у верхней полосы Боллинджера")

    # Stochastic
    st = stochastic(candles)
    if   st < 20: vc += 15; details.append(f"⚡ Stoch перепродан ({st:.1f})")
    elif st > 80: vp += 15; details.append(f"⚡ Stoch перекуплен ({st:.1f})")

    total = vc + vp
    if total == 0: return None
    if vc > vp:
        direction = "CALL"; score = int(vc/total*100)
    else:
        direction = "PUT";  score = int(vp/total*100)
    if score < min_score: return None

    stars = "⭐⭐⭐⭐⭐" if score >= 90 else "⭐⭐⭐⭐" if score >= 80 else "⭐⭐⭐"
    return {"pair": pair, "direction": direction, "score": score,
            "stars": stars, "details": details, "price": price,
            "at_level": at_support or at_resistance}

# ── Форматирование ────────────────────────────────────────────────────────────
def format_signal(sig: dict, expiry: int) -> str:
    now   = datetime.now(MSK).strftime("%H:%M МСК")
    arrow = "🟢 CALL ▲" if sig["direction"] == "CALL" else "🔴 PUT  ▼"
    dtext = "\n".join(f"  • {d}" for d in sig["details"])
    level_tag = "\n🎯 *СИГНАЛ У УРОВНЯ*" if sig.get("at_level") else ""
    return (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 *СИГНАЛ* | {now}{level_tag}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Пара:          *{sig['pair']}*\n"
        f"📌 Направление:  *{arrow}*\n"
        f"⏱ Экспирация:   *{expiry} минут*\n"
        f"📊 Уверенность:  {sig['stars']} *{sig['score']}%*\n"
        f"💰 Цена входа:   `{sig['price']:.5f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Основания:*\n{dtext}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _Не финансовый совет._"
    )

# ── Сканирование ──────────────────────────────────────────────────────────────
async def do_scan(msg, settings: dict, silent_if_empty: bool = False):
    found = 0
    for pair in settings["active_pairs"]:
        if requests_left() == 0:
            await msg.reply_text("⚠️ Лимит запросов на сегодня исчерпан (800/день). Продолжу завтра.")
            break
        fs, ts  = pair.split("/")
        candles = fetch_forex_data(fs, ts, settings["expiry"])
        if not candles:
            await asyncio.sleep(8); continue
        sig = analyze_pair(pair, candles, settings["min_score"])
        if sig:
            found += 1
            await msg.reply_text(format_signal(sig, settings["expiry"]), parse_mode="Markdown")
        await asyncio.sleep(8)
    if found == 0 and not silent_if_empty:
        await msg.reply_text(
            f"🔕 Сигналов нет.\n"
            f"Осталось запросов сегодня: *{requests_left()}*",
            parse_mode="Markdown"
        )
    return found

async def auto_scan(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id  = ctx.job.chat_id
    settings = get_settings(chat_id)
    if not is_trading_time(settings):
        log.info(f"Вне торгового времени для {chat_id}")
        return
    if requests_left() == 0:
        log.warning("Лимит запросов исчерпан")
        return
    log.info(f"Авто-скан для {chat_id}")
    found = 0
    for pair in settings["active_pairs"]:
        if requests_left() == 0: break
        fs, ts  = pair.split("/")
        candles = fetch_forex_data(fs, ts, settings["expiry"])
        if not candles:
            await asyncio.sleep(8); continue
        sig = analyze_pair(pair, candles, settings["min_score"])
        if sig:
            found += 1
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=format_signal(sig, settings["expiry"]),
                parse_mode="Markdown"
            )
        await asyncio.sleep(8)
    log.info(f"Авто-скан {chat_id}: {found} сигналов, осталось запросов: {requests_left()}")

# ── Команды ───────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Доступ запрещён."); return
    s = get_settings(update.effective_chat.id)
    now = datetime.now(MSK)
    weekend = "⛔ Выходной" if now.weekday() >= 5 else "✅ Рабочий день"
    in_hours = "✅ В торговых часах" if is_trading_time(s) else "💤 Вне торговых часов"
    kb = [
        [InlineKeyboardButton("📡 Разовая проверка", callback_data="quick_scan_menu")],
        [InlineKeyboardButton("▶️ Подписаться", callback_data="subscribe"),
         InlineKeyboardButton("⏹ Отписаться", callback_data="unsubscribe")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings_menu")],
        [InlineKeyboardButton("📊 Запросы: " + str(requests_left()) + "/800", callback_data="show_requests")],
    ]
    await update.message.reply_text(
        f"🤖 *Forex Signal Bot v2*\n\n"
        f"{weekend} | {in_hours}\n"
        f"⏱ Экспирация: *{s['expiry']} мин* | TF: *{get_interval(s['expiry'])}*\n"
        f"🔄 Скан каждые: *{s['scan_every']} мин*\n"
        f"🕐 Часы: *{s['hour_from']}:00–{s['hour_to']}:00 МСК*\n"
        f"🎯 Мин. скор: *{s['min_score']}%*\n"
        f"📊 Пар: *{len(s['active_pairs'])}* | Запросов: *{requests_left()}/800*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    msg = update.message or update.callback_query.message
    s   = get_settings(update.effective_chat.id)
    await msg.reply_text(f"🔍 Сканирую {len(s['active_pairs'])} пар... (TF: {get_interval(s['expiry'])})\n💾 Запросов осталось: {requests_left()}")
    await do_scan(msg, s)

async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    msg     = update.message or update.callback_query.message
    chat_id = update.effective_chat.id
    s       = get_settings(chat_id)
    for job in ctx.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    ctx.job_queue.run_repeating(auto_scan, interval=s["scan_every"]*60,
                                first=10, chat_id=chat_id, name=str(chat_id))
    await msg.reply_text(
        f"✅ *Подписка активирована!*\n\n"
        f"🔄 Каждые *{s['scan_every']} мин*\n"
        f"🕐 *{s['hour_from']}:00–{s['hour_to']}:00 МСК*, без выходных\n"
        f"⏱ Экспирация *{s['expiry']} мин* | TF: *{get_interval(s['expiry'])}*\n"
        f"📊 Пар: *{len(s['active_pairs'])}*",
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
        f"⏱ Экспирация: *{s['expiry']} мин* → TF: *{get_interval(s['expiry'])}*\n"
        f"🔄 Скан каждые: *{s['scan_every']} мин*\n"
        f"🕐 Часы: *{s['hour_from']}:00–{s['hour_to']}:00 МСК*\n"
        f"🎯 Мин. скор: *{s['min_score']}%*\n"
        f"📊 Активных пар: *{len(s['active_pairs'])}* из {len(ALL_PAIRS)}\n"
        f"💾 Запросов сегодня: *{requests_left()}/800*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    msg = update.message or update.callback_query.message
    await msg.reply_text(
        "📖 *Forex Signal Bot v2*\n\n"
        "*Индикаторы:*\n"
        "• EMA 9/33 — тренд и кросс\n"
        "• Уровни S/R — swing high/low\n"
        "• RSI 14\n• MACD\n• Bollinger Bands\n• Stochastic\n\n"
        "*Таймфрейм* подбирается автоматически:\n"
        "экспирация 15м → свечи 5м\n"
        "экспирация 30м → свечи 15м\n\n"
        "*/start* — главное меню\n"
        "*/scan* — разовое сканирование\n"
        "*/subscribe* — автосигналы\n"
        "*/unsubscribe* — отключить\n"
        "*/settings* — настройки\n\n"
        "⚠️ _Не финансовый совет._",
        parse_mode="Markdown",
    )

# ── Кнопки ────────────────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_allowed(update):
        await q.message.reply_text("⛔ Доступ запрещён."); return
    chat_id = update.effective_chat.id
    s       = get_settings(chat_id)
    data    = q.data

    if data == "scan":              await cmd_scan(update, ctx)
    elif data == "subscribe":       await cmd_subscribe(update, ctx)
    elif data == "unsubscribe":     await cmd_unsubscribe(update, ctx)
    elif data == "settings_menu":   await cmd_settings(update, ctx)
    elif data == "help":            await cmd_help(update, ctx)

    elif data == "show_requests":
        await q.message.reply_text(
            f"💾 *Запросы Twelve Data*\n\n"
            f"Использовано сегодня: *{800 - requests_left()}*\n"
            f"Осталось: *{requests_left()}/800*\n"
            f"Сброс: каждый день в 00:00",
            parse_mode="Markdown"
        )

    # ── Разовая проверка (не меняет настройки) ──
    elif data == "quick_scan_menu":
        kb = [[InlineKeyboardButton(f"{v} мин", callback_data=f"quick_{v}") for v in [5,10,15,30]]]
        kb.append([InlineKeyboardButton(f"{v} мин", callback_data=f"quick_{v}") for v in [45,60]])
        await q.message.reply_text(
            "📡 *Разовая проверка*\nВыбери экспирацию (основные настройки не изменятся):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data.startswith("quick_"):
        expiry = int(data.split("_")[1])
        temp   = s.copy()
        temp["active_pairs"] = s["active_pairs"].copy()
        temp["expiry"] = expiry
        msg = q.message
        await msg.reply_text(
            f"🔍 Разовая проверка с экспирацией *{expiry} мин* (TF: {get_interval(expiry)})\n"
            f"💾 Запросов осталось: {requests_left()}",
            parse_mode="Markdown"
        )
        await do_scan(msg, temp)

    # ── Экспирация ──
    elif data == "set_expiry":
        kb = [[InlineKeyboardButton(f"{v} мин → TF {get_interval(v)}", callback_data=f"expiry_{v}")]
              for v in [5, 10, 15, 30, 60]]
        await q.message.reply_text("⏱ Выбери экспирацию:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("expiry_"):
        s["expiry"] = int(data.split("_")[1])
        await q.message.reply_text(
            f"✅ Экспирация: *{s['expiry']} мин* → TF: *{get_interval(s['expiry'])}*\n"
            f"Перезапусти подписку: /subscribe",
            parse_mode="Markdown")

    # ── Период ──
    elif data == "set_period":
        kb = [[InlineKeyboardButton(f"{v} мин", callback_data=f"period_{v}") for v in [5,10,15,30]]]
        await q.message.reply_text("🔄 Как часто сканировать?", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("period_"):
        s["scan_every"] = int(data.split("_")[1])
        await q.message.reply_text(
            f"✅ Каждые *{s['scan_every']} мин*\nПерезапусти: /subscribe",
            parse_mode="Markdown")

    # ── Торговые часы ──
    elif data == "set_hours":
        kb = [
            [InlineKeyboardButton("11:00–23:00", callback_data="hours_11_23"),
             InlineKeyboardButton("16:00–20:00", callback_data="hours_16_20")],
            [InlineKeyboardButton("10:00–22:00", callback_data="hours_10_22"),
             InlineKeyboardButton("09:00–23:00", callback_data="hours_9_23")],
        ]
        await q.message.reply_text("🕐 Торговые часы (МСК):", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("hours_"):
        parts = data.split("_")
        s["hour_from"], s["hour_to"] = int(parts[1]), int(parts[2])
        await q.message.reply_text(
            f"✅ *{s['hour_from']}:00–{s['hour_to']}:00 МСК*\nПерезапусти: /subscribe",
            parse_mode="Markdown")

    # ── Скор ──
    elif data == "set_score":
        kb = [[InlineKeyboardButton(f"{v}%", callback_data=f"score_{v}") for v in [70,75,80,85]]]
        await q.message.reply_text(
            "🎯 Минимальный скор:\n_(выше = меньше, но лучше)_",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("score_"):
        s["min_score"] = int(data.split("_")[1])
        await q.message.reply_text(f"✅ Мин. скор: *{s['min_score']}%*", parse_mode="Markdown")

    # ── Выбор пар ──
    elif data == "set_pairs":
        kb = []
        for pair in ALL_PAIRS:
            icon = "✅" if pair in s["active_pairs"] else "⬜"
            kb.append([InlineKeyboardButton(f"{icon} {pair}", callback_data=f"toggle_{pair}")])
        kb.append([InlineKeyboardButton("💾 Готово", callback_data="pairs_done")])
        await q.message.reply_text(
            f"📊 Пары (активно: {len(s['active_pairs'])}):",
            reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("toggle_"):
        pair = data[7:]
        if pair in s["active_pairs"]: s["active_pairs"].remove(pair)
        else: s["active_pairs"].append(pair)
        kb = []
        for p in ALL_PAIRS:
            icon = "✅" if p in s["active_pairs"] else "⬜"
            kb.append([InlineKeyboardButton(f"{icon} {p}", callback_data=f"toggle_{p}")])
        kb.append([InlineKeyboardButton("💾 Готово", callback_data="pairs_done")])
        await q.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(kb))

    elif data == "pairs_done":
        await q.message.reply_text(
            f"✅ Активных пар: *{len(s['active_pairs'])}*\n{', '.join(sorted(s['active_pairs']))}",
            parse_mode="Markdown")

# ── Запуск ────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("scan",        cmd_scan))
    app.add_handler(CommandHandler("subscribe",   cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("settings",    cmd_settings))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CallbackQueryHandler(button_handler))
    log.info("🤖 Бот v2 запущен.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
