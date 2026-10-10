"""Visitor identity, long-term memory and optional Google sign-in profiles.

- Every visitor gets a persistent anonymous id in a signed cookie (crayon_vid).
- Conversation turns are stored encrypted (Fernet derived from SECRET_KEY) in
  Postgres, keyed by that id only - one visitor can never read another's rows.
- Optional "Sign in with Google" (dormant until GOOGLE_CLIENT_ID/SECRET are set)
  links a name/picture profile and merges the anonymous id's memory.
- /api/forget wipes every stored turn for the current identity.
Nothing here runs without SECRET_KEY; memory simply stays off.
"""
import base64, hashlib, hmac, os, secrets, time
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

import gmail_auth
from gmail_auth import db

router = APIRouter()
VID_COOKIE = "crayon_vid"
SID_COOKIE = "crayon_gsid"
OAUTH_COOKIE = "crayon_poauth"
VID_DAYS = 400
SID_DAYS = 30
MEM_TURNS = 24          # message pairs loaded into model context
MEM_KEEP = 120          # rows kept per identity
MEM_DAYS = 90           # hard retention limit
MAX_STORE = 6000        # chars stored per message


def on() -> bool:
    return bool(os.environ.get("SECRET_KEY"))


def oauth_on() -> bool:
    return gmail_auth.enabled() and on()


def _fernet() -> Fernet:
    k = hashlib.sha256(("mem:" + os.environ["SECRET_KEY"]).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(k))


def _sign(val: str) -> str:
    mac = hmac.new(os.environ["SECRET_KEY"].encode(), val.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{val}.{mac}"


def identity(req: Request):
    """Returns (vid, is_new). Signing in rewrites the cookie to the linked id."""
    if on():
        v = gmail_auth._unsign(req.cookies.get(VID_COOKIE, ""))
        if v and len(v) == 34 and v.startswith("v_") and all(c in "0123456789abcdef" for c in v[2:]):
            return v, False
    return "v_" + secrets.token_hex(16), True


def set_vid_cookie(resp, vid: str):
    resp.set_cookie(VID_COOKIE, _sign(vid), max_age=VID_DAYS * 86400, httponly=True, secure=True, samesite="lax", path="/")


def _touch(vid: str):
    try:
        now = int(time.time())
        db("INSERT INTO cw_visitors (vid, created, last_seen) VALUES (%s, %s, %s) ON CONFLICT (vid) DO UPDATE SET last_seen = excluded.last_seen",
           (vid, now, now))
    except Exception as e:
        print("mem touch:", type(e).__name__)


def load_context(vid: str):
    """Recent turns for model context, newest last, stopping at the last 'new chat' boundary."""
    if not on():
        return []
    try:
        rows = db("SELECT role, enc FROM cw_mem WHERE vid = %s ORDER BY id DESC LIMIT %s", (vid, MEM_TURNS * 2 + 10), True) or []
    except Exception as e:
        print("mem load:", type(e).__name__)
        return []
    out = []
    f = _fernet()
    for role, enc in rows:
        if role == "x":
            break
        try:
            out.append((role, f.decrypt(enc.encode()).decode()))
        except Exception:
            pass
        if len(out) >= MEM_TURNS * 2:
            break
    out.reverse()
    return out


def save_turn(vid: str, user_text: str, ai_text: str):
    if not on():
        return
    try:
        f = _fernet()
        now = int(time.time())
        db("INSERT INTO cw_mem (vid, role, enc, ts) VALUES (%s, %s, %s, %s)", (vid, "user", f.encrypt(user_text[:MAX_STORE].encode()).decode(), now))
        db("INSERT INTO cw_mem (vid, role, enc, ts) VALUES (%s, %s, %s, %s)", (vid, "ai", f.encrypt(ai_text[:MAX_STORE].encode()).decode(), now))
        db("DELETE FROM cw_mem WHERE vid = %s AND (ts < %s OR id NOT IN (SELECT id FROM cw_mem WHERE vid = %s ORDER BY id DESC LIMIT %s))",
           (vid, now - MEM_DAYS * 86400, vid, MEM_KEEP))
        _touch(vid)
    except Exception as e:
        print("mem save:", type(e).__name__)


CHAT_KEEP = 200        # rows kept per chat
CHATS_MAX = 50         # chats listed per visitor


def save_chat_turn(vid: str, sid: str, user_text: str, ai_text: str):
    """Per-session transcript for the history sidebar (separate from rolling memory)."""
    if not on() or not sid:
        return
    try:
        f = _fernet()
        now = int(time.time())
        title = (user_text.strip().split("\n")[0] or "Chat")[:60]
        db("INSERT INTO cw_cmsgs (vid, sid, role, enc, ts) VALUES (%s, %s, %s, %s, %s)", (vid, sid, "user", f.encrypt(user_text[:MAX_STORE].encode()).decode(), now))
        db("INSERT INTO cw_cmsgs (vid, sid, role, enc, ts) VALUES (%s, %s, %s, %s, %s)", (vid, sid, "ai", f.encrypt(ai_text[:MAX_STORE].encode()).decode(), now))
        db("INSERT INTO cw_chats (vid, sid, title, updated) VALUES (%s, %s, %s, %s) ON CONFLICT (vid, sid) DO UPDATE SET updated = excluded.updated",
           (vid, sid, title, now))
        db("DELETE FROM cw_cmsgs WHERE vid = %s AND sid = %s AND (ts < %s OR id NOT IN (SELECT id FROM cw_cmsgs WHERE vid = %s AND sid = %s ORDER BY id DESC LIMIT %s))",
           (vid, sid, now - MEM_DAYS * 86400, vid, sid, CHAT_KEEP))
        db("DELETE FROM cw_chats WHERE vid = %s AND sid NOT IN (SELECT sid FROM cw_chats WHERE vid = %s ORDER BY updated DESC LIMIT %s)",
           (vid, vid, CHATS_MAX * 2))
    except Exception as e:
        print("chat save:", type(e).__name__)


def list_chats(vid: str):
    try:
        rows = db("SELECT sid, title, updated FROM cw_chats WHERE vid = %s ORDER BY updated DESC LIMIT %s", (vid, CHATS_MAX), True) or []
        return [{"sid": r[0], "title": r[1], "updated": r[2]} for r in rows]
    except Exception as e:
        print("chat list:", type(e).__name__)
        return []


def load_chat(vid: str, sid: str):
    try:
        rows = db("SELECT role, enc FROM cw_cmsgs WHERE vid = %s AND sid = %s ORDER BY id DESC LIMIT 120", (vid, sid), True) or []
    except Exception as e:
        print("chat load:", type(e).__name__)
        return []
    out = []
    f = _fernet()
    for role, enc in rows:
        try:
            out.append({"role": role, "text": f.decrypt(enc.encode()).decode()})
        except Exception:
            pass
    out.reverse()
    return out


def delete_chat(vid: str, sid: str):
    try:
        db("DELETE FROM cw_cmsgs WHERE vid = %s AND sid = %s", (vid, sid))
        db("DELETE FROM cw_chats WHERE vid = %s AND sid = %s", (vid, sid))
    except Exception as e:
        print("chat delete:", type(e).__name__)


def boundary(vid: str):
    """Marks 'new chat': context and history start fresh, memory is kept."""
    if not on():
        return
    try:
        db("INSERT INTO cw_mem (vid, role, enc, ts) VALUES (%s, 'x', '', %s)", (vid, int(time.time())))
    except Exception as e:
        print("mem boundary:", type(e).__name__)


def profile(req: Request):
    """{'name','email','picture'} for a valid sign-in session, else None."""
    if not oauth_on():
        return None
    tok = req.cookies.get(SID_COOKIE, "")
    if not tok or len(tok) > 100:
        return None
    try:
        rows = db("SELECT v.name, v.email, v.picture FROM cw_signin s JOIN cw_visitors v ON v.vid = s.vid WHERE s.h = %s AND s.exp > %s AND v.gsub IS NOT NULL",
                  (hashlib.sha256(tok.encode()).hexdigest(), int(time.time())), True)
    except Exception:
        return None
    if not rows:
        return None
    return {"name": rows[0][0], "email": rows[0][1], "picture": rows[0][2]}


@router.get("/auth/signin/google")
def signin():
    if not oauth_on():
        return JSONResponse({"error": "Sign-in is not configured yet."}, status_code=503)
    state = secrets.token_urlsafe(24)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"], "redirect_uri": gmail_auth.PUBLIC_URL + "/auth/signin/callback",
        "response_type": "code", "scope": "openid email profile", "state": state})
    r = RedirectResponse(url, status_code=302)
    r.set_cookie(OAUTH_COOKIE, _sign(state), max_age=600, httponly=True, secure=True, samesite="lax", path="/auth")
    return r


@router.get("/auth/signin/callback")
def signin_cb(req: Request, code: str = "", state: str = "", error: str = ""):
    back = RedirectResponse("/", status_code=302)
    back.delete_cookie(OAUTH_COOKIE, path="/auth")
    if error or not code or not state or not oauth_on():
        return RedirectResponse("/?signin=cancelled", status_code=302)
    st = gmail_auth._unsign(req.cookies.get(OAUTH_COOKIE, ""))
    if not st or not secrets.compare_digest(st, state):
        return RedirectResponse("/?signin=error", status_code=302)
    try:
        tr = httpx.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": gmail_auth.PUBLIC_URL + "/auth/signin/callback", "grant_type": "authorization_code"}, timeout=15).json()
        at = tr.get("access_token")
        if not at:
            return RedirectResponse("/?signin=error", status_code=302)
        info = httpx.get("https://openidconnect.googleapis.com/v1/userinfo", headers={"Authorization": "Bearer " + at}, timeout=15).json()
        sub = str(info.get("sub") or "")
        if not sub:
            return RedirectResponse("/?signin=error", status_code=302)
        name = str(info.get("name") or "")[:80]
        email = str(info.get("email") or "")[:120]
        picture = str(info.get("picture") or "")[:300]
        cur, _ = identity(req)
        rows = db("SELECT vid FROM cw_visitors WHERE gsub = %s", (sub,), True) or []
        canonical = rows[0][0] if rows else cur
        now = int(time.time())
        db("INSERT INTO cw_visitors (vid, created, last_seen, gsub, name, email, picture) VALUES (%s,%s,%s,%s,%s,%s,%s) "
           "ON CONFLICT (vid) DO UPDATE SET last_seen=excluded.last_seen, gsub=excluded.gsub, name=excluded.name, email=excluded.email, picture=excluded.picture",
           (canonical, now, now, sub, name, email, picture))
        if canonical != cur:
            db("UPDATE cw_mem SET vid = %s WHERE vid = %s", (canonical, cur))
            db("DELETE FROM cw_visitors WHERE vid = %s AND gsub IS NULL", (cur,))
        tok = secrets.token_urlsafe(32)
        db("DELETE FROM cw_signin WHERE exp < %s", (now,))
        db("INSERT INTO cw_signin (h, vid, exp) VALUES (%s, %s, %s)", (hashlib.sha256(tok.encode()).hexdigest(), canonical, now + SID_DAYS * 86400))
        back.set_cookie(SID_COOKIE, tok, max_age=SID_DAYS * 86400, httponly=True, secure=True, samesite="lax", path="/")
        set_vid_cookie(back, canonical)
        return back
    except Exception as e:
        print("signin callback error:", type(e).__name__)
        return RedirectResponse("/?signin=error", status_code=302)


@router.post("/auth/signout")
def signout(req: Request):
    if req.headers.get("x-requested-with") != "crayon":
        return JSONResponse({"error": "bad request"}, status_code=403)
    tok = req.cookies.get(SID_COOKIE, "")
    if tok and oauth_on():
        db("DELETE FROM cw_signin WHERE h = %s", (hashlib.sha256(tok.encode()).hexdigest(),))
    r = JSONResponse({"ok": True})
    r.delete_cookie(SID_COOKIE, path="/")
    return r


@router.get("/api/history")
def history(req: Request):
    vid, new = identity(req)
    msgs = []
    if on():
        try:
            rows = db("SELECT role, enc FROM cw_mem WHERE vid = %s ORDER BY id DESC LIMIT 45", (vid,), True) or []
            f = _fernet()
            for role, enc in rows:
                if role == "x":
                    break
                try:
                    msgs.append({"role": role, "text": f.decrypt(enc.encode()).decode()})
                except Exception:
                    pass
                if len(msgs) >= 40:
                    break
            msgs.reverse()
        except Exception as e:
            print("mem history:", type(e).__name__)
    r = JSONResponse({"messages": msgs, "memory": on()})
    if new:
        set_vid_cookie(r, vid)
    return r


def forget_vid(vid: str):
    try:
        db("DELETE FROM cw_mem WHERE vid = %s", (vid,))
        db("DELETE FROM cw_cmsgs WHERE vid = %s", (vid,))
        db("DELETE FROM cw_chats WHERE vid = %s", (vid,))
    except Exception as e:
        print("mem forget:", type(e).__name__)


@router.get("/api/chats")
def chats(req: Request):
    vid, new = identity(req)
    r = JSONResponse({"chats": list_chats(vid) if on() else []})
    if new:
        set_vid_cookie(r, vid)
    return r


@router.get("/api/chats/{sid}")
def chat_messages(req: Request, sid: str):
    vid, new = identity(req)
    sid = sid[:64]
    r = JSONResponse({"messages": load_chat(vid, sid) if on() else []})
    if new:
        set_vid_cookie(r, vid)
    return r


@router.delete("/api/chats/{sid}")
def chat_delete(req: Request, sid: str):
    if req.headers.get("x-requested-with") != "crayon":
        return JSONResponse({"error": "bad request"}, status_code=403)
    vid, _ = identity(req)
    if on():
        delete_chat(vid, sid[:64])
    return JSONResponse({"ok": True})


@router.post("/api/forget")
def forget(req: Request):
    if req.headers.get("x-requested-with") != "crayon":
        return JSONResponse({"error": "bad request"}, status_code=403)
    vid, new = identity(req)
    if on():
        try:
            db("DELETE FROM cw_mem WHERE vid = %s", (vid,))
        except Exception as e:
            print("mem forget:", type(e).__name__)
    r = JSONResponse({"ok": True})
    if new:
        set_vid_cookie(r, vid)
    return r


# ---------- usage events + owner dashboard ----------

def event(vid: str | None, kind: str):
    """Fire-and-forget usage counter. Never raises."""
    if not on():
        return
    try:
        db("INSERT INTO cw_events (ts, kind, vid) VALUES (%s, %s, %s)", (int(time.time()), kind[:60], vid))
        db("DELETE FROM cw_events WHERE ts < %s", (int(time.time()) - 60 * 86400,))
    except Exception:
        pass


def _admin_ok(req: Request) -> bool:
    tok = os.environ.get("ADMIN_TOKEN", "")
    if not tok:
        return False
    q = req.query_params.get("token", "")
    if q and hmac.compare_digest(q, tok):
        return True
    # owner signed in with Google and matching OWNER_EMAIL
    try:
        p = profile(req)
        owner = os.environ.get("OWNER_EMAIL", "").strip().lower()
        if p and owner and (p.get("email") or "").lower() == owner:
            return True
    except Exception:
        pass
    return False


@router.get("/admin")
def admin(req: Request):
    if not _admin_ok(req):
        return JSONResponse({"error": "not found"}, status_code=404)
    now = int(time.time())
    day0 = now - 14 * 86400
    try:
        daily = db("SELECT (ts / 86400) * 86400 AS d, count(DISTINCT vid), count(*) FROM cw_events WHERE ts > %s AND kind = 'chat' GROUP BY 1 ORDER BY 1", (day0,), True) or []
        kinds = db("SELECT kind, count(*) FROM cw_events WHERE ts > %s GROUP BY kind ORDER BY 2 DESC LIMIT 15", (day0,), True) or []
        models = db("SELECT kind, count(*) FROM cw_events WHERE ts > %s AND kind LIKE 'model:%%' GROUP BY kind ORDER BY 2 DESC", (day0,), True) or []
        tot_visitors = db("SELECT count(*) FROM cw_visitors", (), True)[0][0]
        tot_signed = db("SELECT count(*) FROM cw_visitors WHERE gsub IS NOT NULL", (), True)[0][0]
        tot_mem = db("SELECT count(*) FROM cw_mem WHERE role <> 'x'", (), True)[0][0]
        tot_shares = db("SELECT count(*) FROM cw_shares", (), True)[0][0]
    except Exception as e:
        return JSONResponse({"error": "db error: " + type(e).__name__}, status_code=500)
    import datetime
    rows = "".join(f"<tr><td>{datetime.datetime.utcfromtimestamp(d).strftime('%b %d')}</td><td>{u}</td><td>{n}</td></tr>" for d, u, n in daily)
    krows = "".join(f"<tr><td>{k}</td><td>{c}</td></tr>" for k, c in kinds if not str(k).startswith("model:"))
    mrows = "".join(f"<tr><td>{k[6:]}</td><td>{c}</td></tr>" for k, c in models)
    html = f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content=noindex>
<title>Crayon admin</title><body style="font:15px system-ui;max-width:560px;margin:32px auto;padding:0 16px;color:#222">
<h2>Crayon - owner dashboard</h2>
<p>Visitors: <b>{tot_visitors}</b> ({tot_signed} signed in) &middot; saved messages: <b>{tot_mem}</b> &middot; shared chats: <b>{tot_shares}</b></p>
<h3>Daily (last 14 days)</h3><table border=1 cellpadding=6 cellspacing=0><tr><th>Day</th><th>Active users</th><th>Messages</th></tr>{rows}</table>
<h3>Features (14 days)</h3><table border=1 cellpadding=6 cellspacing=0>{krows}</table>
<h3>Models used (14 days)</h3><table border=1 cellpadding=6 cellspacing=0>{mrows}</table>
<p style="color:#888">Chat counts count user messages. Data from your own database only.</p>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# ---------- share-able chats ----------

def _enc_json(obj) -> str:
    import json as _json
    return _fernet().encrypt(_json.dumps(obj).encode()).decode()


@router.post("/api/share")
async def share(req: Request):
    if req.headers.get("x-requested-with") != "crayon":
        return JSONResponse({"error": "bad request"}, status_code=403)
    if not on():
        return JSONResponse({"error": "Sharing is unavailable."}, status_code=503)
    import app as _app
    msg = _app.limited(_app.client_ip(req))
    if msg:
        return JSONResponse({"error": msg}, status_code=429)
    vid, _ = identity(req)
    msgs = load_context(vid)
    msgs = [(r, t) for r, t in msgs if r in ("user", "ai")][-30:]
    if not msgs:
        return JSONResponse({"error": "Nothing to share yet - send a message first."}, status_code=400)
    sid = "s" + secrets.token_urlsafe(12)
    try:
        db("INSERT INTO cw_shares (sid, vid, enc, created) VALUES (%s, %s, %s, %s)",
           (sid, vid, _enc_json([{"role": r, "text": t} for r, t in msgs]), int(time.time())))
        db("DELETE FROM cw_shares WHERE vid = %s AND sid NOT IN (SELECT sid FROM cw_shares WHERE vid = %s ORDER BY created DESC LIMIT 20)", (vid, vid))
    except Exception as e:
        print("share save:", type(e).__name__)
        return JSONResponse({"error": "Could not create the link."}, status_code=500)
    event(vid, "share")
    return {"url": gmail_auth.PUBLIC_URL + "/s/" + sid}


@router.get("/s/{sid}")
def shared_page(sid: str):
    if not on() or not re_fullmatch_sid(sid):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        rows = db("SELECT enc FROM cw_shares WHERE sid = %s", (sid,), True)
    except Exception:
        rows = None
    if not rows:
        return JSONResponse({"error": "not found"}, status_code=404)
    import json as _json, html as _html
    try:
        msgs = _json.loads(_fernet().decrypt(rows[0][0].encode()).decode())
    except Exception:
        return JSONResponse({"error": "not found"}, status_code=404)
    parts = []
    for m in msgs[:60]:
        role = "You" if m.get("role") == "user" else "Crayon"
        bg = "#f4ecec" if m.get("role") == "user" else "#fff"
        parts.append(f'<div style="background:{bg};border-radius:14px;padding:12px 14px;margin:8px 0"><b style="font-size:12px;color:#a06">{role}</b><div style="white-space:pre-wrap;margin-top:2px">{_html.escape(str(m.get("text", "")))}</div></div>')
    page = f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content=noindex>
<title>Shared Crayon chat</title></head><body style="font:16px system-ui;max-width:680px;margin:0 auto;padding:20px 16px 60px;background:#faf7f7;color:#2b2224">
<div style="display:flex;align-items:center;gap:8px;margin-bottom:14px"><img src="/static/crayon.svg" width=26 height=26><b>Crayon</b><span style="color:#999;font-size:13px">shared chat (read-only)</span></div>
{''.join(parts)}
<p style="color:#999;font-size:13px;margin-top:20px">Chat with your own Crayon at <a href="https://crayon-web.onrender.com">crayon-web.onrender.com</a></p>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(page, headers={"Cache-Control": "public, max-age=300"})


import re as _re


def re_fullmatch_sid(s: str) -> bool:
    return bool(_re.fullmatch(r"s[A-Za-z0-9_-]{10,20}", s or ""))
