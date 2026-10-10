"""WhatsApp Cloud API transport. Same Crayon brain as Telegram.

Meta POSTs a signed webhook to /whatsapp/webhook (served by main.py). We verify the
X-Hub-Signature-256 header, accept only allow-listed senders, skip repeats, turn the
message into a Telegram-shaped update and call cr_telegram.handle_update with
WhatsAppOut, which sends replies through the Graph API.

A WhatsApp user is a separate Crayon user: uid = their number in digits (for example
918777566396). Their memory, notes and reminders are separate from any Telegram account.
Free-form messages only work within 24 hours of the person's last message to the number.
Tokens and the app secret are never logged.
"""
import hashlib
import hmac
import logging
import threading
import time
import json
from concurrent.futures import ThreadPoolExecutor

import httpx

import cr_config as C
import cr_db as db
from cr_safety import clean_text, redact

log = logging.getLogger("crayon.wa")
_http = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
_pool = ThreadPoolExecutor(max_workers=4)
SCHEMA = """
CREATE TABLE IF NOT EXISTS whatsapp_inbox(
 message_id TEXT PRIMARY KEY, sender TEXT NOT NULL, kind TEXT NOT NULL,
 value TEXT NOT NULL DEFAULT '', profile_name TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT 'queued', attempts INT NOT NULL DEFAULT 0,
 lease_until TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now(), completed_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS whatsapp_outbox(
 message_id TEXT PRIMARY KEY, recipient TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'accepted',
 status_ts BIGINT NOT NULL DEFAULT 0, error_code TEXT NOT NULL DEFAULT '',
 accepted BOOLEAN NOT NULL DEFAULT false, created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now());
"""


def init():
    if db.available():
        for stmt in SCHEMA.split(';'):
            if stmt.strip(): db.q(stmt, fetch='none')


def delivery(mid):
    """Internal readback. accepted is not delivered; never includes message content."""
    return db.q('SELECT message_id,recipient,state,status_ts,error_code FROM whatsapp_outbox WHERE message_id=%s AND accepted=true', (mid,), 'one')


TEXT_LIMIT = 3500  # WhatsApp allows 4096


def allowed_ids():
    raw = C.env("WHATSAPP_ALLOWED_IDS") or C.env("CRAYON_OWNER_WA_ID")
    return {"".join(ch for ch in x if ch.isdigit()) for x in raw.split(",") if any(ch.isdigit() for ch in x)}


def enabled():
    return bool(C.env("WHATSAPP_ACCESS_TOKEN") and C.env("WHATSAPP_PHONE_NUMBER_ID") and C.env("WHATSAPP_APP_SECRET")
                and C.env("WHATSAPP_VERIFY_TOKEN") and allowed_ids())


def owns(chat_id):
    """True when a chat id belongs to a WhatsApp user, so Telegram-side senders
    (reminders, digests) route to WhatsApp instead."""
    try:
        return str(int(chat_id)) in allowed_ids()
    except (TypeError, ValueError):
        return False


def _api_url(path):
    return f"https://graph.facebook.com/{C.env('WHATSAPP_GRAPH_VERSION', 'v25.0')}/{C.env('WHATSAPP_PHONE_NUMBER_ID')}/{path}"


def _headers():
    return {"Authorization": "Bearer " + C.env("WHATSAPP_ACCESS_TOKEN")}


def _post(body):
    if body.get('to') not in allowed_ids() or not db.available(): return None
    if body.get('type') not in ('text','interactive','image','document'): return None
    # Only reply in the current service window. No template or paid re-opening path.
    window = db.q("SELECT 1 FROM whatsapp_inbox WHERE sender=%s AND created_at>now()-interval '24 hours' LIMIT 1", (body['to'],), 'one')
    if not window: return None
    try:
        r = _http.post(_api_url("messages"), headers=_headers(), json=body)
    except httpx.HTTPError:
        log.warning("whatsapp send failed (network)")
        return None
    if r.status_code >= 400:
        # Status code only; the body can echo account details.
        log.warning("whatsapp send failed: HTTP %s%s", r.status_code,
                    " (token expired or invalid: generate a new WHATSAPP_ACCESS_TOKEN)" if r.status_code == 401 else "")
        return None
    try:
        mid = r.json()["messages"][0]["id"]
        if not isinstance(mid, str) or not mid: return None
        if db.available():
            db.q("INSERT INTO whatsapp_outbox(message_id,recipient,accepted) VALUES(%s,%s,true) ON CONFLICT(message_id) DO UPDATE SET accepted=true WHERE whatsapp_outbox.recipient=EXCLUDED.recipient",
                 (mid, body['to']), 'none')
        return mid
    except Exception:
        return None


def verify_signature(app_secret, raw_body, header):
    if not isinstance(header, str) or not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def verify_challenge(args):
    """GET handshake. args is a dict of single values. Returns the challenge or None."""
    tok = C.env("WHATSAPP_VERIFY_TOKEN")
    if args.get("hub.mode") == "subscribe" and tok and hmac.compare_digest(args.get("hub.verify_token", ""), tok):
        return args.get("hub.challenge", "")
    return None


def extract_messages(payload):
    """Yield (message_id, sender, kind, value, profile_name). kind: text, button, other."""
    if payload.get("object") != "whatsapp_business_account":
        return
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if not isinstance(value, dict) or value.get('group_id'):
                continue
            names = {c.get('wa_id'): c.get('profile', {}).get('name', '') for c in value.get('contacts', [])}
            phone = value.get('metadata', {}).get('phone_number_id')
            if phone != C.env('WHATSAPP_PHONE_NUMBER_ID'):
                continue
            for m in value.get("messages", []):
                mid, sender, typ = m.get("id"), m.get("from"), m.get("type")
                if not isinstance(mid, str) or not isinstance(sender, str) or not sender.isdigit() or m.get('group_id'):
                    continue
                name = names.get(sender, "")
                if typ == "text":
                    yield mid, sender, "text", m.get("text", {}).get("body", ""), name
                elif typ == "interactive":
                    i = m.get("interactive", {})
                    pick = i.get("button_reply") or i.get("list_reply") or {}
                    yield mid, sender, "button", pick.get("id", ""), name
                else:
                    yield mid, sender, "other", "", name


def _buttons_from_markup(markup):
    """Telegram inline_keyboard -> [(callback_data, title)]. Reply keyboards are ignored."""
    out = []
    for row in (markup or {}).get("inline_keyboard", []) if isinstance(markup, dict) else []:
        for b in row:
            cd, title = b.get("callback_data"), b.get("text")
            if cd and title and len(cd) <= 200:
                out.append((cd, title))
    return out[:10]


class WhatsAppOut:
    """Same interface as cr_telegram.Out."""

    def send(self, chat_id, text, markup=None):
        to = str(chat_id)
        if to not in allowed_ids(): raise ValueError('WhatsApp recipient is not allowed')
        text = clean_text(text) or "(empty)"
        ids = []
        for i in range(0, len(text), TEXT_LIMIT):
            mid = _post({"messaging_product": "whatsapp", "to": to, "type": "text",
                   "text": {"body": text[i:i + TEXT_LIMIT], "preview_url": False}})
            if not mid: raise RuntimeError('WhatsApp send not accepted')
            ids.append(mid)
        buttons = _buttons_from_markup(markup)
        if not buttons:
            return ids
        if len(buttons) <= 3:
            mid = _post({"messaging_product": "whatsapp", "to": to, "type": "interactive",
                   "interactive": {"type": "button", "body": {"text": "Choose an option:"},
                                   "action": {"buttons": [{"type": "reply", "reply": {"id": cd, "title": t[:20]}}
                                                          for cd, t in buttons]}}})
        else:
            mid = _post({"messaging_product": "whatsapp", "to": to, "type": "interactive",
                   "interactive": {"type": "list", "body": {"text": "Choose an option:"},
                                   "action": {"button": "Options", "sections": [{"title": "Options", "rows": [
                                       {"id": cd, "title": t[:24]} for cd, t in buttons]}]}}})

        if not mid: raise RuntimeError('WhatsApp review controls not accepted')
        ids.append(mid)
        return ids

    def artifact(self, chat_id, item):
        if str(chat_id) not in allowed_ids(): raise ValueError('WhatsApp recipient is not allowed')
        if len(item["data"]) > 2000000:
            raise ValueError("attachment too large")
        mime = item["mime"]
        try:
            r = _http.post(_api_url("media"), headers=_headers(), data={"messaging_product": "whatsapp", "type": mime},
                           files={"file": (item["filename"], item["data"], mime)})
            media_id = r.json()["id"] if r.status_code < 400 else None
        except Exception:
            media_id = None
        if not media_id:
            raise RuntimeError("WhatsApp attachment not confirmed")
        if mime == "image/png" and len(item["data"]) <= 5000000:
            body = {"type": "image", "image": {"id": media_id}}
        else:
            body = {"type": "document", "document": {"id": media_id, "filename": item["filename"]}}
        mid = _post({"messaging_product": "whatsapp", "to": str(chat_id), **body})
        if not mid:
            raise RuntimeError("WhatsApp attachment not confirmed")
        return mid

    def react(self, chat_id, message_id, emoji):
        return False  # no reactions on WhatsApp (never claim one was sent)

    def typing(self, chat_id):
        pass

    def delete(self, chat_id, message_id):
        return False  # WhatsApp cannot delete a user's message


def process(kind, value, uid, name, out=None):
    import cr_telegram as T
    out = out or WhatsAppOut()
    if kind == "other":
        out.send(uid, "Text only on WhatsApp for now. Send it as text and I'll help.")
    elif kind == "button":
        T.handle_callback({"id": "wa", "data": value, "from": {"id": uid}, "message": {"chat": {"id": uid}}}, out)
    else:
        T.handle_update({"message": {"chat": {"id": uid, "type": "private"},
                                     "from": {"id": uid, "first_name": name}, "text": value}}, out)


def extract_statuses(payload):
    if payload.get('object') != 'whatsapp_business_account': return
    for entry in payload.get('entry', []):
        for change in entry.get('changes', []):
            v = change.get('value', {})
            if v.get('metadata', {}).get('phone_number_id') != C.env('WHATSAPP_PHONE_NUMBER_ID'): continue
            for status in v.get('statuses', []):
                mid, recipient, state = status.get('id'), status.get('recipient_id'), status.get('status')
                if not isinstance(mid, str) or recipient not in allowed_ids() or state not in ('sent','delivered','read','failed'): continue
                try: ts = int(status.get('timestamp', 0))
                except (ValueError, TypeError): continue
                errors = status.get('errors') or []
                code = str(errors[0].get('code', '')) if errors and isinstance(errors[0], dict) else ''
                yield mid, recipient, state, ts, code if code.isdigit() else ''


def record_status(mid, recipient, state, ts, code):
    # Status may race the HTTP response. Store metadata first; delivery only exposes accepted sends.
    # Never persist provider error prose (may echo private data) or webhook body.
    if not db.available(): return
    db.q("""INSERT INTO whatsapp_outbox(message_id,recipient,state,status_ts,error_code)
      VALUES(%s,%s,%s,%s,%s) ON CONFLICT(message_id) DO UPDATE
      SET state=EXCLUDED.state,status_ts=EXCLUDED.status_ts,error_code=EXCLUDED.error_code,updated_at=now()
      WHERE whatsapp_outbox.recipient=EXCLUDED.recipient AND whatsapp_outbox.status_ts<=EXCLUDED.status_ts
      AND (whatsapp_outbox.state IN ('accepted','sent') OR
           (whatsapp_outbox.state='delivered' AND EXCLUDED.state='read'))""",
      (mid, recipient, state, ts, code), 'none')


def enqueue(mid, sender, kind, value, name):
    if not db.available():
        raise RuntimeError('WhatsApp persistence unavailable')
    # One durable row per wamid survives process restarts and repeated delivery.
    row = db.q("""INSERT INTO whatsapp_inbox(message_id,sender,kind,value,profile_name)
      VALUES(%s,%s,%s,%s,%s) ON CONFLICT(message_id) DO NOTHING RETURNING message_id""",
      (mid, sender, kind, redact(value)[:20000], redact(name)[:200]), 'one')
    if row: _pool.submit(_drain_one, mid)


def _drain_one(mid):
    row = db.q("""UPDATE whatsapp_inbox SET state='processing',attempts=attempts+1,
      lease_until=now()+interval '10 minutes' WHERE message_id=%s
      AND state='queued'
      AND attempts<3 RETURNING *""", (mid,), 'one')
    if not row: return
    try:
        # Recheck allowlist on replay, never resurrect revoked routing.
        if row['sender'] in allowed_ids():
            process(row['kind'], row['value'], int(row['sender']), row['profile_name'])
        db.q("UPDATE whatsapp_inbox SET state='done',completed_at=now(),value='',profile_name='' WHERE message_id=%s", (mid,), 'none')
    except Exception:
        # A partial send/action is uncertain; never auto-repeat it.
        db.q("UPDATE whatsapp_inbox SET state='uncertain',value='',profile_name='' WHERE message_id=%s", (mid,), 'none')
        log.warning('whatsapp handler failed; marked uncertain, no automatic replay')


def drain_pending():
    if not db.available(): return
    db.q("UPDATE whatsapp_inbox SET state='uncertain',value='',profile_name='' WHERE state='processing' AND lease_until<now()", fetch='none')
    rows = db.q("""SELECT message_id FROM whatsapp_inbox WHERE attempts<3 AND
      state='queued' ORDER BY created_at LIMIT 25""")
    for row in rows:
        if enabled(): _pool.submit(_drain_one, row['message_id'])
    db.q("DELETE FROM whatsapp_inbox WHERE created_at<now()-interval '7 days'", fetch='none')
    db.q("DELETE FROM whatsapp_outbox WHERE created_at<now()-interval '7 days'", fetch='none')


def start():
    def loop():
        while True:
            try: drain_pending()
            except Exception: log.warning('whatsapp inbox recovery unavailable')
            time.sleep(60)
    threading.Thread(target=loop, daemon=True).start()


def handle_webhook(raw_body, signature):
    """ACK after durable enqueue/status storage, never after only in-memory submission."""
    if not enabled(): return 404
    if not verify_signature(C.env('WHATSAPP_APP_SECRET'), raw_body, signature): return 403
    try:
        payload = json.loads(raw_body or b'{}')
        if not isinstance(payload, dict): return 400
        for args in extract_statuses(payload): record_status(*args)
        for mid, sender, kind, value, name in extract_messages(payload):
            if sender in allowed_ids(): enqueue(mid, sender, kind, value, name)
    except (ValueError, TypeError, AttributeError): return 400
    except Exception:
        log.warning('whatsapp webhook persistence unavailable')
        return 503  # provider can retry; do not falsely ACK lost work
    return 200
