import asyncio
import base64
import json
import logging
import os
import re
import sys

sqlite3_imported = True
try:
    import sqlite3
    from contextlib import closing
except ImportError:
    sqlite3_imported = False

from datetime import datetime, timezone
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import requests
import gspread
from google.oauth2.service_account import Credentials

from gemini_client import generate

load_dotenv()

# ---------- Logging Setup (Console + File) ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

app = FastAPI()


def normalize_phone(raw):
    """Number ko sirf ankon mein badalta hai. Pakistani 03XXXXXXXXX ko 923XXXXXXXXX banata hai."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = "92" + digits[1:]
    return digits


WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OWNER_PHONE = normalize_phone(os.getenv("OWNER_PHONE"))  # maslan 923001234567

# Instagram aur Messenger dono ke liye Facebook PAGE ka access token (WhatsApp wala nahi)
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN", WHATSAPP_TOKEN)

# Debugging: poora webhook payload log karta hai. Sab theek hone par Railway mein 0 kar dein.
DEBUG_WEBHOOK = os.getenv("DEBUG_WEBHOOK", "1") == "1"

if not os.getenv("PAGE_ACCESS_TOKEN"):
    logging.warning(
        "PAGE_ACCESS_TOKEN set nahi hai: Instagram/Messenger replies fail hongi."
    )
logging.info(
    f"Owner notifications: {'ON (' + OWNER_PHONE[:4] + '***)' if OWNER_PHONE else 'OFF (OWNER_PHONE set nahi)'}"
)

# WhatsApp error codes ke hal (logs mein dikhane ke liye)
WA_ERROR_HINTS = {
    131047: "-> 24 ghante ka qaida: us number se pehle bot ko WhatsApp par koi message bhejwayein.",
    131030: "-> number test recipient list mein add/verify nahi (Meta > WhatsApp > API Setup).",
    131026: "-> message deliver nahi ho saka (number WhatsApp par nahi ya block).",
    190: "-> WHATSAPP_TOKEN galat ya expire.",
    100: "-> number ya parameter ka format galat.",
}

# ---------- Brands aur Items ----------
# Google Sheet mein "Products" tab ho toh catalog wahin se aata hai
# (columns: Brand | Item | Price | Description | Active).
# Tab na ho ya masla ho toh neeche DEFAULT_BRANDS (test products) chalte hain.
CURRENCY = "Rs"
DEFAULT_BRANDS = {
    "gul_ahmed": {
        "title": "Gul Ahmed",
        "desc": "Lawn collection",
        "items": {
            "lawn_01": {"title": "Summer Printed Suit", "price": 3500, "desc": "3-piece unstitched lawn"},
            "lawn_02": {"title": "Chiffon Dupatta Suit", "price": 5500, "desc": "Fancy embroidered suit"},
        },
    },
    "lucky_garments": {
        "title": "Lucky Garments",
        "desc": "Roz marra ke kapde",
        "items": {
            "lg_1": {"title": "Item 1 (asli naam)", "price": 1000,
                     "desc": "Yahan asli tafseel likhein"},
            "lg_2": {"title": "Item 2 (asli naam)", "price": 1800,
                     "desc": "Yahan asli tafseel likhein"},
        },
    },
}
CATALOG_CACHE_SECONDS = 60
SESSION_TIMEOUT_MIN = 30

BRANDS = {}
PRODUCTS = {}
PRODUCT_ORDER = []  # numbered catalog isi tarteeb mein
CATALOG_TEXT = ""
_catalog_state = {"loaded_at": 0.0, "source": None, "count": -1}


def product_name(p):
    return f"{p['brand']} - {p['title']}"


def _apply_catalog(brands, source):
    """Naye objects banata hai aur ek saath lagata hai (chalta hua code mehfooz rehta hai)."""
    global BRANDS, PRODUCTS, PRODUCT_ORDER, CATALOG_TEXT
    products = {}
    for bid, b in brands.items():
        for pid, it in b["items"].items():
            products[pid] = {**it, "brand": b["title"], "brand_id": bid}
    catalog_text = " | ".join(
        f"{b['title']}: " + ", ".join(
            f"{i['title']} ({CURRENCY} {i['price']})" for i in b["items"].values()
        )
        for b in brands.values()
    )
    PRODUCTS = products
    PRODUCT_ORDER = list(products.keys())
    CATALOG_TEXT = catalog_text
    BRANDS = brands

    if _catalog_state["source"] != source or _catalog_state["count"] != len(products):
        logging.info(
            f"Catalog {source} se load hua: {len(brands)} brands, {len(products)} items")
        if len(brands) > 10 or any(len(b["items"]) > 10 for b in brands.values()):
            logging.warning("WhatsApp list mein sirf pehle 10 brands/items dikhte hain.")
    _catalog_state["source"] = source
    _catalog_state["count"] = len(products)


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_") or "x"


def _parse_price(v):
    s = re.sub(r"[^\d.]", "", str(v))
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f == int(f) else f


def _load_catalog_from_sheet():
    """'Products' tab padhta hai. Tab na ho toh None."""
    if not sheet:
        return None
    try:
        ws = sheet.spreadsheet.worksheet("Products")
    except Exception:
        return None  # Products tab nahi hai

    values = ws.get_all_values()
    if len(values) < 2:
        return None
    headers = [str(h).strip().lower() for h in values[0]]

    brands, seen = {}, set()
    for i, r in enumerate(values[1:], start=2):
        row = {headers[j]: r[j] for j in range(min(len(headers), len(r))) if headers[j]}
        brand = str(row.get("brand", "")).strip()
        item = str(row.get("item") or row.get("items") or "").strip()
        if not brand and not item:
            continue
        if str(row.get("active", "yes")).strip().lower() in (
                "no", "n", "false", "0", "off", "inactive"):
            continue
        price = _parse_price(row.get("price"))
        if not brand or not item or price is None:
            logging.warning(f"Products sheet ki row {i} skip (brand/item/price adhoora)")
            continue
        bid = _slug(brand)
        pid = f"{bid}__{_slug(item)}"[:60]
        base, n = pid, 2
        while pid in seen:
            pid = f"{base}_{n}"
            n += 1
        seen.add(pid)
        b = brands.setdefault(bid, {"title": brand, "desc": "", "items": {}})
        b["items"][pid] = {
            "title": item,
            "price": price,
            "desc": str(row.get("description") or row.get("desc") or "").strip() or "-",
        }
    return brands or None


def refresh_catalog(force=False):
    now = datetime.now(timezone.utc).timestamp()
    if not force and now - _catalog_state["loaded_at"] < CATALOG_CACHE_SECONDS:
        return
    _catalog_state["loaded_at"] = now
    try:
        brands = _load_catalog_from_sheet()
    except Exception as e:
        logging.error(f"Products sheet padhne mein masla (pichla catalog chalta rahega): {e}")
        return
    if brands:
        _apply_catalog(brands, "sheet")


def system_prompt():
    return (
        "Tum 'Apex Order Bot' ho, jo e-commerce aur orders manage karne wala professional omnichannel assistant ho. "
        "User jis zubaan mein likhe (Roman Urdu, Urdu ya English), usi mein jawab do. "
        "Jawab chhota rakho (2-4 jumle), saada text mein, heading ya markdown ke baghair. "
        f"Hamare brands aur products: {CATALOG_TEXT}. Sirf inhi products aur qeematein ki baat karo, koi aur qeemat na banao. "
        "Agar user aam sawal puche toh uska seedha aur acha jawab do. "
        "Lekin bar bar ya har message ke aakhir mein 'catalog likhein' likhne ki zaroorat nahi hai, sirf tab kaho jab user shopping ya order ki baat kare."
    )


_apply_catalog(DEFAULT_BRANDS, "default")

FIXED_COMMANDS = ["help", "status", "about"]
GREETINGS = ["hi", "hello", "salam", "menu"]
CATALOG_WORDS = ["order", "kharidna", "catalog", "products", "shop"]
CONFIRM_WORDS = {"confirm", "yes", "haan", "han", "ha", "ok", "okay"}
MAX_MEDIA_BYTES = 10 * 1024 * 1024  # 10 MB

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")

# ---------- Google Sheets Setup ----------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
CREDS_FILE = "credentials.json"
SHEET_NAME = "Apex Orders"

sheet = None
try:
    creds = None
    google_creds_env = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if google_creds_env:
        creds_dict = json.loads(google_creds_env)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    elif os.path.exists(CREDS_FILE):
        creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)

    if creds:
        client = gspread.authorize(creds)
        sheet = client.open(SHEET_NAME).sheet1
        logging.info("Google Sheets connected successfully!")
except Exception as e:
    logging.error(f"Google Sheets connection error: {e}")

# Server start hote hi catalog sheet se load karein
refresh_catalog(force=True)


def log_order_to_sheet(row):
    """row = [time, phone, name, product, qty, total, address, order_id]"""
    try:
        if sheet:
            sheet.append_row(row)
            logging.info(f"Order logged to sheet: {row[-1]}")
        else:
            logging.error("Sheet connected nahi, order sheet mein save nahi hua.")
    except Exception as e:
        logging.error(f"Error saving to sheet: {e}")


# ---------- Database ----------
def init_db():
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wa_id TEXT UNIQUE,
                    phone TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    text TEXT,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS order_sessions (
                    phone TEXT PRIMARY KEY,
                    product_id TEXT,
                    step TEXT,
                    qty INTEGER,
                    name TEXT,
                    address TEXT,
                    updated_at TEXT
                )"""
            )
            conn.commit()
    except Exception as e:
        logging.error(f"Database initialization error: {e}")


def save_message(phone, direction, text, wa_id=None):
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO messages "
                "(wa_id, phone, direction, text, created_at) VALUES (?,?,?,?,?)",
                (wa_id, str(phone), direction, text,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            return cur.rowcount == 1
    except Exception as e:
        logging.error(f"DB error in save_message: {e}")
        return True


def get_history(phone, limit=6):
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            rows = conn.execute(
                "SELECT direction, text FROM messages WHERE phone=? "
                "ORDER BY id DESC LIMIT ?",
                (str(phone), limit),
            ).fetchall()
        rows.reverse()

        history = []
        for direction, text in rows:
            if not text or text.startswith("["):
                continue
            role = "user" if direction == "in" else "model"
            history.append({"role": role, "parts": [{"text": text}]})

        while history and history[0]["role"] != "user":
            history.pop(0)

        return history
    except Exception as e:
        logging.error(f"Error fetching history for {phone}: {e}")
        return []


# ---------- Order sessions ----------
def get_session(phone):
    try:
        phone_str = str(phone)
        with closing(sqlite3.connect(DB_PATH)) as conn:
            row = conn.execute(
                "SELECT product_id, step, qty, name, address, updated_at "
                "FROM order_sessions WHERE phone=?",
                (phone_str,),
            ).fetchone()
        if not row:
            return None
        s = dict(zip(
            ["product_id", "step", "qty", "name", "address", "updated_at"], row))
        age = datetime.now(timezone.utc) - datetime.fromisoformat(s["updated_at"])
        if age.total_seconds() > SESSION_TIMEOUT_MIN * 60:
            clear_session(phone_str)
            return None
        if s["product_id"] not in PRODUCTS:
            clear_session(phone_str)
            return None
        return s
    except Exception as e:
        logging.error(f"get_session error: {e}")
        return None


def save_session(phone, product_id, step, qty=None, name=None, address=None):
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO order_sessions "
                "(phone, product_id, step, qty, name, address, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (str(phone), product_id, step, qty, name, address,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except Exception as e:
        logging.error(f"save_session error: {e}")


def clear_session(phone):
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.execute("DELETE FROM order_sessions WHERE phone=?", (str(phone),))
            conn.commit()
    except Exception as e:
        logging.error(f"clear_session error: {e}")


init_db()


# ---------- Instagram / Messenger ke liye text catalog ----------
CATALOG_HEADER = "🛍️ Hamare Products"


def build_social_catalog():
    lines = [CATALOG_HEADER, ""]
    n = 1
    for b in BRANDS.values():
        lines.append(b["title"])
        for it in b["items"].values():
            lines.append(f"{n}. {it['title']} - {CURRENCY} {it['price']}")
            n += 1
        lines.append("")
    lines.append("Order ke liye item ka number ya naam likhein (maslan 1).")
    return "\n".join(lines)


def last_out_is_catalog(phone):
    """Kya bot ka aakhri message catalog tha? (tab number se item chunna theek hai)"""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            row = conn.execute(
                "SELECT text FROM messages WHERE phone=? AND direction='out' "
                "ORDER BY id DESC LIMIT 1",
                (str(phone),),
            ).fetchone()
        return bool(row and row[0] and row[0].startswith(CATALOG_HEADER))
    except Exception:
        return False


def find_product(text, allow_number=False):
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in PRODUCTS:
        return t
    if t.isdigit():
        if allow_number and 1 <= int(t) <= len(PRODUCT_ORDER):
            return PRODUCT_ORDER[int(t) - 1]
        return None
    matches = []
    for pid, p in PRODUCTS.items():
        title = p["title"].lower()
        if t == title or title in t or (len(t) >= 5 and t in title):
            matches.append(pid)
    return matches[0] if len(matches) == 1 else None


def split_text(text, max_bytes):
    """Lambay message ko bytes ke hisaab se hisson mein todta hai."""
    chunks, cur, cur_bytes = [], "", 0
    for ch in text:
        b = len(ch.encode("utf-8"))
        if cur_bytes + b > max_bytes:
            chunks.append(cur)
            cur, cur_bytes = "", 0
        cur += ch
        cur_bytes += b
    if cur:
        chunks.append(cur)
    return chunks or [""]


# ---------- WhatsApp media download ----------
def download_media(media_id):
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
        meta = requests.get(
            f"https://graph.facebook.com/v20.0/{media_id}",
            headers=headers,
            timeout=30,
        ).json()
        url = meta.get("url")
        if not url:
            return None, None
        r = requests.get(url, headers=headers, timeout=60)
        if r.status_code != 200 or len(r.content) > MAX_MEDIA_BYTES:
            return None, None
        return r.content, meta.get("mime_type")
    except Exception as e:
        logging.error(f"Media download exception: {e}")
        return None, None


# ---------- AI (Gemini) ----------
def merge_turns(contents):
    merged = []
    for turn in contents:
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["parts"].extend(turn["parts"])
        else:
            merged.append({"role": turn["role"], "parts": list(turn["parts"])})
    return merged


def ask_ai(phone, media_id=None, caption=""):
    if not GEMINI_API_KEY:
        logging.error("GEMINI_API_KEY is not set.")
        return "Maazrat, AI key configure nahi hai."

    contents = get_history(phone)

    if media_id:
        data, mime = download_media(media_id)
        if not data or not mime:
            return "Maazrat, file download nahi ho saki."
        prompt = caption or "Is file mein kya hai? Mukhtasar bayan karo."
        contents.append({
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}},
                {"text": prompt}
            ]
        })

    if not contents or contents[-1]["role"] != "user":
        contents.append({"role": "user", "parts": [{"text": "Hello"}]})

    contents = merge_turns(contents)

    text, err = generate(contents, system_prompt(), 1000)
    if err:
        logging.error(f"AI error: {err}")
        return "Maazrat, abhi AI jawab nahi de saka. Thori der baad try karein."
    return text


# ---------- Order flow (Platform agnostic dispatcher) ----------
def send_reply_to_user(phone, platform, text_msg):
    if platform == "whatsapp":
        send_whatsapp_message(phone, text_msg)
    elif platform == "instagram":
        send_instagram_message(phone, text_msg)
    elif platform == "messenger":
        send_messenger_message(phone, text_msg)


def start_order(phone, product_id, platform="whatsapp"):
    product = PRODUCTS[product_id]
    save_session(phone, product_id, "qty")
    msg = (
        f"Aapne chuna: {product_name(product)} ({CURRENCY} {product['price']}).\n"
        "Kitni quantity chahiye? \n"
        "Order rokne ke liye 'cancel' likhein."
    )
    send_reply_to_user(phone, platform, msg)


def handle_order_step(phone, raw_text, session, platform="whatsapp"):
    step = session["step"]
    pid = session["product_id"]

    if step == "qty":
        if raw_text.isdigit() and 1 <= int(raw_text) <= 99:
            save_session(phone, pid, "name", qty=int(raw_text))
            send_reply_to_user(phone, platform, "Shukriya! Ab apna naam likhein.")
        else:
            send_reply_to_user(
                phone,
                platform,
                "Meherbani karke quantity sirf number mein likhein. "
                "Order rokne ke liye 'cancel' likhein.",
            )

    elif step == "name":
        if len(raw_text) >= 2:
            save_session(phone, pid, "address", qty=session["qty"], name=raw_text[:60])
            send_reply_to_user(phone, platform, "Ab delivery ka mukammal pata likhein.")
        else:
            send_reply_to_user(phone, platform, "Meherbani karke apna naam sahi likhein.")

    elif step == "address":
        if len(raw_text) >= 8:
            address = raw_text[:200]
            save_session(phone, pid, "confirm", qty=session["qty"],
                         name=session["name"], address=address)
            product = PRODUCTS[pid]
            total = session["qty"] * product["price"]
            summary = (
                "Aapka order:\n"
                f"{product_name(product)} x {session['qty']}\n"
                f"Total: {CURRENCY} {total}\n"
                f"Naam: {session['name']}\n"
                f"Pata: {address}\n\n"
                "Kya order confirm karein?"
            )
            if platform == "whatsapp":
                send_confirm_buttons(phone, summary)
            else:
                send_reply_to_user(
                    phone, platform,
                    summary + " 'Confirm' ya 'Cancel' likhein.")
        else:
            send_reply_to_user(phone, platform, "Meherbani karke pata thoda mukammal likhein.")

    else:  # confirm step
        low = raw_text.lower().strip()
        if low in CONFIRM_WORDS or "confirm" in low:
            finalize_order(phone, session, platform)
        else:
            send_reply_to_user(
                phone,
                platform,
                "Order confirm karne ke liye 'Confirm' ya cancel karne ke liye 'Cancel' likhein.",
            )


def finalize_order(phone, session, platform="whatsapp"):
    product = PRODUCTS[session["product_id"]]
    qty = session["qty"]
    total = qty * product["price"]
    order_id = "AO-" + datetime.now().strftime("%m%d%H%M%S")
    pname = product_name(product)

    log_order_to_sheet([
        datetime.now(timezone.utc).isoformat(), str(phone), session["name"],
        pname, qty, total, session["address"], order_id,
    ])
    clear_session(phone)

    send_reply_to_user(
        phone,
        platform,
        f"Shukriya! Aapka order confirm ho gaya hai ✅\n"
        f"Order ID: {order_id}\n"
        f"{pname} x {qty} = {CURRENCY} {total}\n"
        f"Hum jald aap se rabta karenge.",
    )

    # ---- Owner ko notification ----
    if not OWNER_PHONE:
        logging.warning(
            f"Order {order_id}: OWNER_PHONE set nahi hai, owner notification skip.")
    elif str(OWNER_PHONE) == str(phone):
        logging.info(
            f"Order {order_id}: customer aur owner ka number same hai, owner notification skip.")
    else:
        ok, _ = send_whatsapp_message(
            OWNER_PHONE,
            f"🆕 Naya order {order_id} ({platform.upper()})\n"
            f"Customer: {session['name']} ({phone})\n"
            f"{pname} x {qty} = {CURRENCY} {total}\n"
            f"Pata: {session['address']}",
        )
        logging.info(
            f"Order {order_id}: owner notification "
            f"{'Meta ne qabool ki' if ok else 'FAIL hui'} ({OWNER_PHONE[:4]}***)")


# ---------- Instagram / Messenger text handler ----------
async def handle_social_text(sender_id, text, platform):
    command = text.lower().strip()
    session = get_session(sender_id)

    if command in ["cancel", "order_cancel"]:
        clear_session(sender_id)
        send_reply_to_user(
            sender_id, platform,
            "Order cancel kar diya gaya. Catalog dekhne ke liye 'catalog' likhein.")
    elif command in GREETINGS:
        clear_session(sender_id)
        send_reply_to_user(
            sender_id, platform,
            "Assalam-o-Alaikum! 👋 Main Apex Order Bot hoon.\n"
            "Products dekhne ke liye 'catalog' likhein, madad ke liye 'help' likhein, "
            "ya koi bhi sawal pooch lein.")
    elif session:
        handle_order_step(sender_id, text, session, platform)
    elif command in FIXED_COMMANDS:
        send_reply_to_user(sender_id, platform, get_reply(command, platform))
    elif any(word in command for word in CATALOG_WORDS):
        send_reply_to_user(sender_id, platform, build_social_catalog())
    else:
        pid = find_product(text, allow_number=last_out_is_catalog(sender_id))
        if pid:
            start_order(sender_id, pid, platform)
        else:
            ai_text = await asyncio.to_thread(ask_ai, sender_id)
            send_reply_to_user(sender_id, platform, ai_text)


# ---------- Webhook Endpoints ----------
@app.get("/webhook")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token and mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(challenge, status_code=200)
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request):
    try:
        body = await request.json()
        object_type = body.get("object")
        await asyncio.to_thread(refresh_catalog)  # Sheet se taaza catalog (har 60 second mein)
        if DEBUG_WEBHOOK:
            logging.info(f"WEBHOOK object={object_type}: {json.dumps(body)[:1500]}")

        # 1. WhatsApp Handler
        if object_type == "whatsapp_business_account":
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})

                    # Delivery status: nakam messages hamesha log karein
                    for st in value.get("statuses", []):
                        if st.get("status") == "failed":
                            errs = st.get("errors") or []
                            code = errs[0].get("code") if errs else None
                            logging.error(
                                f"WhatsApp DELIVERY FAILED to {st.get('recipient_id')}: "
                                f"code={code} {json.dumps(errs)[:300]} "
                                f"{WA_ERROR_HINTS.get(code, '')}")

                    if "messages" not in value:
                        continue

                    message = value["messages"][0]
                    sender_phone = message["from"]
                    msg_type = message.get("type")

                    media_id = None
                    caption = ""
                    command = ""
                    raw_text = ""

                    if msg_type == "text":
                        raw_text = message["text"]["body"].strip()
                        command = raw_text.lower()
                    elif msg_type == "interactive":
                        interactive_data = message.get("interactive", {})
                        if "button_reply" in interactive_data:
                            command = interactive_data["button_reply"]["id"]
                        elif "list_reply" in interactive_data:
                            command = interactive_data["list_reply"]["id"]
                        else:
                            command = "unknown"
                    elif msg_type == "image":
                        media_id = message["image"]["id"]
                        caption = message["image"].get("caption", "").strip()
                        command = f"(image bheji) {caption}".strip()
                    elif msg_type == "document" and message["document"].get("mime_type") == "application/pdf":
                        media_id = message["document"]["id"]
                        caption = message["document"].get("caption", "").strip()
                        command = f"(PDF bheji) {caption}".strip()
                    else:
                        send_whatsapp_message(sender_phone, "Abhi main text, image aur PDF samajh sakta hoon.")
                        continue

                    if not save_message(sender_phone, "in", command, message.get("id")):
                        continue

                    session = get_session(sender_phone)

                    if media_id:
                        ai_text = await asyncio.to_thread(ask_ai, sender_phone, media_id, caption)
                        send_whatsapp_message(sender_phone, ai_text)
                    elif command == "order_confirm":
                        if session and session["step"] == "confirm":
                            finalize_order(sender_phone, session, "whatsapp")
                        else:
                            send_whatsapp_message(
                                sender_phone,
                                "Koi active order nahi hai. Naya order shuru karne ke liye 'catalog' likhein.")
                    elif command in ["order_cancel", "cancel"]:
                        clear_session(sender_phone)
                        send_whatsapp_message(
                            sender_phone, "Order cancel kar diya gaya. Dobara shuru karne ke liye 'catalog' likhein.")
                    elif command in GREETINGS:
                        clear_session(sender_phone)
                        send_buttons(sender_phone)
                    elif session and msg_type == "text":
                        handle_order_step(sender_phone, raw_text, session, "whatsapp")
                    elif command in FIXED_COMMANDS:
                        send_whatsapp_message(sender_phone, get_reply(command))
                    elif command.startswith("brand_") and command[6:] in BRANDS:
                        send_item_list(sender_phone, command[6:])
                    elif command in PRODUCTS:
                        start_order(sender_phone, command, "whatsapp")
                    elif any(word in command for word in CATALOG_WORDS):
                        send_catalog(sender_phone)
                    else:
                        ai_text = await asyncio.to_thread(ask_ai, sender_phone)
                        send_whatsapp_message(sender_phone, ai_text)

        # 2. Instagram & 3. Messenger Handler
        elif object_type in ["instagram", "page"]:
            platform = "instagram" if object_type == "instagram" else "messenger"
            for entry in body.get("entry", []):
                for event in entry.get("messaging", []):
                    try:
                        msg = event.get("message")
                        sender_id = (event.get("sender") or {}).get("id")
                        if not msg or not sender_id:
                            continue  # read/delivery/postback wagera abhi ignore
                        # Bot ke apne bheje messages (echo) ignore karein
                        if msg.get("is_echo") or str(sender_id) == str(entry.get("id")):
                            continue

                        text = (msg.get("text") or "").strip()
                        if not text:
                            send_reply_to_user(
                                sender_id, platform,
                                "Abhi main sirf text messages samajh sakta hoon.")
                            continue

                        # Duplicate delivery se bachne ke liye message id se dedupe
                        if not save_message(sender_id, "in", text, msg.get("mid")):
                            continue

                        logging.info(f"{platform} message from {sender_id}: {text[:80]}")
                        await handle_social_text(str(sender_id), text, platform)
                    except Exception as e:
                        logging.error(f"Error handling {platform} event: {e}")

    except Exception as e:
        logging.error(f"Error parsing webhook message: {e}")

    return {"status": "ok"}


# ---------- Bot logic ----------
def get_reply(command, platform="whatsapp"):
    if command == "help":
        if platform == "whatsapp":
            return (
                "Aap yeh commands use kar sakte hain:\n"
                "1. hi / hello / menu\n2. help\n3. status\n4. about\n"
                "5. catalog / order (Brands dekhne aur order karne ke liye)\n"
                "6. cancel (order rokne ke liye)\n"
                "Koi bhi sawal seedha likh dein, ya image / PDF bhej dein, AI jawab dega."
            )
        return (
            "Aap yeh likh sakte hain:\n"
            "1. hi / menu\n2. help\n3. status\n4. about\n"
            "5. catalog (products dekhne aur order karne ke liye)\n"
            "6. cancel (order rokne ke liye)\n"
            "Koi bhi sawal seedha likh dein, AI jawab dega."
        )
    if command == "status":
        return "Bot bilkul theek aur active halat mein kaam kar raha hai!"
    if command == "about":
        return "Main FastAPI par bana ek omnichannel bot hoon, AI ke saath 🤖"
    return "Samajh nahi aaya. 'help' likhein."


# ---------- Sending Functions (WhatsApp, Instagram, Messenger) ----------
def send_payload(payload):
    """WhatsApp ko payload bhejta hai. (ok, data) wapas deta hai."""
    try:
        url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:300]}

        if r.status_code == 200:
            logging.info(f"WhatsApp send OK to {str(payload.get('to'))[:4]}***")
            return True, data

        err = data.get("error", {}) if isinstance(data, dict) else {}
        code = err.get("code")
        logging.error(
            f"WhatsApp send error {r.status_code} (code {code}) to "
            f"{str(payload.get('to'))[:4]}***: {r.text[:400]} {WA_ERROR_HINTS.get(code, '')}")
        return False, data
    except Exception as e:
        logging.error(f"Error sending WhatsApp payload: {e}")
        return False, {}


def send_whatsapp_message(recipient_phone, message_text):
    save_message(recipient_phone, "out", message_text)
    return send_payload({
        "messaging_product": "whatsapp",
        "to": recipient_phone,
        "type": "text",
        "text": {"body": message_text},
    })


def _send_social(platform, recipient_id, message_text):
    """Instagram aur Messenger dono Facebook Page ke token se jate hain."""
    save_message(recipient_id, "out", message_text)

    url = "https://graph.facebook.com/v20.0/me/messages"
    limit = 900 if platform == "instagram" else 1900  # IG: 1000 bytes, Messenger: 2000 chars
    headers = {
        "Authorization": f"Bearer {PAGE_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    for part in split_text(message_text, limit):
        payload = {"recipient": {"id": recipient_id}, "message": {"text": part}}
        if platform == "messenger":
            payload["messaging_type"] = "RESPONSE"
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=30)
            if r.status_code == 200:
                logging.info(f"{platform} send OK to {recipient_id}")
            else:
                logging.error(f"{platform} send error {r.status_code}: {r.text}")
        except Exception as e:
            logging.error(f"Error sending {platform} message: {e}")


def send_instagram_message(recipient_id, message_text):
    _send_social("instagram", recipient_id, message_text)


def send_messenger_message(recipient_id, message_text):
    _send_social("messenger", recipient_id, message_text)


def send_buttons(recipient_phone):
    save_message(recipient_phone, "out", "[buttons menu]")
    send_payload({
        "messaging_product": "whatsapp",
        "to": recipient_phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Assalam-o-Alaikum! Main aapki kya madad kar sakta hoon? 👇"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "status", "title": "Status"}},
                    {"type": "reply", "reply": {"id": "help", "title": "Help"}},
                    {"type": "reply", "reply": {"id": "about", "title": "About"}},
                ]
            },
        },
    })


def send_confirm_buttons(recipient_phone, summary_text):
    save_message(recipient_phone, "out", "[order summary]")
    send_payload({
        "messaging_product": "whatsapp",
        "to": recipient_phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": summary_text[:1000]},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "order_confirm", "title": "Confirm"}},
                    {"type": "reply", "reply": {"id": "order_cancel", "title": "Cancel"}},
                ]
            },
        },
    })


def send_catalog(recipient_phone):
    if len(BRANDS) == 1:
        send_item_list(recipient_phone, next(iter(BRANDS)))
    else:
        send_brand_list(recipient_phone)


def send_brand_list(recipient_phone):
    try:
        save_message(recipient_phone, "out", "[brand list]")
        rows = [
            {
                "id": f"brand_{bid}",
                "title": b["title"][:24],
                "description": (b.get("desc") or f"{len(b['items'])} items")[:72],
            }
            for bid, b in BRANDS.items()
        ][:10]
        send_payload({
            "messaging_product": "whatsapp",
            "to": recipient_phone,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "header": {"type": "text", "text": "🛍️ Hamare Brands"},
                "body": {"text": "Pehle brand chunein, phir uski items dekhein:"},
                "footer": {"text": "Powered by Apex Order Bot"},
                "action": {
                    "button": "Brands Dekhein",
                    "sections": [{"title": "Brands", "rows": rows}],
                },
            },
        })
    except Exception as e:
        logging.error(f"Error sending brand list: {e}")


def send_item_list(recipient_phone, brand_id):
    try:
        brand = BRANDS[brand_id]
        if not brand["items"]:
            send_whatsapp_message(recipient_phone, "Is brand ki items abhi available nahi hain.")
            return
        save_message(recipient_phone, "out", "[item list]")
        rows = [
            {
                "id": pid,
                "title": p["title"][:24],
                "description": f"{CURRENCY} {p['price']} - {p['desc']}"[:72],
            }
            for pid, p in brand["items"].items()
        ][:10]
        send_payload({
            "messaging_product": "whatsapp",
            "to": recipient_phone,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "header": {"type": "text", "text": f"🛍️ {brand['title']}"[:60]},
                "body": {"text": "Item chunein, phir quantity, naam aur pata poochha jayega:"},
                "footer": {"text": "Doosra brand dekhne ke liye 'catalog' likhein"},
                "action": {
                    "button": "Items Dekhein",
                    "sections": [{"title": "Available Items"[:24], "rows": rows}],
                },
            },
        })
    except Exception as e:
        logging.error(f"Error sending item list: {e}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
