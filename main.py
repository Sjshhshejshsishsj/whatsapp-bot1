import asyncio
import base64
import json
import logging
import os
import sys

sqlite3_imported = True
try:
    import sqlite3
    from contextlib import closing
except ImportError:
    sqlite3_imported = False

from datetime import datetime, timezone
from dotenv import load_dotenv
from fastapi import FastAPI, Request
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

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OWNER_PHONE = os.getenv("OWNER_PHONE")  # maslan 923001234567 (+ ke baghair)

# ---------- Brands aur Items (yahan apni asli cheezein likhein) ----------
# Qaide: har item ki "id" poore catalog mein alag ho (chhote harf aur _ mein),
# item/brand "title" 24 harf tak, ek brand mein 10 items tak, 10 brands tak.
# "price" sirf number ho (1500, na "1500" na 1,500).
CURRENCY = "Rs"
BRANDS = {
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

# Flat list (order flow isi se kaam karta hai)
PRODUCTS = {}
for _bid, _b in BRANDS.items():
    for _pid, _it in _b["items"].items():
        PRODUCTS[_pid] = {**_it, "brand": _b["title"], "brand_id": _bid}


def product_name(p):
    return f"{p['brand']} - {p['title']}"


CATALOG_TEXT = " | ".join(
    f"{b['title']}: " + ", ".join(
        f"{i['title']} ({CURRENCY} {i['price']})" for i in b["items"].values()
    )
    for b in BRANDS.values()
)
SESSION_TIMEOUT_MIN = 30

SYSTEM_PROMPT = (
    "Tum 'Apex Order Bot' ho, jo e-commerce aur orders manage karne wala professional WhatsApp assistant ho. "
    "User jis zubaan mein likhe (Roman Urdu, Urdu ya English), usi mein jawab do. "
    "Jawab chhota rakho (2-4 jumle), saada text mein, heading ya markdown ke baghair. "
    f"Hamare brands aur products: {CATALOG_TEXT}. Sirf inhi products aur qeematon ki baat karo, koi aur qeemat na banao. "
    "Order lene ke liye user ko 'catalog' likhne ko kaho."
)
FIXED_COMMANDS = ["help", "status", "about"]
GREETINGS = ["hi", "hello", "salam", "menu"]
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
                (wa_id, phone, direction, text,
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
                (phone, limit),
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
        with closing(sqlite3.connect(DB_PATH)) as conn:
            row = conn.execute(
                "SELECT product_id, step, qty, name, address, updated_at "
                "FROM order_sessions WHERE phone=?",
                (phone,),
            ).fetchone()
        if not row:
            return None
        s = dict(zip(
            ["product_id", "step", "qty", "name", "address", "updated_at"], row))
        age = datetime.now(timezone.utc) - datetime.fromisoformat(s["updated_at"])
        if age.total_seconds() > SESSION_TIMEOUT_MIN * 60:
            clear_session(phone)
            return None
        if s["product_id"] not in PRODUCTS:
            clear_session(phone)
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
                (phone, product_id, step, qty, name, address,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except Exception as e:
        logging.error(f"save_session error: {e}")


def clear_session(phone):
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.execute("DELETE FROM order_sessions WHERE phone=?", (phone,))
            conn.commit()
    except Exception as e:
        logging.error(f"clear_session error: {e}")


init_db()


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
    """Ek ke baad ek same role wale messages ko jod deta hai."""
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

    text, err = generate(contents, SYSTEM_PROMPT, 1000)
    if err:
        logging.error(f"AI error: {err}")
        return "Maazrat, abhi AI jawab nahi de saka. Thori der baad try karein."
    return text


# ---------- Order flow ----------
def start_order(phone, product_id):
    product = PRODUCTS[product_id]
    save_session(phone, product_id, "qty")
    send_whatsapp_message(
        phone,
        f"Aapne chuna: {product_name(product)} ({CURRENCY} {product['price']}).\n"
        "Kitni quantity chahiye? Number likhein (maslan 2).\n"
        "Order rokne ke liye 'cancel' likhein.",
    )


def handle_order_step(phone, raw_text, session):
    step = session["step"]
    pid = session["product_id"]

    if step == "qty":
        if raw_text.isdigit() and 1 <= int(raw_text) <= 99:
            save_session(phone, pid, "name", qty=int(raw_text))
            send_whatsapp_message(phone, "Shukriya! Ab apna naam likhein.")
        else:
            send_whatsapp_message(
                phone,
                "Meherbani karke quantity sirf number mein likhein (maslan 2). "
                "Order rokne ke liye 'cancel' likhein.",
            )

    elif step == "name":
        if len(raw_text) >= 2:
            save_session(phone, pid, "address", qty=session["qty"], name=raw_text[:60])
            send_whatsapp_message(phone, "Ab delivery ka mukammal pata likhein.")
        else:
            send_whatsapp_message(phone, "Meherbani karke apna naam sahi likhein.")

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
            send_confirm_buttons(phone, summary)
        else:
            send_whatsapp_message(phone, "Meherbani karke pata thoda mukammal likhein.")

    else:  # confirm step par user ne button ke bajaye text likha
        send_whatsapp_message(
            phone,
            "Order confirm ya cancel karne ke liye upar wale button dabayein, "
            "ya 'cancel' likhein.",
        )


def finalize_order(phone, session):
    product = PRODUCTS[session["product_id"]]
    qty = session["qty"]
    total = qty * product["price"]
    order_id = "AO-" + datetime.now().strftime("%m%d%H%M%S")
    pname = product_name(product)

    log_order_to_sheet([
        datetime.now(timezone.utc).isoformat(), phone, session["name"],
        pname, qty, total, session["address"], order_id,
    ])
    clear_session(phone)

    send_whatsapp_message(
        phone,
        f"Shukriya! Aapka order confirm ho gaya hai ✅\n"
        f"Order ID: {order_id}\n"
        f"{pname} x {qty} = {CURRENCY} {total}\n"
        "Hum jald aap se rabta karenge.",
    )

    if OWNER_PHONE and OWNER_PHONE != phone:
        send_whatsapp_message(
            OWNER_PHONE,
            f"🆕 Naya order {order_id}\n"
            f"Customer: {session['name']} ({phone})\n"
            f"{pname} x {qty} = {CURRENCY} {total}\n"
            f"Pata: {session['address']}",
        )


# ---------- Webhook ----------
@app.get("/webhook")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token and mode == "subscribe" and token == VERIFY_TOKEN:
        return int(challenge)
    return {"error": "Verification failed"}


@app.post("/webhook")
async def receive_webhook(request: Request):
    try:
        body = await request.json()
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
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

                # --- SMART ROUTING ---
                if media_id:
                    ai_text = await asyncio.to_thread(ask_ai, sender_phone, media_id, caption)
                    send_whatsapp_message(sender_phone, ai_text)
                elif command == "order_confirm":
                    if session and session["step"] == "confirm":
                        finalize_order(sender_phone, session)
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
                    handle_order_step(sender_phone, raw_text, session)
                elif command in FIXED_COMMANDS:
                    send_whatsapp_message(sender_phone, get_reply(command))
                elif command.startswith("brand_") and command[6:] in BRANDS:
                    send_item_list(sender_phone, command[6:])
                elif command in PRODUCTS:
                    start_order(sender_phone, command)
                elif any(word in command for word in ["order", "kharidna", "catalog", "products", "shop"]):
                    send_catalog(sender_phone)
                else:
                    ai_text = await asyncio.to_thread(ask_ai, sender_phone)
                    send_whatsapp_message(sender_phone, ai_text)

    except Exception as e:
        logging.error(f"Error parsing webhook message: {e}")

    return {"status": "ok"}


# ---------- Bot logic ----------
def get_reply(command):
    if command == "help":
        return (
            "Aap yeh commands use kar sakte hain:\n"
            "1. hi / hello / menu\n2. help\n3. status\n4. about\n"
            "5. catalog / order (Brands dekhne aur order karne ke liye)\n"
            "6. cancel (order rokne ke liye)\n"
            "Koi bhi sawal seedha likh dein, ya image / PDF bhej dein, AI jawab dega."
        )
    if command == "status":
        return "Bot bilkul theek aur active halat mein kaam kar raha hai!"
    if command == "about":
        return "Main FastAPI par bana ek WhatsApp bot hoon, AI ke saath 🤖"
    return "Samajh nahi aaya. 'help' likhein."


# ---------- Sending ----------
def send_payload(payload):
    try:
        url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        if r.status_code != 200:
            logging.error(f"WhatsApp send error {r.status_code}: {r.text}")
    except Exception as e:
        logging.error(f"Error sending WhatsApp payload: {e}")


def send_whatsapp_message(recipient_phone, message_text):
    save_message(recipient_phone, "out", message_text)
    send_payload({
        "messaging_product": "whatsapp",
        "to": recipient_phone,
        "type": "text",
        "text": {"body": message_text},
    })


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
    """Ek hi brand ho toh seedha items, warna pehle brands ki list."""
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
