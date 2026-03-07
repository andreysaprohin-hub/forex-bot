#!/usr/bin/env python3
"""Forex Signal Bot — v5 (три режима: ручной / лучший 30min / лучший 15min)"""

import asyncio
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

BOT_TOKEN       = "8612612451:AAE7dMyGwf1Ddigz23Ygeop5ubh1nkrm6M8"
TWELVE_DATA_KEY = "55dae6924d864941b1ab27052b0871ef"
ALLOWED_USERS   = {544863362}

MSK = ZoneInfo("Europe/Moscow")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

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

stats: dict = {}

def get_stats(chat_id: int) -> dict:
    if chat_id not in stats:
        stats[chat_id] = {"win": 0, "loss": 0}
    return stats[chat_id]

ALL_PAIRS = [
    "AUD/CAD","AUD/CHF","AUD/JPY","AUD/USD",
    "CAD/CHF","CAD/JPY","CHF/JPY",
    "EUR/AUD","EUR/CAD","EUR/CHF","EUR/GBP","EUR/JPY","EUR/USD",
    "GBP/AUD","GBP/CAD","GBP/CHF","GBP/JPY","GBP/USD",
    "USD/CAD","USD/CHF","USD/JPY",
]

BEST_30MIN = {
    "GBP/USD": [18, 21],
    "CAD/CHF": [20, 21, 22],
    "USD/CHF": [15, 16, 18],
    "AUD/CHF": [14, 18, 20, 22],
}

BEST_15MIN = {
    "AUD/USD": [12, 13, 16, 20],
    "GBP/AUD": [12, 15, 21],
    "CHF/JPY": [22],
    "CAD/JPY": [11, 21],
    "AUD/CAD": [13, 14],
    "USD/CHF": [13],
}

MODE_NAMES = {"manual": "🔧 Ручной", "best30": "🏆 Лучший 30min", "best15": "⚡ Лучший 15min"}

DEFAULT_SETTINGS = {
    "mode": "manual", "expiry": 30, "scan_every": 30,
    "hour_from": 11, "hour_to": 23, "min_score": 75,
    "active_pairs": ["EUR/USD","GBP/USD","USD/JPY","AUD/USD","EUR/GBP","GBP/JPY","EUR/JPY","USD/CHF"],
}

user_settings: dict = {}
signal_history: dict = {}

def get_settings(chat_id: int) -> dict:
    if chat_id not in user_settings:
        user_settings[chat_id] = DEFAULT_SETTINGS.copy()
        user_settings[chat_id]["active_pairs"] = DEFAULT_SETTINGS["active_pairs"].copy()
    return user_settings[chat_id]

def get_effective_settings(chat_id: int) -> dict:
    s = get_settings(chat_id)
    mode = s.get("mode", "manual")
    if mode == "best30":
        return {**s, "active_pairs": list(BEST_30MIN.keys()), "expiry": 90,
                "scan_every": 30, "min_score": 85, "hour_from": 11, "hour_to": 23,
                "_best_hours": BEST_30MIN}
    elif mode == "best15":
        return {**s, "active_pairs": list(BEST_15MIN.keys()), "expiry": 45,
                "scan_every": 15, "min_score": 75, "hour_from": 11, "hour_to": 23,
                "_best_hours": BEST_15MIN}
    else:
        return {**s, "_best_hours": None}

def is_allowed(update: Update) -> bool:
    return update.effective_user.id in ALLOWED_USERS

def is_trading_time(settings: dict) -> bool:
    now = datetime.now(MSK)
    if now.weekday() >= 5: return False
    return settings["hour_from"] <= now.hour < settings["hour_to"]

def is_best_hour(pair: str, best_hours) -> bool:
    if not best_hours or pair not in best_hours: return True
    return datetime.now(MSK).hour in best_hours[pair]

def get_interval(expiry: int) -> str:
    if expiry <= 5:  return "1min"
    if expiry <= 10: return "3min"
    if expiry <= 15: return "5min"
    if expiry <= 30: return "15min"
    if expiry <= 90: return "30min"
    return "1h"

def minutes_to_next_period(scan_every: int) -> int:
    now = datetime.now(MSK)
    minutes = now.hour * 60 + now.minute
    return scan_every - (minutes % scan_every)

def should_scan_now(scan_every: int) -> bool:
    now = datetime.now(MSK)
    minutes = now.hour * 60 + now.minute
    return (minutes % scan_every) <= max(1, scan_every // 10)

def fetch_forex_data(from_sym: str, to_sym: str, expiry: int):
    if not use_request(): return None
    url = (
        "https://api.twelvedata.com/time_series"
        f"?symbol={from_sym}/{to_sym}&interval={get_interval(expiry)}&outputsize=50"
        f"&apikey={TWELVE_DATA_KEY}"
    )
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("status") == "error" or "values" not in data: return None
        return [{"time": v["datetime"], "open": float(v["open"]), "high": float(v["high"]),
                 "low": float(v["low"]), "close": float(v["close"])}
                for v in reversed(data["values"])]
    except Exception as e:
        log.error(f"Ошибка {from_sym}/{to_sym}: {e}"); return None

def ema(values, period):
    result, k = [], 2 / (period + 1)
    for i, v in enumerate(values):
        result.append(v if i == 0 else v * k + result[-1] * (1 - k))
    return result

def rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, period+1)]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, period+1)]
    ag, al = sum(gains)/period, sum(losses)/period
    return 100.0 if al == 0 else 100 - 100/(1 + ag/al)

def macd(closes):
    if len(closes) < 26: return 0.0, 0.0, 0.0
    e12 = ema(closes, 12); e26 = ema(closes, 26)
    ml = [a-b for a,b in zip(e12, e26)]; sig = ema(ml, 9)
    return ml[-1], sig[-1], ml[-1]-sig[-1]

def bollinger(closes, period=20):
    if len(closes) < period:
        c = closes[-1]; return c*1.001, c, c*0.999
    w = closes[-period:]; m = sum(w)/period
    s = (sum((x-m)**2 for x in w)/period)**0.5
    return m+2*s, m, m-2*s

def stochastic(candles, period=14):
    if len(candles) < period: return 50.0
    w = candles[-period:]
    hi, lo = max(c["high"] for c in w), min(c["low"] for c in w)
    cl = candles[-1]["close"]
    return 50.0 if hi == lo else (cl-lo)/(hi-lo)*100

def find_levels(candles, lookback=20):
    if len(candles) < lookback + 2: return [], []
    window = candles[-(lookback+2):-1]
    supports, resistances = [], []
    for i in range(1, len(window)-1):
        if window[i]["low"]  < window[i-1]["low"]  and window[i]["low"]  < window[i+1]["low"]:
            supports.append(window[i]["low"])
        if window[i]["high"] > window[i-1]["high"] and window[i]["high"] > window[i+1]["high"]:
            resistances.append(window[i]["high"])
    return supports, resistances

def near_level(price, levels, threshold_pct=0.001):
    return any(abs(price - lvl) / lvl < threshold_pct for lvl in levels)

def analyze_pair(pair, candles, min_score):
    if len(candles) < 35: return None
    closes = [c["close"] for c in candles]
    price = closes[-1]; vc, vp = 0, 0
    e9, e33 = ema(closes, 9), ema(closes, 33)
    if   e9[-1] > e33[-1] and e9[-2] <= e33[-2]: vc += 25
    elif e9[-1] < e33[-1] and e9[-2] >= e33[-2]: vp += 25
    elif e9[-1] > e33[-1]: vc += 10
    else: vp += 10
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
    _, _, hist = macd(closes); _, _, ph = macd(closes[:-1])
    if   hist > 0 and ph <= 0: vc += 20
    elif hist < 0 and ph >= 0: vp += 20
    elif hist > 0: vc += 8
    else: vp += 8
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
    return {"pair": pair, "direction": direction, "score": score, "stars": stars,
            "price": price, "at_level": at_support or at_resistance, "rsi": rv, "stoch": st}

def format_signal(sig, expiry, mode="manual"):
    now = datetime.now(MSK).strftime("%H:%M МСК")
    arrow = "🟢 CALL ▲" if sig["direction"] == "CALL" else "🔴 PUT ▼"
    tag = " · 🏆" if mode == "best30" else (" · ⚡" if mode == "best15" else "")
    return (f"*{sig['pair']}* | {arrow}{tag}\n"
            f"⏱ Экспирация: *{expiry} мин*\n"
            f"{sig['stars']} Оценка: *{sig['score']}%*\n"
            f"💰 `{sig['price']:.5f}` | {now}")

def format_result(rec, current_price, won):
    diff_p = (current_price - rec["price"]) / rec["price"] * 100
    icon = "✅ ЗАШЁЛ" if won else "❌ НЕ ЗАШЁЛ"
    return (f"{icon} | *{rec['pair']}*\n"
            f"Вход: `{rec['price']:.5f}` → `{current_price:.5f}` ({diff_p:+.3f}%)")

async def check_pending_results(bot, chat_id, pair, current_price):
    records = signal_history.get(chat_id, [])
    now = datetime.now(MSK)
    st = get_stats(chat_id)
    for rec in records:
        if rec.get("done") or rec["pair"] != pair: continue
        elapsed = (now - rec["time"]).total_seconds() / 60
        if elapsed < rec["expiry"]: continue
        diff_pct = (current_price - rec["price"]) / rec["price"] * 100
        if rec["direction"] == "CALL":
            won = diff_pct > 0.01; lost = diff_pct < -0.01
        else:
            won = diff_pct < -0.01; lost = diff_pct > 0.01
        if won or lost:
            rec["done"] = True
            if won: st["win"] += 1
            else:   st["loss"] += 1
            total = st["win"] + st["loss"]
            winrate = int(st["win"] / total * 100) if total > 0 else 0
            await bot.send_message(
                chat_id=chat_id,
                text=f"{format_result(rec, current_price, won)}\n📊 ✅{st['win']} ❌{st['loss']} | {winrate}%",
                reply_to_message_id=rec.get("message_id"),
                parse_mode="Markdown"
            )

async def do_scan(msg, settings, chat_id=None, bot=None, silent_if_empty=False):
    found = 0
    mode = settings.get("mode", "manual")
    best_hours = settings.get("_best_hours", None)
    for pair in settings["active_pairs"]:
        if requests_left() == 0:
            await msg.reply_text("⚠️ Лимит 800 запросов исчерпан. Продолжу завтра.")
            break
        if not is_best_hour(pair, best_hours):
            continue
        fs, ts = pair.split("/")
        candles = fetch_forex_data(fs, ts, settings["expiry"])
        if not candles:
            await asyncio.sleep(8); continue
        current_price = candles[-1]["close"]
        if chat_id and bot:
            await check_pending_results(bot, chat_id, pair, current_price)
        sig = analyze_pair(pair, candles, settings["min_score"])
        if sig:
            found += 1
            sent = await msg.reply_text(format_signal(sig, settings["expiry"], mode), parse_mode="Markdown")
            if chat_id:
                if chat_id not in signal_history: signal_history[chat_id] = []
                signal_history[chat_id].append({
                    "pair": pair, "direction": sig["direction"], "price": sig["price"],
                    "expiry": settings["expiry"], "time": datetime.now(MSK),
                    "done": False, "message_id": sent.message_id if sent else None,
                })
                signal_history[chat_id] = signal_history[chat_id][-30:]
        await asyncio.sleep(8)
    if found == 0 and not silent_if_empty:
        st = get_stats(chat_id) if chat_id else {"win":0,"loss":0}
        total = st["win"] + st["loss"]
        wr = int(st["win"]/total*100) if total > 0 else 0
        await msg.reply_text(
            f"🔕 Сигналов нет\n💾 Запросов: *{requests_left()}/800*\n📊 Счёт: ✅{st['win']} ❌{st['loss']} | {wr}%",
            parse_mode="Markdown")
    return found

async def auto_scan(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = ctx.job.chat_id
    settings = get_effective_settings(chat_id)
    if not is_trading_time(settings) or requests_left() == 0: return
    log.info(f"Авто-скан {chat_id} режим={settings.get('mode','manual')}")
    class FakeMsg:
        async def reply_text(self, text, **kwargs):
            return await ctx.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    await do_scan(FakeMsg(), settings, chat_id=chat_id, bot=ctx.bot)

MAIN_KEYBOARD = ReplyKeyboardMarkup([
    ["📡 Разовая проверка", "⚙️ Настройки"],
    ["▶️ Подписаться",      "⏹ Отписаться"],
    ["📊 Статус",           "❓ Помощь"],
], resize_keyboard=True)

def mode_info(s):
    mode = s.get("mode", "manual")
    if mode == "best30":
        return (f"🏆 *Лучший 30min*\n"
                f"Пары: {', '.join(BEST_30MIN.keys())}\n"
                f"Экспирация: 90 мин | Скор: 85%\n"
                f"Часы: фильтр по walk-forward")
    elif mode == "best15":
        return (f"⚡ *Лучший 15min*\n"
                f"Пары: {', '.join(BEST_15MIN.keys())}\n"
                f"Экспирация: 45 мин | Скор: 75%\n"
                f"Часы: фильтр по walk-forward")
    else:
        return (f"🔧 *Ручной*\n"
                f"Пары: {len(s['active_pairs'])} шт\n"
                f"Экспирация: {s['expiry']} мин | Скор: {s['min_score']}%\n"
                f"Часы: {s['hour_from']}:00–{s['hour_to']}:00 МСК")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    s = get_settings(update.effective_chat.id)
    es = get_effective_settings(update.effective_chat.id)
    now = datetime.now(MSK)
    wd = "⛔ Выходной" if now.weekday() >= 5 else "✅ Рабочий день"
    ih = "✅ В торговых часах" if is_trading_time(es) else "💤 Вне часов"
    st = get_stats(update.effective_chat.id)
    total = st["win"] + st["loss"]
    wr = int(st["win"]/total*100) if total > 0 else 0
    await update.message.reply_text(
        f"🤖 *Forex Signal Bot v5*\n\n{wd} | {ih}\n\nРежим: {mode_info(s)}\n\n"
        f"💾 Запросов: *{requests_left()}/800*\n📊 Счёт: ✅{st['win']} ❌{st['loss']} | {wr}%",
        parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id); es = get_effective_settings(chat_id)
    now = datetime.now(MSK)
    jobs = ctx.job_queue.get_jobs_by_name(str(chat_id))
    sub = "✅ Активна" if jobs else "❌ Не активна"
    wd = "⛔ Выходной" if now.weekday() >= 5 else "✅ Рабочий день"
    ih = "✅ Торговые часы" if is_trading_time(es) else "💤 Вне часов"
    nxt = minutes_to_next_period(es["scan_every"])
    st = get_stats(chat_id); total = st["win"] + st["loss"]
    wr = int(st["win"]/total*100) if total > 0 else 0
    open_s = len([r for r in signal_history.get(chat_id, []) if not r.get("done")])
    kb = [[InlineKeyboardButton("🔄 Сбросить счёт", callback_data="reset_stats")]]
    await (update.message or update.callback_query.message).reply_text(
        f"📊 *Статус*\n\n🖥 Сервер: *✅ Работает*\n📡 Подписка: *{sub}*\n"
        f"📅 {wd} | {ih}\n⏰ До скана: *~{nxt} мин*\n\n"
        f"Режим: {mode_info(s)}\n\n"
        f"💾 Запросов: *{requests_left()}/800*\n📋 Открытых сигналов: *{open_s}*\n"
        f"🏆 Счёт: ✅{st['win']} ❌{st['loss']} | *{wr}%*",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    msg = update.message or update.callback_query.message
    chat_id = update.effective_chat.id
    s = get_settings(chat_id); es = get_effective_settings(chat_id)
    for job in ctx.job_queue.get_jobs_by_name(str(chat_id)): job.schedule_removal()
    scan_every = es["scan_every"]
    if should_scan_now(scan_every):
        first_in, first_msg = 5, "Первый скан через ~5 сек"
    else:
        first_in = minutes_to_next_period(scan_every) * 60
        first_msg = f"Первый скан через ~{minutes_to_next_period(scan_every)} мин"
    ctx.job_queue.run_repeating(auto_scan, interval=scan_every*60, first=first_in,
                                chat_id=chat_id, name=str(chat_id))
    await msg.reply_text(
        f"✅ *Подписка активирована*\n\nРежим: {mode_info(s)}\n\n"
        f"🔄 Каждые *{scan_every} мин*\n⏰ {first_msg}",
        parse_mode="Markdown")

async def cmd_unsubscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    chat_id = update.effective_chat.id
    for job in ctx.job_queue.get_jobs_by_name(str(chat_id)): job.schedule_removal()
    await (update.message or update.callback_query.message).reply_text("❌ Автосигналы отключены.")

async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    chat_id = update.effective_chat.id; s = get_settings(chat_id)
    msg = update.message or update.callback_query.message
    mode = s.get("mode", "manual")
    m = {"manual": "⬜", "best30": "⬜", "best15": "⬜"}; m[mode] = "✅"
    kb = [[
        InlineKeyboardButton(f"{m['manual']} 🔧 Ручной",  callback_data="mode_manual"),
        InlineKeyboardButton(f"{m['best30']} 🏆 30min",   callback_data="mode_best30"),
        InlineKeyboardButton(f"{m['best15']} ⚡ 15min",    callback_data="mode_best15"),
    ]]
    if mode == "manual":
        kb += [
            [InlineKeyboardButton("⏱ Экспирация",          callback_data="set_expiry")],
            [InlineKeyboardButton("🔄 Период сканирования", callback_data="set_period")],
            [InlineKeyboardButton("🕐 Торговые часы",       callback_data="set_hours")],
            [InlineKeyboardButton("🎯 Минимальный скор",    callback_data="set_score")],
            [InlineKeyboardButton("📊 Выбор пар",           callback_data="set_pairs")],
        ]
    await msg.reply_text(
        f"⚙️ *Настройки*\n\nТекущий режим:\n{mode_info(s)}\n\n💾 Запросов: *{requests_left()}/800*",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await (update.message or update.callback_query.message).reply_text(
        "📖 *Forex Signal Bot v5*\n\n"
        "*Режимы:*\n"
        "🔧 Ручной — все пары, настройки вручную\n"
        "🏆 Лучший 30min — 4 пары, 90мин, скор 85%, часы авто\n"
        "⚡ Лучший 15min — 6 пар, 45мин, скор 75%, часы авто\n\n"
        "*Индикаторы:* EMA 9/33, S/R, RSI, MACD, Bollinger, Stochastic\n\n"
        "*Результат* проверяется через время экспирации\n"
        "*Счётчик* ✅/❌ ведётся автоматически\n\n"
        "*/start* /status /subscribe /unsubscribe /settings\n\n"
        "⚠️ _Не финансовый совет._",
        parse_mode="Markdown")

async def menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    text = update.message.text; chat_id = update.effective_chat.id
    if   text == "📊 Статус":       await cmd_status(update, ctx)
    elif text == "⚙️ Настройки":    await cmd_settings(update, ctx)
    elif text == "▶️ Подписаться":   await cmd_subscribe(update, ctx)
    elif text == "⏹ Отписаться":    await cmd_unsubscribe(update, ctx)
    elif text == "❓ Помощь":        await cmd_help(update, ctx)
    elif text == "📡 Разовая проверка":
        s = get_settings(chat_id); mode = s.get("mode", "manual")
        if mode in ("best30", "best15"):
            es = get_effective_settings(chat_id)
            await update.message.reply_text(
                f"🔍 *Разовая проверка* | {MODE_NAMES[mode]}\n"
                f"TF: {get_interval(es['expiry'])} | Скор: {es['min_score']}%\n"
                f"💾 Запросов: {requests_left()}",
                parse_mode="Markdown")
            await do_scan(update.message, es, chat_id=chat_id, bot=ctx.bot)
        else:
            kb = [[InlineKeyboardButton(f"{v} мин", callback_data=f"quick_{v}") for v in [5,10,15,30]],
                  [InlineKeyboardButton(f"{v} мин", callback_data=f"quick_{v}") for v in [45,60]]]
            await update.message.reply_text("📡 *Разовая проверка*\nВыбери экспирацию:",
                                            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_allowed(update): return
    chat_id = update.effective_chat.id; s = get_settings(chat_id); data = q.data

    if data == "reset_stats":
        stats[chat_id] = {"win": 0, "loss": 0}
        await q.message.reply_text("🔄 Счёт сброшен: ✅0 ❌0")
    elif data in ("subscribe","unsubscribe"):
        if data == "subscribe": await cmd_subscribe(update, ctx)
        else: await cmd_unsubscribe(update, ctx)
    elif data == "settings_menu": await cmd_settings(update, ctx)
    elif data == "help": await cmd_help(update, ctx)

    elif data.startswith("mode_"):
        mode = data[5:]
        s["mode"] = mode
        for job in ctx.job_queue.get_jobs_by_name(str(chat_id)): job.schedule_removal()
        await q.message.reply_text(
            f"✅ Режим: *{MODE_NAMES[mode]}*\n\n{mode_info(s)}\n\nПерезапусти: /subscribe",
            parse_mode="Markdown")

    elif data.startswith("quick_"):
        expiry = int(data.split("_")[1])
        temp = {**s, "expiry": expiry, "active_pairs": s["active_pairs"].copy(),
                "mode": "manual", "_best_hours": None}
        await q.message.reply_text(
            f"🔍 *Разовая проверка* | {expiry} мин | TF: {get_interval(expiry)}\n💾 Запросов: {requests_left()}",
            parse_mode="Markdown")
        await do_scan(q.message, temp, chat_id=chat_id, bot=ctx.bot)

    elif data == "set_expiry":
        kb = [[InlineKeyboardButton(f"{v} мин → {get_interval(v)}", callback_data=f"expiry_{v}")]
              for v in [5,10,15,30,60]]
        await q.message.reply_text("⏱ Выбери экспирацию:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("expiry_"):
        s["expiry"] = int(data.split("_")[1])
        await q.message.reply_text(f"✅ *{s['expiry']} мин* → TF: *{get_interval(s['expiry'])}*\nПерезапусти: /subscribe", parse_mode="Markdown")

    elif data == "set_period":
        kb = [[InlineKeyboardButton(f"{v} мин", callback_data=f"period_{v}") for v in [5,10,15,30]]]
        await q.message.reply_text("🔄 Как часто сканировать?", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("period_"):
        s["scan_every"] = int(data.split("_")[1])
        await q.message.reply_text(f"✅ Каждые *{s['scan_every']} мин*\nПерезапусти: /subscribe", parse_mode="Markdown")

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
        await q.message.reply_text(f"✅ *{s['hour_from']}:00–{s['hour_to']}:00 МСК*\nПерезапусти: /subscribe", parse_mode="Markdown")

    elif data == "set_score":
        kb = [[InlineKeyboardButton(f"{v}%", callback_data=f"score_{v}") for v in [70,75,80,85]]]
        await q.message.reply_text("🎯 Минимальный скор:\n_(выше = меньше, но лучше)_",
                                   parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("score_"):
        s["min_score"] = int(data.split("_")[1])
        await q.message.reply_text(f"✅ Скор: *{s['min_score']}%*", parse_mode="Markdown")

    elif data == "set_pairs":
        kb = [[InlineKeyboardButton(("✅" if p in s["active_pairs"] else "⬜") + f" {p}",
                                    callback_data=f"toggle_{p}")] for p in ALL_PAIRS]
        kb.append([InlineKeyboardButton("✅ Выбрать все", callback_data="pairs_all"),
                   InlineKeyboardButton("⬜ Убрать все",  callback_data="pairs_none")])
        kb.append([InlineKeyboardButton(f"💾 Готово ({len(s['active_pairs'])} пар)", callback_data="pairs_done")])
        await q.message.reply_text("📊 Выбери пары:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("toggle_"):
        pair = data[7:]
        if pair in s["active_pairs"]: s["active_pairs"].remove(pair)
        else: s["active_pairs"].append(pair)
        kb = [[InlineKeyboardButton(("✅" if p in s["active_pairs"] else "⬜") + f" {p}",
                                    callback_data=f"toggle_{p}")] for p in ALL_PAIRS]
        kb.append([InlineKeyboardButton("✅ Выбрать все", callback_data="pairs_all"),
                   InlineKeyboardButton("⬜ Убрать все",  callback_data="pairs_none")])
        kb.append([InlineKeyboardButton(f"💾 Готово ({len(s['active_pairs'])} пар)", callback_data="pairs_done")])
        await q.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(kb))

    elif data == "pairs_all":
        s["active_pairs"] = ALL_PAIRS.copy()
        kb = [[InlineKeyboardButton(f"✅ {p}", callback_data=f"toggle_{p}")] for p in ALL_PAIRS]
        kb.append([InlineKeyboardButton("✅ Выбрать все", callback_data="pairs_all"),
                   InlineKeyboardButton("⬜ Убрать все",  callback_data="pairs_none")])
        kb.append([InlineKeyboardButton(f"💾 Готово ({len(ALL_PAIRS)} пар)", callback_data="pairs_done")])
        await q.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(kb))

    elif data == "pairs_none":
        s["active_pairs"] = []
        kb = [[InlineKeyboardButton(f"⬜ {p}", callback_data=f"toggle_{p}")] for p in ALL_PAIRS]
        kb.append([InlineKeyboardButton("✅ Выбрать все", callback_data="pairs_all"),
                   InlineKeyboardButton("⬜ Убрать все",  callback_data="pairs_none")])
        kb.append([InlineKeyboardButton("💾 Готово (0 пар)", callback_data="pairs_done")])
        await q.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(kb))

    elif data == "pairs_done":
        await q.message.reply_text(
            f"✅ Активных пар: *{len(s['active_pairs'])}*\n{', '.join(sorted(s['active_pairs']))}",
            parse_mode="Markdown")

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
    log.info("🤖 Бот v5 запущен.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
