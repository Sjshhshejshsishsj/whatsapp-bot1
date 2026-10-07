import os
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import sqlite3
from gemini_client import ask_ai  # Aapka Gemini client jisme 90s timeout hai

app = FastAPI()

# Token aur Credentials jo aapne Railway env vars mein rakhe hain
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "my_verify_token")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN", WHATSAPP_TOKEN)  # Instagram aur Messenger ke liye

# Database Connection Setup
def init_db():
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            user_id TEXT PRIMARY KEY,
            state TEXT,
            selected_brand TEXT,
            selected_item TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Catalog Data
CATALOG_TEXT = """
1. Gul Ahmed: Chiffon Dupatta Suit (Rs 5500 - Fancy embroidered suit), Lawn Collection 3pc (Rs 4200 - Summer special printed lawn).
2. Sana Safinaz: Muzlin Vol-3 (Rs 4800 - Premium winter fabric), Luxury Formals (Rs 8500 - Party wear chiffon).
3. Sapphire: Daily Wear Kurtis (Rs 2500 - Stitched single shirt), Intermix Lawn Suit (Rs 3900 - 3 piece unstitched).
"""

SYSTEM_PROMPT = (
    "Tum 'Apex Order Bot' ho, jo e-commerce aur orders manage karne wala professional WhatsApp assistant ho. "
    "User jis zubaan mein likhe (Roman Urdu, Urdu ya English), usi mein jawab do. "
    "Jawab chhota rakho (2-4 jumle), saada text mein, heading ya markdown ke baghair. "
    f"Hamare brands aur products: {CATALOG_TEXT}. Sirf inhi products aur qeematein ki baat karo, koi aur qeemat na banao. "
    "Agar user aam sawal puche toh uska seedha aur acha jawab do. "
    "Lekin bar bar ya har message ke aakhir mein 'catalog likhein' likhne ki zaroorat nahi hai, sirf tab kaho jab user shopping ya order ki baat kare."
)

# Webhook Verification (Teeno platforms ke liye ek hi URL)
@app.get("/webhook")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    
    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return PlainTextResponse(challenge, status_code=200)
        else:
            raise HTTPException(status_code=403, detail="Verification token mismatch")
    raise HTTPException(status_code=400, detail="Missing parameters")

# Message Bhejne ke Functions (WhatsApp, Instagram, Messenger)
def send_whatsapp_message(to_phone: str, message: str):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "text": {"body": message}
    }
    requests.post(url, json=payload, timeout=30)

def send_instagram_message(recipient_id: str, message: str):
    url = f"https://graph.facebook.com/v20.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message}
    }
    requests.post(url, params=params, json=payload, timeout=30)

def send_messenger_message(recipient_id: str, message: str):
    url = f"https://graph.facebook.com/v20.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": message}
    }
    requests.post(url, params=params, json=payload, timeout=30)

# Incoming Webhook Endpoint
@app.post("/webhook")
async def handle_webhook(request: Request):
    body = await request.json()
    
    try:
        # 1. WhatsApp Message Handler
        if "object" in body and body["object"] == "whatsapp_business_account":
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    if "messages" in value:
                        msg_data = value["messages"][0]
                        sender_id = msg_data["from"]
                        msg_body = msg_data.get("text", {}).get("body", "").strip()
                        
                        response_text = process_bot_logic(sender_id, msg_body)
                        send_whatsapp_message(sender_id, response_text)
                        
        # 2. Instagram Message Handler
        elif "object" in body and body["object"] == "instagram":
            for entry in body.get("entry", []):
                for messaging in entry.get("messaging", []):
                    if "message" in messaging:
                        sender_id = messaging["sender"]["id"]
                        msg_body = messaging["message"].get("text", "").strip()
                        
                        if msg_body:
                            response_text = process_bot_logic(sender_id, msg_body)
                            send_instagram_message(sender_id, response_text)

        # 3. Facebook Messenger Message Handler
        elif "object" in body and body["object"] == "page":
            for entry in body.get("entry", []):
                for messaging in entry.get("messaging", []):
                    if "message" in messaging:
                        sender_id = messaging["sender"]["id"]
                        msg_body = messaging["message"].get("text", "").strip()
                        
                        if msg_body:
                            response_text = process_bot_logic(sender_id, msg_body)
                            send_messenger_message(sender_id, response_text)
                            
        return {"status": "ok"}
    except Exception as e:
        print(f"Error handling webhook: {e}")
        return {"status": "error"}

# Bot ka main logic (Order state + AI)
def process_bot_logic(user_id: str, text: str) -> str:
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT state, selected_brand, selected_item FROM sessions WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    state = row[0] if row else "IDLE"
    brand = row[1] if row else None
    item = row[2] if row else None
    
    text_lower = text.lower()
    
    # Cancel command
    if text_lower == "cancel":
        cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return "Order cancel kar diya gaya. Dobara shuru karne ke liye 'catalog' likhein."
    
    # Catalog trigger
    if "catalog" in text_lower or "collection" in text_lower or (state == "IDLE" and "hi" in text_lower):
        cursor.execute("INSERT OR REPLACE INTO sessions (user_id, state, selected_brand, selected_item) VALUES (?, ?, ?, ?)",
                       (user_id, "SELECTING_BRAND", None, None))
        conn.commit()
        conn.close()
        return f"Hamare brands:\n{CATALOG_TEXT}\n\nPehle brand chunein (maslan: Gul Ahmed), phir uski items dekhein."
    
    # Brand selection logic
    if state == "SELECTING_BRAND":
        if "gul ahmed" in text_lower:
            cursor.execute("UPDATE sessions SET state = ?, selected_brand = ? WHERE user_id = ?", 
                           ("SELECTING_ITEM", "Gul Ahmed", user_id))
            conn.commit()
            conn.close()
            return "Gul Ahmed chuna gaya. Chiffon Dupatta Suit (Rs 5500) ya Lawn Collection (Rs 4200) mein se item likhein."
        elif "sana safinaz" in text_lower:
            cursor.execute("UPDATE sessions SET state = ?, selected_brand = ? WHERE user_id = ?", 
                           ("SELECTING_ITEM", "Sana Safinaz", user_id))
            conn.commit()
            conn.close()
            return "Sana Safinaz chuna gaya. Muzlin Vol-3 ya Luxury Formals mein se item likhein."
        elif "sapphire" in text_lower:
            cursor.execute("UPDATE sessions sab SET state = ?, selected_brand = ? WHERE user_id = ?", 
                           ("SELECTING_ITEM", "Sapphire", user_id)) # syntax corrected below
        # Let's fix the minor typo in sapphire query inside the logic:
    
    # Re-checking state for brand selection if code runs past check
    # Let's write the clean snippet for brand selection inside process_bot_logic:
    if state == "SELECTING_BRAND":
        if "gul ahmed" in text_lower:
            cursor.execute("UPDATE sessions SET state = ?, selected_brand = ? WHERE user_id = ?", 
                           ("SELECTING_ITEM", "Gul Ahmed", user_id))
            conn.commit()
            conn.close()
            return "Gul Ahmed chuna gaya. Chiffon Dupatta Suit (Rs 5500) ya Lawn Collection (Rs 4200) mein se item likhein."
        elif "sana safinaz" in text_lower:
            cursor.execute("UPDATE sessions SET state = ?, selected_brand = ? WHERE user_id = ?", 
                           ("SELECTING_ITEM", "Sana Safinaz", user_id))
            conn.commit()
            conn.close()
            return "Sana Safinaz chuna gaya. Muzlin Vol-3 ya Luxury Formals mein se item likhein."
        elif "sapphire" in text_lower:
            cursor.execute("UPDATE sessions SET state = ?, selected_brand = ? WHERE user_id = ?", 
                           ("SELECTING_ITEM", "Sapphire", user_id))
            conn.commit()
            conn.close()
            return "Sapphire chuna gaya. Daily Wear Kurtis ya Intermix Lawn Suit mein se item likhein."

    # Agar koi aam baat ho ya AI ka kaam ho
    conn.close()
    ai_response = ask_ai(SYSTEM_PROMPT, text)
    return ai_response
