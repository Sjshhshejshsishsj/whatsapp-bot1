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

AI_MODEL = "gemini-1.5-flash-latest"
SYSTEM_PROMPT = (
    "Tum 'Apex Order Bot' ho, jo e-commerce aur orders manage karne wala professional WhatsApp assistant ho. "
    "User jis zubaan mein likhe (Roman Urdu, Urdu ya English), usi mein jawab do. "
    "Jawab chhota rakho (2-4 jumle), saada text mein, heading ya markdown ke baghair. "
    "User ko products dekhne, catalog open karne aur orders place karne mein madad karo."
)
FIXED_COMMANDS = ["help", "status", "about"]
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

def log_order_to_sheet(user_phone, order_details):
    try:
        if sheet:
            sheet.append_row([user_phone, order_details, datetime.now(timezone.utc).isoformat()])
            logging.info(f"Order logged to sheet for {user_phone}")
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

    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{AI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        )
        
        payload = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": 1000},
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]}
        }

        r = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        
        if r.status_code != 200:
            # Yeh line ab seedha terminal aur log dono par exact error print karegi
            error_msg = f"AI API error code {r.status_code}: {r.text}"
            logging.error(error_msg)
            return f"Maazrat, AI error ({r.status_code})."

        res_json = r.json()
        candidates = res_json.get("candidates", [])
        if not candidates:
            return "Maazrat, AI ne koi jawab nahi diya."
            
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or "Maazrat, main samajh nahi saka."
    except Exception as e:
        logging.error(f"AI exception: {e}")
        return "Maazrat, AI process karte waqt masla aa gaya."


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

                if msg_type == "text":
                    command = message["text"]["body"].strip().lower()
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

                # --- SMART ROUTING ---
                if media_id:
                    ai_text = await asyncio.to_thread(ask_ai, sender_phone, media_id, caption)
                    send_whatsapp_message(sender_phone, ai_text)
                elif command in ["hi", "hello", "salam", "menu"]:
                    send_buttons(sender_phone)
                elif command in FIXED_COMMANDS:
                    send_whatsapp_message(sender_phone, get_reply(command))
                elif command in ["item_1", "item_2"]:
                    order_text = f"Selected Product ID: {command}"
                    log_order_to_sheet(sender_phone, order_text)
                    send_whatsapp_message(
                        sender_phone,
                        f"Shukriya! Aapka order ({command}) record kar liya gaya hai. Hum jald rabta karenge."
                    )
                elif any(word in command for word in ["order", "kharidna", "catalog", "products", "shop"]):
                    send_product_list(sender_phone)
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
            "5. catalog / order (Products dekhne ke liye)\n"
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
        requests.post(url, json=payload, headers=headers, timeout=30)
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


def send_product_list(recipient_phone):
    try:
        save_message(recipient_phone, "out", "[product catalog menu]")
        send_payload({
            "messaging_product": "whatsapp",
            "to": recipient_phone,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "header": {"type": "text", "text": "🛍️ Product Catalog"},
                "body": {"text": "Neeche diye gaye button par click karke hamari items dekhein aur order select karein:"},
                "footer": {"text": "Powered by Apex Order Bot"},
                "action": {
                    "button": "Catalog Dekhein",
                    "sections": [
                        {
                            "title": "Available Items",
                            "rows": [
                                {"id": "item_1", "title": "Item 1 - Special Deal", "description": "Best price and high quality."},
                                {"id": "item_2", "title": "Item 2 - Standard Pack", "description": "Perfect for daily use."}
                            ]
                        }
                    ]
                }
            }
        })
    except Exception as e:
        logging.error(f"Error sending product list: {e}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
