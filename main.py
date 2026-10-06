import asyncio
import base64
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, Request
import requests

load_dotenv()

app = FastAPI()

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

AI_MODEL = "gemini-3.1-flash-lite"
SYSTEM_PROMPT = (
    "Tum ek dostana WhatsApp assistant ho. User jis zubaan mein likhe "
    "(Roman Urdu, Urdu ya English), usi mein jawab do. Jawab chhota rakho "
    "(2-4 jumle), saada text mein, heading ya markdown ke baghair. "
    "Agar kisi baat ka pata na ho toh sach bol do."
)
FIXED_COMMANDS = ["help", "status", "about"]
MAX_MEDIA_BYTES = 10 * 1024 * 1024  # 10 MB

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")


# ---------- Database ----------
def init_db():
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


def save_message(phone, direction, text, wa_id=None):
    """Naya message save hua toh True, duplicate ho toh False."""
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
        print(f"DB error: {e}")
        return True


def get_history(phone, limit=10):
    """Is user ke last messages, Gemini ke format mein."""
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
        if history and history[-1]["role"] == role:
            history[-1]["parts"][0]["text"] += "\n" + text
        else:
            history.append({"role": role, "parts": [{"text": text}]})
    while history and history[0]["role"] != "user":
        history.pop(0)
    return history


init_db()


# ---------- WhatsApp media download ----------
def download_media(media_id):
    """(bytes, mime_type) wapas deta hai, fail ho toh (None, None)."""
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
        meta = requests.get(
            f"https://graph.facebook.com/v20.0/{media_id}",
            headers=headers,
            timeout=30,
        ).json()
        url = meta.get("url")
        if not url:
            print(f"Media error: {meta}")
            return None, None
        r = requests.get(url, headers=headers, timeout=60)
        if r.status_code != 200:
            print(f"Media download error: {r.status_code}")
            return None, None
        if len(r.content) > MAX_MEDIA_BYTES:
            print("Media bohat badi hai, skip.")
            return None, None
        return r.content, meta.get("mime_type")
    except Exception as e:
        print(f"Media error: {e}")
        return None, None


# ---------- AI (Gemini) ----------
def ask_ai(phone, media_id=None, caption=""):
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY set nahi hai")
        return None

    contents = get_history(phone)

    if media_id:
        data, mime = download_media(media_id)
        if not data or not mime:
            return None
        prompt = caption or "Is file mein kya hai? Mukhtasar bayan karo."
        parts = [
            {"inline_data": {
                "mime_type": mime,
                "data": base64.b64encode(data).decode(),
            }},
            {"text": prompt},
        ]
        if contents and contents[-1]["role"] == "user":
            contents[-1]["parts"] = parts
        else:
            contents.append({"role": "user", "parts": parts})

    if not contents or contents[-1]["role"] != "user":
        return None

    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{AI_MODEL}:generateContent"
        )
        r = requests.post(
            url,
            headers={
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": contents,
                "generationConfig": {"maxOutputTokens": 1000},
            },
            timeout=60,
        )
        if r.status_code != 200:
            print(f"AI error: {r.status_code} {r.text}")
            return None
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None
    except Exception as e:
        print(f"AI error: {e}")
        return None


# ---------- Webhook ----------
@app.get("/webhook")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            print(f"Webhook Verified! Challenge: {challenge}")
            return int(challenge)
    return {"error": "Verification failed"}


@app.post("/webhook")
async def receive_webhook(request: Request):
    body = await request.json()
    print("Webhook Data:", body)

    try:
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

                if msg_type == "text":
                    command = message["text"]["body"].strip().lower()
                elif msg_type == "interactive":
                    command = message["interactive"]["button_reply"]["id"]
                elif msg_type == "image":
                    media_id = message["image"]["id"]
                    caption = message["image"].get("caption", "").strip()
                    command = f"(image bheji) {caption}".strip()
                elif (
                    msg_type == "document"
                    and message["document"].get("mime_type") == "application/pdf"
                ):
                    media_id = message["document"]["id"]
                    caption = message["document"].get("caption", "").strip()
                    command = f"(PDF bheji) {caption}".strip()
                else:
                    send_whatsapp_message(
                        sender_phone,
                        "Abhi main text, image aur PDF samajh sakta hoon.",
                    )
                    continue

                if not save_message(sender_phone, "in", command, message.get("id")):
                    print("Duplicate message, skip.")
                    continue

                print(f"Message aya {sender_phone} se: {command}")

                if media_id:
                    ai_text = await asyncio.to_thread(
                        ask_ai, sender_phone, media_id, caption
                    )
                    send_whatsapp_message(
                        sender_phone,
                        ai_text
                        or "Maazrat, main yeh file nahi parh saka. "
                        "Dobara bhej kar dekhein.",
                    )
                elif command in ["hi", "hello", "salam", "menu"]:
                    send_buttons(sender_phone)
                elif command in FIXED_COMMANDS:
                    send_whatsapp_message(sender_phone, get_reply(command))
                else:
                    ai_text = await asyncio.to_thread(ask_ai, sender_phone)
                    send_whatsapp_message(
                        sender_phone,
                        ai_text
                        or "Maazrat, abhi main jawab nahi de saka. "
                        "Thori der baad try karein ya 'help' likhein.",
                    )

    except Exception as e:
        print(f"Error parsing message: {e}")

    return {"status": "ok"}


# ---------- Saved chats dekhne ke liye ----------
@app.get("/messages")
async def list_messages(key: str = ""):
    if not VERIFY_TOKEN or key != VERIFY_TOKEN:
        return {"error": "unauthorized"}
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT phone, direction, text, created_at FROM messages "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()
    return [
        {"phone": r[0], "direction": r[1], "text": r[2], "time": r[3]}
        for r in rows
    ]


# ---------- Bot logic ----------
def get_reply(command):
    if command == "help":
        return (
            "Aap yeh commands use kar sakte hain:\n"
            "1. hi / hello / menu\n2. help\n3. status\n4. about\n"
            "Koi bhi sawal seedha likh dein, ya image / PDF bhej dein, "
            "AI jawab dega."
        )
    if command == "status":
        return "Bot bilkul theek aur active halat mein kaam kar raha hai!"
    if command == "about":
        return "Main FastAPI par bana ek WhatsApp bot hoon, AI ke saath 🤖"
    return "Samajh nahi aaya. 'help' likhein."


# ---------- Sending ----------
def send_payload(payload):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    response = requests.post(url, json=payload, headers=headers)
    print("Reply Response:", response.json())


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
            "body": {
                "text": "Assalam-o-Alaikum! Main aapki kya madad kar sakta hoon? 👇"
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "status", "title": "Status"}},
                    {"type": "reply", "reply": {"id": "help", "title": "Help"}},
                    {"type": "reply", "reply": {"id": "about", "title": "About"}},
                ]
            },
        },
    })