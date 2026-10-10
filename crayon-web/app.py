import json, os, time, uuid, asyncio
from collections import OrderedDict, defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import llm
import gmail_auth
import keystore
import memory
import telegram_bot
import discord_bot
import twilio_wa
import d360_wa
import voice

BASE = Path(__file__).parent
MAX_MSG = 9000
MAX_TURNS = 16            # kept per visitor
MAX_SESSIONS = 2000
PER_IP_PER_MIN = int(os.environ.get("RATE_PER_MIN", "24"))
PER_IP_PER_DAY = int(os.environ.get("RATE_IP_DAY", "200"))
MAX_BODY = 30000
MAX_STREAMS = int(os.environ.get("MAX_STREAMS", "8"))
GLOBAL_PER_DAY = int(os.environ.get("RATE_GLOBAL_DAY", "3000"))

app = FastAPI(title="Crayon", docs_url=None, redoc_url=None)
import shutil
_S = BASE / "static"
if not _S.exists():
    _S.mkdir()
    for _n in ("index.html", "style.css", "app.js", "crayon.svg", "favicon.svg", "pyworker.js", "manifest.webmanifest", "sw.js", "privacy.html", "icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"):
        if (BASE / _n).exists():
            shutil.copy(BASE / _n, _S / _n)
app.include_router(gmail_auth.router)
app.include_router(keystore.router)
app.include_router(memory.router)
app.include_router(telegram_bot.router)
app.include_router(twilio_wa.router)
app.include_router(d360_wa.router)
app.include_router(voice.router)
keystore.load_into_env()
if os.environ.get("NEON_DATABASE_URL"):
    try:
        import psycopg
        psycopg.connect(os.environ["NEON_DATABASE_URL"], connect_timeout=8).close()
        gmail_auth._DB = os.environ["NEON_DATABASE_URL"]
        gmail_auth._ready = False
    except Exception as e:
        print("neon boot check failed, staying on DATABASE_URL:", type(e).__name__)
gmail_auth.migrate_if_needed()
import threading
threading.Thread(target=lambda: telegram_bot.register() if telegram_bot.token() else None, daemon=True).start()
threading.Thread(target=lambda: discord_bot.register() if discord_bot.token() else None, daemon=True).start()
app.mount("/static", StaticFiles(directory=_S), name="static")

sessions: "OrderedDict[str, list]" = OrderedDict()
hits = defaultdict(deque)
ip_day: dict = {}
active = {"n": 0}
day = {"d": time.strftime("%Y-%m-%d"), "n": 0}
_model = None


_models: dict = {}


def model(name=None):
    key = name or "_default"
    if key not in _models:
        _models[key] = llm.build_model(name=name) if name else llm.build_model()
    return _models[key]


def client_ip(req: Request) -> str:
    xf = req.headers.get("x-forwarded-for", "")
    return (xf.split(",")[0].strip() if xf else (req.client.host if req.client else "?"))


def limited(ip: str) -> str | None:
    now = time.time()
    q = hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= PER_IP_PER_MIN:
        return "You're sending messages fast. Give it a few seconds."
    today = time.strftime("%Y-%m-%d")
    if day["d"] != today:
        day["d"], day["n"] = today, 0
    rec = ip_day.get(ip)
    if not rec or rec[0] != today:
        if len(ip_day) > 20000:
            ip_day.clear()
        rec = ip_day[ip] = [today, 0]
    if rec[1] >= PER_IP_PER_DAY:
        return "You've hit today's free limit for one visitor. Come back tomorrow."
    if day["n"] >= GLOBAL_PER_DAY:
        return "Crayon is out of free capacity for today. Try again tomorrow."
    q.append(now)
    day["n"] += 1
    rec[1] += 1
    return None


@app.middleware("http")
async def sec_headers(request: Request, call_next):
    r = await call_next(request)
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "no-referrer"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return r


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/sw.js")
def sw():
    return FileResponse(BASE / "static" / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(BASE / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/privacy")
def privacy():
    return FileResponse(BASE / "static" / "privacy.html")


@app.get("/.well-known/assetlinks.json")
def assetlinks():
    p = BASE / "static" / "assetlinks.json"
    if p.exists():
        return FileResponse(p, media_type="application/json")
    return JSONResponse([], status_code=200)


@app.get("/health")
def health():
    return {"ok": True, "provider": llm.provider()}


@app.post("/api/chat")
async def chat(req: Request):
    if int(req.headers.get("content-length") or 0) > MAX_BODY:
        return JSONResponse({"error": "Request too large."}, status_code=413)
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    text = str(body.get("message", "")).strip()
    sid = str(body.get("session_id") or "")[:64] or uuid.uuid4().hex
    vid0, _nv0 = memory.identity(req)
    skey = vid0 + ":" + sid  # cache is per-visitor: a sid alone must never cross visitors
    if not text:
        return JSONResponse({"error": "empty message"}, status_code=400)
    if len(text) > MAX_MSG:
        return JSONResponse({"error": f"Message too long (max {MAX_MSG} characters)."}, status_code=400)
    msg = limited(client_ip(req))
    if msg:
        return JSONResponse({"error": msg}, status_code=429)

    if active["n"] >= MAX_STREAMS:
        return JSONResponse({"error": "Crayon is busy right now. Try again in a few seconds."}, status_code=503)
    mail_on = False
    try:
        mail_on = bool(body.get("mail")) and gmail_auth.current_user(req) is not None
    except Exception:
        mail_on = False
    agent_on = bool(body.get("agent"))
    vid, new_vid = memory.identity(req)
    hist = sessions.get(skey)
    if hist is None:
        hist = await asyncio.to_thread(memory.load_context, vid)
    sessions[skey] = hist
    sessions.move_to_end(skey)
    while len(sessions) > MAX_SESSIONS:
        sessions.popitem(last=False)
    assert skey.startswith(vid + ":"), "visitor/session key mismatch"
    asyncio.create_task(asyncio.to_thread(memory.event, vid, "agent" if agent_on else "chat"))

    async def gen():
        out = []
        active["n"] += 1
        try:
            used_model = None
            steps = llm.plan(text) if llm.provider() == "gemini" else [("gemini", None, "default")]
            last = None
            for kind, nm, lab in steps:
                try:
                    mdl = llm.build_or(nm) if kind == "or" else model(nm)
                    used_model = nm
                    sent_label = False
                    async for ch in mdl.astream(llm.to_messages(hist[-MAX_TURNS * 2:], text, mail_on, agent_on)):
                        tt = llm.chunk_text(ch)
                        if tt:
                            if not sent_label:
                                sent_label = True
                                yield f"data: {json.dumps({'m': llm.label(lab)})}\n\n"
                            out.append(tt)
                            yield f"data: {json.dumps({'t': tt})}\n\n"
                    last = None
                    break
                except Exception as e2:
                    last = e2
                    print("model fail:", kind, nm, type(e2).__name__)
                    if out:
                        break
            if last is not None and not out:
                raise last
            full = "".join(out).strip()
            if not full:
                yield f"data: {json.dumps({'t': 'I got an empty answer. Try rephrasing?'})}\n\n"
            else:
                hist.append(("user", text))
                hist.append(("ai", full))
                del hist[:-MAX_TURNS * 2]
                await asyncio.to_thread(memory.save_turn, vid, text, full)
                await asyncio.to_thread(memory.save_chat_turn, vid, sid, text, full)
                asyncio.create_task(asyncio.to_thread(memory.event, vid, "model:" + str(used_model)))
        except Exception as e:  # never leak keys or internals
            print("llm error:", type(e).__name__, str(e)[:200].replace(os.environ.get("GEMINI_API_KEY", "#"), "***"))
            yield f"data: {json.dumps({'err': 'Crayon hit a snag talking to its brain. Please try again in a moment.'})}\n\n"
        finally:
            active["n"] = max(0, active["n"] - 1)
        yield "data: [DONE]\n\n"

    resp = StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    if new_vid:
        memory.set_vid_cookie(resp, vid)
    return resp


_scache: dict = {}
_stcache: dict = {}


def _do_search(q: str):
    from concurrent.futures import ThreadPoolExecutor
    from ddgs import DDGS

    def get_news():
        for _ in range(2):
            try:
                return [{"title": str(r.get("title", ""))[:140], "url": str(r.get("url", ""))[:300], "snippet": str(r.get("body", ""))[:300], "date": str(r.get("date", ""))[:10]}
                        for r in DDGS(timeout=8).news(q, max_results=6, safesearch="moderate")]
            except Exception as e:
                print("search news error:", type(e).__name__)
        return None

    def get_text():
        for _ in range(2):
            try:
                return [{"title": str(r.get("title", ""))[:140], "url": str(r.get("href", ""))[:300], "snippet": str(r.get("body", ""))[:300], "date": ""}
                        for r in DDGS(timeout=8).text(q, max_results=6, region="wt-wt", safesearch="moderate")]
            except Exception as e:
                print("search text error:", type(e).__name__)
        return None

    with ThreadPoolExecutor(2) as ex:
        fn, ft = ex.submit(get_news), ex.submit(get_text)
        news, text = fn.result(), ft.result()
    ok = news is not None and text is not None
    news, text = news or [], text or []
    seen, res = set(), []
    for r in news[:5] + text + news[5:]:
        if r["url"].startswith(("http://", "https://")) and r["url"] not in seen:
            seen.add(r["url"])
            res.append(r)
    return res[:9], ok


@app.post("/api/search")
async def search(req: Request):
    if int(req.headers.get("content-length") or 0) > 2000:
        return JSONResponse({"error": "Request too large."}, status_code=413)
    try:
        body = await req.json()
        q = str(body.get("q", "")).strip()[:200]
        focus = str(body.get("focus", "")).strip().lower()[:12]
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not q:
        return JSONResponse({"error": "empty query"}, status_code=400)
    msg = limited(client_ip(req))
    if msg:
        return JSONResponse({"error": msg}, status_code=429)
    v0, n0 = memory.identity(req)
    if not n0:
        asyncio.create_task(asyncio.to_thread(memory.event, v0, "search"))
    now = time.time()
    hit = _scache.get(q.lower())
    if hit and now - hit[0] < 300:
        return {"results": hit[1]}
    try:
        fq = q
        if focus == "deep":
            from concurrent.futures import ThreadPoolExecutor
            variants = [q, q + " in-depth analysis", q + " latest developments"]
            with ThreadPoolExecutor(3) as ex:
                parts = list(ex.map(_do_search, variants))
            seen2, merged, anyok = set(), [], False
            for res2, ok2 in parts:
                anyok = anyok or ok2
                for r2 in res2:
                    if r2["url"] not in seen2:
                        seen2.add(r2["url"])
                        merged.append(r2)
            res, ok = merged[:15], anyok
            if ok and len(res) >= 3:
                if len(_scache) > 500:
                    _scache.clear()
                _scache[q.lower()] = (now, res)
            return {"results": res}
        if focus == "academic":
            fq = q + " (site:edu OR site:gov OR site:ac.in OR site:nature.com OR site:sciencedirect.com)"
        elif focus == "social":
            fq = q + " (site:reddit.com OR site:quora.com OR site:x.com)"
        elif focus == "video":
            fq = q + " site:youtube.com"
        res, ok = await asyncio.wait_for(asyncio.to_thread(_do_search, fq), timeout=30)
    except Exception:
        return JSONResponse({"error": "Search is unavailable right now."}, status_code=503)
    if ok and len(res) >= 3:
        if len(_scache) > 500:
            _scache.clear()
        _scache[q.lower()] = (now, res)
    return {"results": res}


import ipaddress, socket
from urllib.parse import urlparse, urljoin
import httpx

_fcache = OrderedDict()


def _safe_url(u: str) -> bool:
    try:
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            return False
        if p.port not in (None, 80, 443):
            return False
        for info in socket.getaddrinfo(p.hostname, p.port or 443, proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                return False
        return True
    except Exception:
        return False


def _do_fetch(url: str):
    import trafilatura
    cur = url
    with httpx.Client(timeout=httpx.Timeout(8.0), follow_redirects=False,
                      headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 CrayonReader/1.0", "Accept": "text/html,text/plain;q=0.9"}) as c:
        for _ in range(4):
            if not _safe_url(cur):
                return None, "That address can't be fetched."
            with c.stream("GET", cur) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    cur = urljoin(cur, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    return None, "The site returned an error (%d)." % r.status_code
                ct = r.headers.get("content-type", "").lower()
                if not any(t in ct for t in ("text/html", "text/plain", "application/xhtml")):
                    return None, "Not a readable web page."
                buf = b""
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > 1_500_000:
                        break
                html = buf.decode(r.encoding or "utf-8", "replace")
                break
        else:
            return None, "Too many redirects."
    if "text/plain" in ct:
        text, title = html, ""
    else:
        text = trafilatura.extract(html, include_links=False, include_comments=False, favor_recall=True) or ""
        m = trafilatura.extract_metadata(html)
        title = (m.title if m and m.title else "")
    text = text.strip()
    if not text:
        return None, "Couldn't read any text from that page."
    return {"url": cur, "title": title[:200], "text": text[:9000]}, None


@app.post("/api/fetch")
async def fetch_page(req: Request):
    if int(req.headers.get("content-length") or 0) > 2000:
        return JSONResponse({"error": "Request too large."}, status_code=413)
    try:
        url = str((await req.json()).get("url", "")).strip()[:600]
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not url:
        return JSONResponse({"error": "empty url"}, status_code=400)
    msg = limited(client_ip(req))
    if msg:
        return JSONResponse({"error": msg}, status_code=429)
    v0, n0 = memory.identity(req)
    if not n0:
        asyncio.create_task(asyncio.to_thread(memory.event, v0, "fetch"))
    hit = _fcache.get(url)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        res, err = await asyncio.wait_for(asyncio.to_thread(_do_fetch, url), timeout=20)
    except Exception:
        return JSONResponse({"error": "Couldn't open that page."}, status_code=502)
    if err:
        return JSONResponse({"error": err}, status_code=422)
    if len(_fcache) > 100:
        _fcache.clear()
    _fcache[url] = (time.time(), res)
    return res


WMO = {0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Icy fog",
       51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
       66: "Freezing rain", 67: "Heavy freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
       77: "Snow grains", 80: "Light showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers",
       86: "Heavy snow showers", 95: "Thunderstorm", 96: "Storm with hail", 99: "Storm with heavy hail"}


@app.get("/api/weather")
async def weather(req: Request, q: str = ""):
    q = q.strip()[:80]
    if not q:
        return JSONResponse({"error": "empty place"}, status_code=400)
    msg = limited(client_ip(req))
    if msg:
        return JSONResponse({"error": msg}, status_code=429)
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            g = (await c.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": q, "count": 1, "language": "en"})).json()
            if not g.get("results"):
                return JSONResponse({"error": "Place not found."}, status_code=404)
            p = g["results"][0]
            w = (await c.get("https://api.open-meteo.com/v1/forecast",
                             params={"latitude": p["latitude"], "longitude": p["longitude"],
                                     "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                                     "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                                     "timezone": "auto", "forecast_days": 3})).json()
        cur, d = w.get("current", {}), w.get("daily", {})
        days = []
        for i in range(min(3, len(d.get("time", [])))):
            days.append({"date": d["time"][i], "max": d["temperature_2m_max"][i], "min": d["temperature_2m_min"][i],
                         "rain": (d.get("precipitation_probability_max") or [None] * 3)[i]})
        return {"place": p.get("name", q) + (", " + p["country"] if p.get("country") else ""),
                "temp": cur.get("temperature_2m"), "feels": cur.get("apparent_temperature"),
                "humidity": cur.get("relative_humidity_2m"), "wind": cur.get("wind_speed_10m"),
                "cond": WMO.get(cur.get("weather_code"), "Unknown"), "days": days, "source": "open-meteo.com"}
    except Exception as e:
        print("weather:", type(e).__name__)
        return JSONResponse({"error": "Weather is unavailable right now."}, status_code=503)


@app.get("/api/fx")
async def fx(req: Request, pair: str = ""):
    import re as _re
    m = _re.findall(r"[A-Za-z]{3}", pair.upper())[:2]
    if len(m) != 2:
        return JSONResponse({"error": "Give a pair like USD to INR."}, status_code=400)
    base, to = m
    msg = limited(client_ip(req))
    if msg:
        return JSONResponse({"error": msg}, status_code=429)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            j = (await c.get(f"https://open.er-api.com/v6/latest/{base}")).json()
        if j.get("result") != "success" or to not in j.get("rates", {}):
            return JSONResponse({"error": "Unknown currency pair."}, status_code=404)
        return {"base": base, "to": to, "rate": j["rates"][to], "date": j.get("time_last_update_utc", "")[:16],
                "source": "open.er-api.com"}
    except Exception as e:
        print("fx:", type(e).__name__)
        return JSONResponse({"error": "Rates are unavailable right now."}, status_code=503)


@app.get("/api/stock")
async def stock(req: Request, sym: str = ""):
    import re as _re
    sym = _re.sub(r"[^A-Za-z0-9.^\-]", "", sym.strip().upper())[:12]
    if not sym:
        return JSONResponse({"error": "empty ticker"}, status_code=400)
    msg = limited(client_ip(req))
    if msg:
        return JSONResponse({"error": msg}, status_code=429)
    hit = _stcache.get(sym)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        import datetime as _dt
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}) as c:
            j = (await c.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", params={"interval": "1d", "range": "1d"})).json()
        res = (j.get("chart") or {}).get("result") or []
        if not res:
            return JSONResponse({"error": "Unknown ticker."}, status_code=404)
        meta = res[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        if price is None:
            return JSONResponse({"error": "Unknown ticker."}, status_code=404)
        ts = meta.get("regularMarketTime")
        when = _dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M UTC") if ts else ""
        out = {"symbol": meta.get("symbol", sym), "name": meta.get("shortName") or meta.get("longName") or "",
                "price": price, "prev": meta.get("chartPreviousClose"), "currency": meta.get("currency", ""),
                "exchange": meta.get("fullExchangeName") or meta.get("exchangeName") or "",
                "date": when, "source": "yahoo finance"}
        _stcache[sym] = (time.time(), out)
        return out
    except Exception as e:
        print("stock:", type(e).__name__)
        return JSONResponse({"error": "Quotes are unavailable right now."}, status_code=503)


@app.get("/api/image")
async def image(req: Request, prompt: str = ""):
    prompt = prompt.strip()[:400]
    if not prompt:
        return JSONResponse({"error": "empty prompt"}, status_code=400)
    msg = limited(client_ip(req))
    if msg:
        return JSONResponse({"error": msg}, status_code=429)
    v0, n0 = memory.identity(req)
    if not n0:
        asyncio.create_task(asyncio.to_thread(memory.event, v0, "image"))
    from urllib.parse import quote
    return RedirectResponse("https://image.pollinations.ai/prompt/" + quote(prompt) + "?width=1024&height=1024&nologo=true", status_code=302)


@app.post("/api/reset")
async def reset(req: Request):
    try:
        sid = str((await req.json()).get("session_id", ""))
    except Exception:
        sid = ""
    vid, _ = memory.identity(req)
    sessions.pop(vid + ":" + sid, None)
    await asyncio.to_thread(memory.boundary, vid)
    return {"ok": True}
