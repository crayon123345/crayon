"""Per-user Google sign-in + Gmail tools. Tokens are encrypted at rest; every Gmail call is scoped to the signed-in session's user."""
import base64, hashlib, json, os, re, secrets, sqlite3, threading, time
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

router = APIRouter()
SCOPES = "openid email https://www.googleapis.com/auth/gmail.modify"
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://crayon-web.onrender.com").rstrip("/")
REDIRECT = PUBLIC_URL + "/auth/google/callback"
COOKIE = "crayon_sid"
SESSION_DAYS = 7
_lock = threading.Lock()


def enabled() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET") and os.environ.get("SECRET_KEY"))


def _fernet() -> Fernet:
    k = hashlib.sha256(("tok:" + os.environ["SECRET_KEY"]).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(k))


def _sign(val: str) -> str:
    import hmac
    mac = hmac.new(os.environ["SECRET_KEY"].encode(), val.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{val}.{mac}"


def _unsign(s: str):
    import hmac
    if not s or "." not in s:
        return None
    val, mac = s.rsplit(".", 1)
    good = hmac.new(os.environ["SECRET_KEY"].encode(), val.encode(), hashlib.sha256).hexdigest()[:32]
    return val if hmac.compare_digest(mac, good) else None


# ---------- storage (Postgres on Render; sqlite fallback for local dev) ----------
_DB = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
_ready = False


def _conn():
    if _DB.startswith("postgres"):
        import psycopg
        return psycopg.connect(_DB, connect_timeout=8)
    c = sqlite3.connect(os.environ.get("LOCAL_DB", "/tmp/crayon_local.db"))
    return c


def db(sql: str, args=(), fetch=False):
    global _ready
    pg = _DB.startswith("postgres")
    if not pg:
        sql = sql.replace("%s", "?")
    with _lock:
        c = _conn()
        try:
            if not _ready:
                ddl = ["CREATE TABLE IF NOT EXISTS cw_users (id %s PRIMARY KEY, sub TEXT UNIQUE NOT NULL, email TEXT NOT NULL, refresh_enc TEXT NOT NULL, created BIGINT NOT NULL)" % ("SERIAL" if pg else "INTEGER"),
                       "CREATE TABLE IF NOT EXISTS cw_sessions (h TEXT PRIMARY KEY, user_id INTEGER NOT NULL, exp BIGINT NOT NULL)",
                       "CREATE TABLE IF NOT EXISTS cw_visitors (vid TEXT PRIMARY KEY, created BIGINT NOT NULL, last_seen BIGINT NOT NULL, gsub TEXT UNIQUE, name TEXT, email TEXT, picture TEXT)",
                       "CREATE TABLE IF NOT EXISTS cw_mem (id %s PRIMARY KEY, vid TEXT NOT NULL, role TEXT NOT NULL, enc TEXT NOT NULL, ts BIGINT NOT NULL)" % ("SERIAL" if pg else "INTEGER"),
                       "CREATE TABLE IF NOT EXISTS cw_signin (h TEXT PRIMARY KEY, vid TEXT NOT NULL, exp BIGINT NOT NULL)",
                       "CREATE TABLE IF NOT EXISTS cw_events (id %s PRIMARY KEY, ts BIGINT NOT NULL, kind TEXT NOT NULL, vid TEXT)" % ("SERIAL" if pg else "INTEGER"),
                       "CREATE TABLE IF NOT EXISTS cw_shares (sid TEXT PRIMARY KEY, vid TEXT NOT NULL, enc TEXT NOT NULL, created BIGINT NOT NULL)",
                       "CREATE TABLE IF NOT EXISTS cw_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)",
                       "CREATE INDEX IF NOT EXISTS cw_mem_vid ON cw_mem (vid, id)",
                       "CREATE INDEX IF NOT EXISTS cw_events_ts ON cw_events (ts)",
                       "CREATE TABLE IF NOT EXISTS cw_chats (vid TEXT NOT NULL, sid TEXT NOT NULL, title TEXT NOT NULL, updated BIGINT NOT NULL, PRIMARY KEY (vid, sid))",
                       "CREATE TABLE IF NOT EXISTS cw_cmsgs (id %s PRIMARY KEY, vid TEXT NOT NULL, sid TEXT NOT NULL, role TEXT NOT NULL, enc TEXT NOT NULL, ts BIGINT NOT NULL)" % ("SERIAL" if pg else "INTEGER"),
                       "CREATE INDEX IF NOT EXISTS cw_cmsgs_vs ON cw_cmsgs (vid, sid, id)"]
                for d in ddl:
                    c.execute(d)
                c.commit()
                _ready = True
            cur = c.execute(sql, args)
            rows = cur.fetchall() if fetch else None
            c.commit()
            return rows
        finally:
            c.close()


def migrate_if_needed():
    """One-time copy of all cw_* rows from the old DATABASE_URL Postgres to NEON_DATABASE_URL.
    Runs at boot only when both are set; marks itself done in cw_meta."""
    src, dst = os.environ.get("DATABASE_URL", ""), os.environ.get("NEON_DATABASE_URL", "")
    if not src.startswith("postgres") or not dst.startswith("postgres") or src == dst:
        return
    try:
        done = db("SELECT v FROM cw_meta WHERE k = 'migrated_from_old'", (), True)
        if done:
            return
        import psycopg
        s = psycopg.connect(src, connect_timeout=8)
        d = psycopg.connect(dst, connect_timeout=8)
        try:
            for table, cols in (("cw_users", "id, sub, email, refresh_enc, created"),
                                ("cw_sessions", "h, user_id, exp"),
                                ("cw_secrets", "name, val"),
                                ("cw_visitors", "vid, created, last_seen, gsub, name, email, picture"),
                                ("cw_mem", "id, vid, role, enc, ts"),
                                ("cw_signin", "h, vid, exp"),
                                ("cw_events", "id, ts, kind, vid")):
                try:
                    rows = s.execute(f"SELECT {cols} FROM {table}").fetchall()
                except Exception:
                    continue
                ph = ",".join(["%s"] * len(cols.split(",")))
                for row in rows:
                    try:
                        d.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph}) ON CONFLICT DO NOTHING", row)
                        d.commit()
                    except Exception:
                        d.rollback()
            for table, seq in (("cw_users", "cw_users_id_seq"), ("cw_mem", "cw_mem_id_seq"), ("cw_events", "cw_events_id_seq")):
                try:
                    d.execute(f"SELECT setval('{seq}', COALESCE((SELECT MAX(id) FROM {table}), 1))")
                    d.commit()
                except Exception:
                    d.rollback()
            d.execute("INSERT INTO cw_meta (k, v) VALUES ('migrated_from_old', %s) ON CONFLICT DO NOTHING", (str(int(time.time())),))
            d.commit()
            print("db migrate: copied rows old -> neon")
        finally:
            s.close()
            d.close()
    except Exception as e:
        print("db migrate:", type(e).__name__, str(e)[:120])


def _hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def current_user(req: Request):
    """Returns (user_id, email, refresh_token) for the session cookie, or None."""
    if not enabled():
        return None
    tok = req.cookies.get(COOKIE, "")
    if not tok or len(tok) > 100:
        return None
    rows = db("SELECT u.id, u.email, u.refresh_enc FROM cw_sessions s JOIN cw_users u ON u.id = s.user_id WHERE s.h = %s AND s.exp > %s", (_hash(tok), int(time.time())), fetch=True)
    if not rows:
        return None
    uid, email, enc = rows[0]
    try:
        return uid, email, _fernet().decrypt(enc.encode()).decode()
    except Exception:
        return None


def _csrf_ok(req: Request) -> bool:
    return req.headers.get("x-requested-with") == "crayon"


# ---------- OAuth ----------
@router.get("/auth/google/login")
def login():
    if not enabled():
        return JSONResponse({"error": "Gmail sign-in is not configured yet."}, status_code=503)
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"], "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    r = RedirectResponse(url, status_code=302)
    r.set_cookie("crayon_oauth", _sign(state + ":" + verifier), max_age=600, httponly=True, secure=True, samesite="lax", path="/auth")
    return r


@router.get("/auth/google/callback")
def callback(req: Request, code: str = "", state: str = "", error: str = ""):
    back = RedirectResponse("/", status_code=302)
    back.delete_cookie("crayon_oauth", path="/auth")
    if error or not code or not state or not enabled():
        return RedirectResponse("/?mail=cancelled", status_code=302)
    data = _unsign(req.cookies.get("crayon_oauth", ""))
    if not data or ":" not in data:
        return RedirectResponse("/?mail=error", status_code=302)
    st, verifier = data.split(":", 1)
    if not secrets.compare_digest(st, state):
        return RedirectResponse("/?mail=error", status_code=302)
    try:
        tr = httpx.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": REDIRECT, "grant_type": "authorization_code", "code_verifier": verifier}, timeout=15).json()
        at, rt = tr.get("access_token"), tr.get("refresh_token")
        if not at or "gmail.modify" not in (tr.get("scope") or ""):
            return RedirectResponse("/?mail=noscope", status_code=302)
        info = httpx.get("https://openidconnect.googleapis.com/v1/userinfo", headers={"Authorization": "Bearer " + at}, timeout=15).json()
        sub, email = info.get("sub"), info.get("email")
        if not sub or not email:
            return RedirectResponse("/?mail=error", status_code=302)
        if not rt:  # Google only returns a refresh token on consent; reuse the stored one if we have it
            row = db("SELECT refresh_enc FROM cw_users WHERE sub = %s", (sub,), fetch=True)
            if not row:
                return RedirectResponse("/?mail=error", status_code=302)
            enc = row[0][0]
        else:
            enc = _fernet().encrypt(rt.encode()).decode()
        db("INSERT INTO cw_users (sub, email, refresh_enc, created) VALUES (%s, %s, %s, %s) ON CONFLICT (sub) DO UPDATE SET email = excluded.email, refresh_enc = excluded.refresh_enc", (sub, email, enc, int(time.time())))
        uid = db("SELECT id FROM cw_users WHERE sub = %s", (sub,), fetch=True)[0][0]
        tok = secrets.token_urlsafe(32)
        db("DELETE FROM cw_sessions WHERE exp < %s", (int(time.time()),))
        db("INSERT INTO cw_sessions (h, user_id, exp) VALUES (%s, %s, %s)", (_hash(tok), uid, int(time.time()) + SESSION_DAYS * 86400))
        back.set_cookie(COOKIE, tok, max_age=SESSION_DAYS * 86400, httponly=True, secure=True, samesite="lax", path="/")
        return back
    except Exception as e:
        print("oauth callback error:", type(e).__name__)
        return RedirectResponse("/?mail=error", status_code=302)


@router.get("/api/me")
def me(req: Request):
    u = current_user(req)
    prof, profiles_on = None, False
    try:
        import memory
        prof = memory.profile(req)
        profiles_on = memory.oauth_on()
    except Exception:
        pass
    return {"configured": enabled(), "signed_in": bool(u), "email": u[1] if u else None,
            "profiles": profiles_on, "profile": prof}


@router.post("/auth/logout")
def logout(req: Request):
    if not _csrf_ok(req):
        return JSONResponse({"error": "bad request"}, status_code=403)
    tok = req.cookies.get(COOKIE, "")
    if tok and enabled():
        db("DELETE FROM cw_sessions WHERE h = %s", (_hash(tok),))
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE, path="/")
    return r


@router.post("/auth/disconnect")
def disconnect(req: Request):
    if not _csrf_ok(req):
        return JSONResponse({"error": "bad request"}, status_code=403)
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not signed in"}, status_code=401)
    uid, _, rt = u
    try:
        httpx.post("https://oauth2.googleapis.com/revoke", data={"token": rt}, timeout=10)
    except Exception:
        pass
    db("DELETE FROM cw_sessions WHERE user_id = %s", (uid,))
    db("DELETE FROM cw_users WHERE id = %s", (uid,))
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE, path="/")
    return r


# ---------- Gmail tools ----------
_atok: dict = {}  # user_id -> (token, exp)
API = "https://gmail.googleapis.com/gmail/v1/users/me"
ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
SYSTEM_LABELS = {"INBOX", "UNREAD", "STARRED", "IMPORTANT", "SPAM", "TRASH", "SENT", "DRAFT"}


def _access(uid: int, rt: str) -> str:
    t = _atok.get(uid)
    if t and t[1] > time.time() + 60:
        return t[0]
    r = httpx.post("https://oauth2.googleapis.com/token", data={"client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"], "refresh_token": rt, "grant_type": "refresh_token"}, timeout=15).json()
    if "access_token" not in r:
        raise PermissionError("Google access was revoked. Reconnect Gmail.")
    _atok[uid] = (r["access_token"], time.time() + int(r.get("expires_in", 3000)))
    return r["access_token"]


def _g(uid, rt, method, path, **kw):
    h = {"Authorization": "Bearer " + _access(uid, rt)}
    r = httpx.request(method, API + path, headers=h, timeout=20, **kw)
    if r.status_code == 401:
        _atok.pop(uid, None)
    if r.status_code >= 400:
        raise RuntimeError(f"Gmail error {r.status_code}")
    return r.json() if r.content else {}


def _hdr(msg, name):
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name:
            return h.get("value", "")[:300]
    return ""


def _text(payload) -> str:
    out = []

    def walk(p):
        mt = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data and mt in ("text/plain", "text/html"):
            try:
                s = base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace")
            except Exception:
                s = ""
            if mt == "text/html":
                s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
                s = re.sub(r"<[^>]+>", " ", s)
            out.append((0 if mt == "text/plain" else 1, s))
        for q in p.get("parts", []) or []:
            walk(q)
    walk(payload or {})
    out.sort(key=lambda x: x[0])
    s = out[0][1] if out else ""
    return re.sub(r"\s+", " ", s).strip()


def _label_ids(uid, rt, names, create=False):
    labels = _g(uid, rt, "GET", "/labels").get("labels", [])
    by = {l["name"].lower(): l["id"] for l in labels}
    ids = []
    for n in names or []:
        n = str(n).strip()[:60]
        if not n:
            continue
        if n.upper() in SYSTEM_LABELS:
            ids.append(n.upper()); continue
        if n.lower() in by:
            ids.append(by[n.lower()]); continue
        if create:
            ids.append(_g(uid, rt, "POST", "/labels", json={"name": n, "labelListVisibility": "labelShow", "messageListVisibility": "show"})["id"])
    return ids


WRITES = {"modify", "trash", "draft", "send"}


def describe(action: str, a: dict) -> str:
    if action == "modify":
        return f"Change message {a.get('id')}: add {a.get('add') or []}, remove {a.get('remove') or []}"
    if action == "trash":
        return f"Move message {a.get('id')} to Trash"
    if action == "draft":
        return f"Create a draft to {a.get('to')} - subject: {a.get('subject')}"
    if action == "send":
        return f"SEND an email to {a.get('to')} - subject: {a.get('subject')}"
    return action


def run_tool(uid, rt, action, a):
    if action == "search":
        q = str(a.get("query", ""))[:200]
        n = max(1, min(int(a.get("max", 8) or 8), 10))
        ids = _g(uid, rt, "GET", "/messages", params={"q": q, "maxResults": n}).get("messages", [])
        res = []
        for m in ids:
            d = _g(uid, rt, "GET", "/messages/" + m["id"], params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
            res.append({"id": m["id"], "from": _hdr(d, "from"), "subject": _hdr(d, "subject"), "date": _hdr(d, "date"), "snippet": d.get("snippet", "")[:160], "labels": d.get("labelIds", [])})
        return {"messages": res}
    if action == "read":
        mid = str(a.get("id", ""))
        if not ID_RE.match(mid):
            raise ValueError("bad id")
        d = _g(uid, rt, "GET", "/messages/" + mid, params={"format": "full"})
        return {"id": mid, "from": _hdr(d, "from"), "to": _hdr(d, "to"), "subject": _hdr(d, "subject"), "date": _hdr(d, "date"), "labels": d.get("labelIds", []), "body": _text(d.get("payload"))[:4000]}
    if action == "labels":
        return {"labels": [l["name"] for l in _g(uid, rt, "GET", "/labels").get("labels", [])][:100]}
    if action in ("modify", "trash"):
        mid = str(a.get("id", ""))
        if not ID_RE.match(mid):
            raise ValueError("bad id")
        if action == "trash":
            _g(uid, rt, "POST", f"/messages/{mid}/trash")
            return {"ok": True}
        add = _label_ids(uid, rt, a.get("add"), create=True)
        rem = _label_ids(uid, rt, a.get("remove"))
        if "TRASH" in add or "SPAM" in add:
            raise ValueError("use the trash action")
        _g(uid, rt, "POST", f"/messages/{mid}/modify", json={"addLabelIds": add, "removeLabelIds": rem})
        return {"ok": True}
    if action in ("draft", "send"):
        from email.message import EmailMessage
        to, subj, body = str(a.get("to", ""))[:200], str(a.get("subject", ""))[:200], str(a.get("body", ""))[:8000]
        if not re.match(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$", to) or "\n" in subj:
            raise ValueError("bad recipient or subject")
        m = EmailMessage(); m["To"] = to; m["Subject"] = subj; m.set_content(body)
        raw = base64.urlsafe_b64encode(m.as_bytes()).decode()
        if action == "draft":
            _g(uid, rt, "POST", "/drafts", json={"message": {"raw": raw}})
        else:
            _g(uid, rt, "POST", "/messages/send", json={"raw": raw})
        return {"ok": True}
    raise ValueError("unknown action")


@router.post("/api/mail/tool")
async def mail_tool(req: Request):
    import asyncio
    if not _csrf_ok(req) or int(req.headers.get("content-length") or 0) > 20000:
        return JSONResponse({"error": "bad request"}, status_code=403)
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "Sign in with Google first."}, status_code=401)
    try:
        body = await req.json()
        action = str(body.get("action", ""))
        a = body.get("args") or {}
        if not isinstance(a, dict):
            raise ValueError("bad args")
        if action == "describe":
            return {"text": describe(str(a.get("action")), a.get("args") or {})}
        res = await asyncio.wait_for(asyncio.to_thread(run_tool, u[0], u[2], action, a), timeout=40)
        return {"result": res}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=401)
    except ValueError as e:
        return JSONResponse({"error": str(e)[:100]}, status_code=400)
    except Exception as e:
        print("mail tool error:", type(e).__name__, str(e)[:80])
        return JSONResponse({"error": "Gmail request failed."}, status_code=502)
