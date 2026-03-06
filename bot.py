#!/usr/bin/env python3
"""
Forex Signal Bot for Telegram
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

BOT_TOKEN       = "8612612451:AAE7dMyGwf1Ddigz23Ygeop5ubh1nkrm6M8"
TWELVE_DATA_KEY = "55dae6924d864941b1ab27052b0871ef"

DEFAULT_SETTINGS = {
    "expiry":      15,
    "scan_every":  5,
    "hour_from":   10,
    "hour_to":     23,
    "min_score":   75,
    "active_pairs": [
        "EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD",
        "EUR/GBP", "GBP/JPY", "EUR/JPY", "USD/CHF",
        "AUD/JPY", "EUR/CHF",
    ]
}

ALL_PAIRS = [
    "AUD/CHF", "CHF/JPY", "AUD/CAD", "EUR/CAD", "EUR/GBP",
    "GBP/USD", "USD/CHF", "CAD/JPY", "GBP/CHF", "GBP/JPY",
    "AUD/JPY", "EUR/USD", "EUR/AUD", "AUD/USD", "GBP/CAD",
    "USD/JPY", "EUR/CHF", "USD/CAD", "CAD/CHF", "GBP/AUD", "EUR/JPY"
]

MSK = ZoneInfo("Europe/Moscow")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
user_settings: dict = {}

def get_settings(chat_id: int) -> dict:
    if chat_id not in user_settings:
        user_settings[chat_id] = DEFAULT_SETTINGS.copy()
        user_settings[chat_id]["active_pairs"] = DEFAULT_SETTINGS["active_pairs"].copy()
    return user_settings[chat_id]

def is_trading_time(settings: dict) -> bool:
    now = datetime.now(MSK)
    if now.weekday() >= 5:
        return False
    return settings["hour_from"] <= now.hour < settings["hour_to"]

def get_interval(expiry: int) -> str:
    if expiry <= 5: return "5min"
    elif expiry <= 15: return "15min"
    else: return "30min"

def fetch_forex_data(from_sym, to_sym, expiry):
    symbol = f"{from_sym}/{to_sym}"
    url = (
        "https://api.twelvedata.com/time_series"
        f"?symbol={symbol}&interval={get_interval(expiry)}&outputsize=30"
        f"&apikey={TWELVE_DATA_KEY}"
    )
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("status") == "error" or "values" not in data:
            log.warning(f"Нет данных {symbol}: {data.get('message','')}")
            return None
        candles = []
        for v in reversed(data["values"]):
            candles.append({
                "time": v["datetime"],
                "open": float(v["open"]), "high": float(v["high"]),
                "low":  float(v["low"]),  "close": float(v["close"]),
            })
        return candles
    except Exception as e:
        log.error(f"Ошибка {symbol}: {e}")
        return None

def ema(values, period):
    result, k = [], 2 / (period + 1)
    for i, v in enumerate(values):
        result.append(v if i == 0 else v * k + result[-1] * (1 - k))
    return result

def rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains = [max(closes[i]-closes[i-1], 0) for i in range(1, period+1)]
    losses= [max(closes[i-1]-closes[i], 0) for i in range(1, period+1)]
    ag, al = sum(gains)/period, sum(losses)/period
    return 100.0 if al == 0 else 100 - 100/(1 + ag/al)

def macd(closes):
    if len(closes) < 26: return 0.0, 0.0, 0.0
    e12 = ema(closes, 12); e26 = ema(closes, 26)
    ml = [a - b for a, b in zip(e12, e26)]
    sig = ema(ml, 9)
    return ml[-1], sig[-1], ml[-1] - sig[-1]

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
    return 50.0 if hi == lo else (cl - lo)/(hi - lo)*100

def analyze_pair(pair, candles, min_score):
    closes = [c["close"] for c in candles]
    vc, vp, details = 0, 0, []

    e7, e21 = ema(closes, 7), ema(closes, 21)
    if   e7[-1] > e21[-1] and e7[-2] <= e21[-2]: vc += 25; details.append("📈 EMA кросс вверх")
    elif e7[-1] < e21[-1] and e7[-2] >= e21[-2]: vp += 25; details.append("📉 EMA кросс вниз")
    elif e7[-1] > e21[-1]:                        vc += 10; details.append("📈 EMA тренд вверх")
    else:                                          vp += 10; details.append("📉 EMA тренд вниз")

    rv = rsi(closes)
    if   rv < 30: vc += 25; details.append(f"💚 RSI перепродан ({rv:.1f})")
    elif rv > 70: vp += 25; details.append(f"🔴 RSI перекуплен ({rv:.1f})")
    elif rv < 45: vc += 10; details.append(f"↗️ RSI бычий ({rv:.1f})")
    elif rv > 55: vp += 10; details.append(f"↘️ RSI медвежий ({rv:.1f})")

    _, _, hist = macd(closes)
    _, _, ph   = macd(closes[:-1])
    if   hist > 0 and ph <= 0: vc += 20; details.append("📊 MACD пересёк вверх")
    elif hist < 0 and ph >= 0: vp += 20; details.append("📊 MACD пересёк вниз")
    elif hist > 0:             vc += 8;  details.append("📊 MACD положительный")
    else:                      vp += 8;  details.append("📊 MACD отрицательный")

    upper, _, lower = bollinger(closes)
    price = closes[-1]
    if   price <= lower: vc += 20; details.append("🎯 Цена у нижней полосы Боллинджера")
    elif price >= upper: vp += 20; details.append("🎯 Цена у верхней полосы Боллинджера")

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
            "stars": stars, "details": details, "price": price, "rsi": rv}

def format_signal(sig, expiry):
    now   = datetime.now(MSK).strftime("%H:%M МСК")
    arrow = "🟢 CALL ▲" if sig["direction"] == "CALL" else "🔴 PUT  ▼"
    dtext = "\n".join(f"  • {d}" for d in sig["details"])
    return (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 *СИГНАЛ* | {now}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💱 Пара:          *{sig['pair']}*\n"
        f"📌 Направление:  *{arrow}*\n"
        f"⏱ Экспирация:   *{expiry} минут*\n"
        f"📊 Уверенность:  {sig['stars']}\n"
        f"🎯 Скор:          *{sig['score']}%*\n"
        f"💰 Цена входа:   `{sig['price']:.5f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Основания:*\n{dtext}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _Торгуй ответственно. Не финансовый совет._"
    )

async def do_scan(msg, settings, silent_if_empty=False):
    found = 0
    for pair in settings["active_pairs"]:
        fs, ts = pair.split("/")
        candles = fetch_forex_data(fs, ts, settings["expiry"])
        if not candles or len(candles) < 26:
            await asyncio.sleep(8); continue
        sig = analyze_pair(pair, candles, settings["min_score"])
        if sig:
            found += 1
            await msg.reply_text(format_signal(sig, settings["expiry"]), parse_mode="Markdown")
        await asyncio.sleep(8)
    if found == 0 and not silent_if_empty:
        await msg.reply_text("🔕 Сейчас нет сигналов нужного качества.\nПопробуй позже или измени скор в /settings")
    return found

async def auto_scan(ctx: ContextTypes.DEFAULT_TYPE):
    chat_id  = ctx.job.chat_id
    settings = get_settings(chat_id)
    if not is_trading_time(settings):
        log.info(f"Вне торгового времени для {chat_id}")
        return
    log.info(f"Авто-скан для {chat_id}")
    found = 0
    for pair in settings["active_pairs"]:
        fs, ts = pair.split("/")
        candles = fetch_forex_data(fs, ts, settings["expiry"])
        if not candles or len(candles) < 26:
            await asyncio.sleep(8); continue
        sig = analyze_pair(pair, candles, settings["min_score"])
        if sig:
            found += 1
            await ctx.bot.send_message(chat_id=chat_id,
                text=format_signal(sig, settings["expiry"]), parse_mode="Markdown")
        await asyncio.sleep(8)
    log.info(f"Авто-скан {chat_id}: {found} сигналов")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_settings(update.effective_chat.id)
    kb = [
        [InlineKeyboardButton("📡 Сканировать сейчас", callback_data="scan")],
        [InlineKeyboardButton("▶️ Подписаться на сигналы", callback_data="subscribe")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings_menu")],
        [InlineKeyboardButton("📖 Помощь", callback_data="help")],
    ]
    await update.message.reply_text(
        f"🤖 *Forex Signal Bot*\n\n"
        f"⏱ Экспирация: *{s['expiry']} мин* | 🔄 Скан каждые *{s['scan_every']} мин*\n"
        f"🕐 Торговые часы: *{s['hour_from']}:00–{s['hour_to']}:00 МСК*\n"
        f"🎯 Мин. скор: *{s['min_score']}%* | 📊 Пар: *{len(s['active_pairs'])}*\n\n"
        f"Выбери действие:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb),
    )

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    s   = get_settings(update.effective_chat.id)
    await msg.reply_text(f"🔍 Сканирую {len(s['active_pairs'])} пар...")
    await do_scan(msg, s)

async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        f"🕐 Только *{s['hour_from']}:00–{s['hour_to']}:00 МСК*\n"
        f"⏱ Экспирация *{s['expiry']} мин*\n"
        f"📊 Пар: *{len(s['active_pairs'])}*\n\n/unsubscribe — отключить",
        parse_mode="Markdown",
    )

async def cmd_unsubscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    for job in ctx.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()
    await (update.message or update.callback_query.message).reply_text("❌ Автосигналы отключены.")

async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        f"⚙️ *Текущие настройки*\n\n"
        f"⏱ Экспирация: *{s['expiry']} мин*\n"
        f"🔄 Скан каждые: *{s['scan_every']} мин*\n"
        f"🕐 Торговые часы: *{s['hour_from']}:00–{s['hour_to']}:00 МСК*\n"
        f"🎯 Мин. скор: *{s['min_score']}%*\n"
        f"📊 Активных пар: *{len(s['active_pairs'])}* из {len(ALL_PAIRS)}",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb),
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.callback_query.message
    await msg.reply_text(
        "📖 *Как работает бот*\n\n"
        "5 индикаторов: EMA, RSI, MACD, Bollinger, Stochastic\n"
        "Сигнал — только если скор выше минимального\n\n"
        "*/start* — главное меню\n"
        "*/scan* — разовое сканирование\n"
        "*/subscribe* — автосигналы\n"
        "*/unsubscribe* — отключить\n"
        "*/settings* — настройки\n\n"
        "🕐 *Лучшее время:*\n"
        "10:00–13:00 и 15:00–19:00 МСК\n\n"
        "⚠️ _Не финансовый совет._",
        parse_mode="Markdown",
    )

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query
    await q.answer()
    chat_id = update.effective_chat.id
    s       = get_settings(chat_id)
    data    = q.data

    if data == "scan":             await cmd_scan(update, ctx)
    elif data == "subscribe":      await cmd_subscribe(update, ctx)
    elif data == "settings_menu":  await cmd_settings(update, ctx)
    elif data == "help":           await cmd_help(update, ctx)

    elif data == "set_expiry":
        kb = [[InlineKeyboardButton(f"{v} мин", callback_data=f"expiry_{v}") for v in [5,10,15,30]]]
        await q.message.reply_text("⏱ Выбери экспирацию:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("expiry_"):
        s["expiry"] = int(data.split("_")[1])
        await q.message.reply_text(f"✅ Экспирация: *{s['expiry']} мин*", parse_mode="Markdown")

    elif data == "set_period":
        kb = [[InlineKeyboardButton(f"{v} мин", callback_data=f"period_{v}") for v in [5,10,15,30]]]
        await q.message.reply_text("🔄 Как часто сканировать?", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("period_"):
        s["scan_every"] = int(data.split("_")[1])
        await q.message.reply_text(
            f"✅ Сканирование каждые *{s['scan_every']} мин*\nПерезапусти: /subscribe",
            parse_mode="Markdown")

    elif data == "set_hours":
        kb = [
            [InlineKeyboardButton("10:00–23:00", callback_data="hours_10_23"),
             InlineKeyboardButton("08:00–22:00", callback_data="hours_8_22")],
            [InlineKeyboardButton("10:00–20:00", callback_data="hours_10_20"),
             InlineKeyboardButton("09:00–23:00", callback_data="hours_9_23")],
        ]
        await q.message.reply_text("🕐 Торговые часы (МСК):", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("hours_"):
        parts = data.split("_")
        s["hour_from"], s["hour_to"] = int(parts[1]), int(parts[2])
        await q.message.reply_text(
            f"✅ *{s['hour_from']}:00–{s['hour_to']}:00 МСК*", parse_mode="Markdown")

    elif data == "set_score":
        kb = [[InlineKeyboardButton(f"{v}%", callback_data=f"score_{v}") for v in [70,75,80,85]]]
        await q.message.reply_text(
            "🎯 Минимальный скор:\n_(выше = меньше, но лучше сигналов)_",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("score_"):
        s["min_score"] = int(data.split("_")[1])
        await q.message.reply_text(f"✅ Мин. скор: *{s['min_score']}%*", parse_mode="Markdown")

    elif data == "set_pairs":
        kb = []
        for pair in ALL_PAIRS:
            icon = "✅" if pair in s["active_pairs"] else "⬜"
            kb.append([InlineKeyboardButton(f"{icon} {pair}", callback_data=f"toggle_{pair}")])
        kb.append([InlineKeyboardButton("💾 Готово", callback_data="pairs_done")])
        await q.message.reply_text("📊 Выбери пары:", reply_markup=InlineKeyboardMarkup(kb))

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
            f"✅ Активных пар: *{len(s['active_pairs'])}*\n{', '.join(s['active_pairs'])}",
            parse_mode="Markdown")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("scan",        cmd_scan))
    app.add_handler(CommandHandler("subscribe",   cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("settings",    cmd_settings))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CallbackQueryHandler(button_handler))
    log.info("🤖 Бот запущен.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
