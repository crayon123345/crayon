"""One-time secret intake: lets the owner (or an agent filling from a password vault) store the
OpenRouter key without it ever passing through chat or the repo. Encrypted in Postgres."""
import hmac, os, time
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
import gmail_auth

router = APIRouter()
_tries = {"n": 0}
import re
ALLOWED = {"OPENROUTER_API_KEY": re.compile(r"^sk-or-[\w-]{10,250}$"), "TELEGRAM_BOT_TOKEN": re.compile(r"^\d{6,12}:[\w-]{30,60}$"),
           "NEON_DATABASE_URL": re.compile(r"^postgresql?://[^\s]{20,400}$"), "ADMIN_TOKEN": re.compile(r"^\S{10,120}$"),
           "DISCORD_BOT_TOKEN": re.compile(r"^[A-Za-z0-9_\-.]{50,120}$"),
           "TWILIO_ACCOUNT_SID": re.compile(r"^AC[0-9a-fA-F]{32}$"),
           "TWILIO_AUTH_TOKEN": re.compile(r"^[0-9a-fA-F]{32}$"),
           "TWILIO_WA_FROM": re.compile(r"^whatsapp:\+[0-9]{8,15}$"),
           "TELEGRAM_BOT_TOKEN": re.compile(r"^[0-9]{8,12}:[A-Za-z0-9_-]{30,40}$")}
PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>Crayon setup</title>
<body style="font:16px system-ui;max-width:420px;margin:40px auto;padding:0 16px"><h3>Crayon key setup</h3>
<input id=t type=password placeholder="setup token" style="width:100%;padding:10px"><br><br>
<input id=n placeholder="key name (e.g. OPENROUTER_API_KEY)" style="width:100%;padding:10px"><br><br>
<input id=k type=password placeholder="secret value" style="width:100%;padding:10px"><br><br>
<button id=b style="padding:10px 16px">Save</button> <span id=m></span>
<script>const h=location.hash.slice(1).split('/');if(h[0])t.value=h[0];if(h[1])n.value=h[1];else n.value='OPENROUTER_API_KEY';
b.onclick=async()=>{const r=await fetch('/setup/key',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'crayon'},body:JSON.stringify({t:t.value,n:n.value,k:k.value})});m.textContent=r.ok?'saved':'failed';k.value=''}</script>"""


def _init():
    gmail_auth.db("CREATE TABLE IF NOT EXISTS cw_secrets (name TEXT PRIMARY KEY, val TEXT NOT NULL)")


def load_into_env():
    try:
        _init()
        rows = gmail_auth.db("SELECT name, val FROM cw_secrets", (), True) or []
        for n, v in rows:
            if n in ALLOWED and not os.environ.get(n):
                os.environ[n] = gmail_auth._fernet().decrypt(v.encode()).decode()
    except Exception as e:
        print("keystore load:", type(e).__name__)


@router.get("/setup/key")
def page():
    if not os.environ.get("SETUP_TOKEN"):
        return JSONResponse({"error": "not found"}, status_code=404)
    return HTMLResponse(PAGE, headers={"Cache-Control": "no-store"})


@router.post("/setup/key")
async def save(req: Request):
    tok = os.environ.get("SETUP_TOKEN", "")
    if not tok or _tries["n"] > 20 or req.headers.get("x-requested-with") != "crayon":
        return JSONResponse({"error": "no"}, status_code=403)
    _tries["n"] += 1
    try:
        b = await req.json()
        t, k, nm = str(b.get("t", "")), str(b.get("k", "")).strip(), str(b.get("n", "OPENROUTER_API_KEY"))
    except Exception:
        return JSONResponse({"error": "bad"}, status_code=400)
    if not hmac.compare_digest(t, tok) or nm not in ALLOWED or not ALLOWED[nm].match(k):
        return JSONResponse({"error": "no"}, status_code=403)
    _init()
    enc = gmail_auth._fernet().encrypt(k.encode()).decode()
    gmail_auth.db("DELETE FROM cw_secrets WHERE name=%s", (nm,))
    gmail_auth.db("INSERT INTO cw_secrets (name, val) VALUES (%s, %s)", (nm, enc))
    os.environ[nm] = k
    if nm == "NEON_DATABASE_URL":
        try:
            import psycopg
            psycopg.connect(k, connect_timeout=8).close()
            gmail_auth._DB = k
            gmail_auth._ready = False
            gmail_auth.migrate_if_needed()
        except Exception as e:
            print("neon repoint rejected:", type(e).__name__)
            return JSONResponse({"error": "Could not connect to that Postgres URL."}, status_code=400)
    if nm == "TELEGRAM_BOT_TOKEN":
        try:
            import telegram_bot
            telegram_bot.register()
        except Exception as e:
            print("tg register:", type(e).__name__)
    if nm == "DISCORD_BOT_TOKEN":
        try:
            import discord_bot
            discord_bot._token = None
            discord_bot.register()
        except Exception as e:
            print("dc register:", type(e).__name__)
    os.environ.pop("SETUP_TOKEN", None)
    return {"ok": True}
