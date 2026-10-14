import html
import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

PROVIDER = html.escape(os.getenv("LEGAL_PROVIDER_NAME", "Apex Digital"))
EMAIL = html.escape(os.getenv("LEGAL_CONTACT_EMAIL", "apexdigital260@gmail.com"))
CLIENT = html.escape(os.getenv("LEGAL_BUSINESS_NAME", "").strip())  # client ka business naam (optional)
SERVICE = "Apex Order Bot"
UPDATED = "14 October 2026"
ON_BEHALF = f" on behalf of <strong>{CLIENT}</strong>" if CLIENT else ""


def _page(title, body):
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{title} - {SERVICE}</title>"
        "<style>body{font-family:Arial,Helvetica,sans-serif;max-width:760px;"
        "margin:0 auto;padding:24px 16px;line-height:1.6;color:#222}"
        "h1{font-size:1.6rem}h2{font-size:1.15rem;margin-top:1.6em}"
        "a{color:#0b57d0}.muted{color:#666;font-size:.9rem}</style></head><body>"
        f"<h1>{title}</h1><p class='muted'>Last updated: {UPDATED}</p>"
        f"{body}<hr><p class='muted'>{SERVICE} is operated by {PROVIDER}. "
        f"Contact: <a href='mailto:{EMAIL}'>{EMAIL}</a></p></body></html>"
    )


@router.get("/privacy")
async def privacy():
    return _page("Privacy Policy", f"""
<p>{SERVICE} is a chat assistant operated by {PROVIDER} ("we", "us"). It lets businesses
receive and answer customer messages and take orders on WhatsApp, Instagram and Facebook
Messenger{ON_BEHALF}. This policy explains what data the service handles and how.</p>

<h2>1. Information we handle</h2>
<ul>
<li>Your identifier on the messaging platform: your WhatsApp phone number, or your Instagram /
Messenger user ID.</li>
<li>The messages you send us (text, and images or PDF files you choose to send) and the
replies we send back.</li>
<li>Order details you give us: your name, delivery address, chosen items, quantity and the
order total.</li>
</ul>

<h2>2. How we use it</h2>
<ul>
<li>To reply to your messages and answer your questions.</li>
<li>To take, confirm and record your order.</li>
<li>To notify the business so it can prepare and deliver your order.</li>
<li>To keep the service running, fix errors and prevent abuse.</li>
</ul>
<p>We do not sell your data and we do not use it for advertising.</p>

<h2>3. Automated (AI) replies</h2>
<p>Replies are generated automatically. The text of your messages, and any image or PDF you
send, is processed by a third-party AI service (Google Gemini) to produce the reply. AI
replies can contain mistakes, so please check your order summary before you confirm it.
Please do not send sensitive information such as ID card numbers, card or bank details or
passwords in the chat.</p>

<h2>4. Who we share data with</h2>
<ul>
<li><strong>Meta</strong> (WhatsApp, Instagram, Messenger), which carries the messages.</li>
<li><strong>Google</strong>, for AI replies (Gemini) and for the spreadsheet (Google Sheets)
where orders are recorded.</li>
<li><strong>Railway</strong>, the hosting provider where conversation records are stored.</li>
<li><strong>The business</strong> that operates the chatbot, which receives your order
details so it can fulfil them.</li>
</ul>
<p>We may also disclose information when the law requires it.</p>

<h2>5. Storage and retention</h2>
<p>Conversation records and unfinished orders are stored on our hosting server and kept
until you ask us to delete them or we remove them. Completed orders are recorded in the
business's order spreadsheet and kept by the business as long as needed for delivery,
accounting and legal reasons.</p>

<h2>6. Your choices and deletion</h2>
<p>You can ask us to delete your data at any time. See our
<a href="/data-deletion">Data Deletion</a> page for how.</p>

<h2>7. Security</h2>
<p>We use access-restricted accounts, encrypted (HTTPS) connections and keep access keys
out of the code. No method of transmission or storage is completely secure.</p>

<h2>8. Children</h2>
<p>The service is not directed to children under 13 (or the minimum age in your country),
and we do not knowingly collect their data.</p>

<h2>9. Changes</h2>
<p>We may update this policy. The date at the top shows the latest version.</p>

<h2>10. Contact</h2>
<p>Questions about this policy: <a href="mailto:{EMAIL}">{EMAIL}</a>.</p>
""")


@router.get("/terms")
async def terms():
    return _page("Terms of Service", f"""
<p>By chatting with {SERVICE}, operated by {PROVIDER}{ON_BEHALF}, you agree to these terms.</p>

<h2>1. The service</h2>
<p>{SERVICE} is an automated chat assistant that answers questions and takes orders for a
business on WhatsApp, Instagram and Facebook Messenger.</p>

<h2>2. Orders</h2>
<p>An order placed through the chat is a request to buy from the business. Prices, stock,
delivery and payment are set by the business, not by {PROVIDER}. Please check your order
summary carefully before you confirm it.</p>

<h2>3. Automated replies</h2>
<p>Replies are generated automatically and may be inaccurate or incomplete. They are not
professional advice.</p>

<h2>4. Acceptable use</h2>
<p>Do not use the service for anything unlawful, abusive or misleading, do not send spam,
and do not try to disrupt or misuse the service.</p>

<h2>5. Availability</h2>
<p>The service is provided "as is" and may be unavailable, interrupted or changed at any
time.</p>

<h2>6. Liability</h2>
<p>To the extent permitted by law, {PROVIDER} is not responsible for the goods, delivery or
service provided by the business, or for losses caused by the use of or inability to use
the chatbot.</p>

<h2>7. Privacy</h2>
<p>How we handle your data is described in our <a href="/privacy">Privacy Policy</a>.</p>

<h2>8. Changes and contact</h2>
<p>We may update these terms; the date at the top shows the latest version. Questions:
<a href="mailto:{EMAIL}">{EMAIL}</a>.</p>
""")


@router.get("/data-deletion")
async def data_deletion():
    return _page("Data Deletion Instructions", f"""
<p>You can ask {PROVIDER} to delete the data that {SERVICE} holds about you.</p>

<h2>How to request deletion</h2>
<ol>
<li>Send an email to <a href="mailto:{EMAIL}?subject=Data%20Deletion%20Request">{EMAIL}</a>
with the subject <strong>Data Deletion Request</strong>.</li>
<li>In the email, include the WhatsApp phone number or the Instagram / Facebook username you
used to chat with the bot, so we can find your records.</li>
<li>We will confirm by email and delete your data within 30 days.</li>
</ol>

<h2>What is deleted</h2>
<ul>
<li>Your conversation records and any unfinished order stored by the chatbot.</li>
<li>Your order details held in the business's order spreadsheet, except where the business
must keep a completed order for accounting or legal reasons.</li>
</ul>

<p>Deleting a chat inside WhatsApp, Instagram or Messenger removes it only from your own
app and does not remove it from our records, so please email us to have it deleted.</p>
""")
