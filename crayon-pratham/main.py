"""Crayon entrypoint: health server, database, Telegram polling."""
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cr_config as C
from cr_safety import redact


class RedactFilter(logging.Filter):
    def filter(self, record):
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for h in logging.getLogger().handlers:
        h.addFilter(RedactFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


log = logging.getLogger("crayon.main")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/plain"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _admin(self):
        tok = self.headers.get("Authorization", "").replace("Bearer ", "")
        return bool(C.ADMIN_TOKEN) and tok == C.ADMIN_TOKEN

    def do_OPTIONS(self):
        import cr_web_http
        if not cr_web_http.handle(self, "OPTIONS"):
            return self._send(404,"not found")

    def do_GET(self):
        import cr_web_http
        if cr_web_http.handle(self, "GET"): return
        if self.path.split("?",1)[0]=="/whatsapp/webhook":
            from urllib.parse import parse_qs,urlsplit
            import cr_whatsapp as W
            q={k:v[0] for k,v in parse_qs(urlsplit(self.path).query).items()}
            challenge=W.verify_challenge(q) if W.enabled() else None
            return self._send(200,challenge) if challenge is not None else self._send(403,"forbidden")
        route=self.path.split('?',1)[0]
        if route.startswith('/connections/'):
            from urllib.parse import parse_qs,urlsplit
            import cr_connections as X
            parts=route.strip('/').split('/')
            if len(parts)!=3 or parts[1] not in X.PROVIDERS:return self._send(404,'not found')
            q=parse_qs(urlsplit(self.path).query)
            try:
                if parts[2]=='connect':
                    url=X.authorization_url(q.get('state',[''])[0],parts[1])
                    self.send_response(302);self.send_header('Location',url);self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','no-referrer');self.end_headers();return
                if parts[2]=='callback' and not q.get('error'):
                    X.complete(q.get('state',[''])[0],q.get('code',[''])[0],parts[1])
                    return self._send(200,'Connected. Return to your private Crayon chat and check connection status.')
                return self._send(400,'Consent cancelled or invalid route. Start again in your private chat.')
            except Exception:return self._send(400,'Connection not confirmed. Start again in your private chat.')
        if self.path.split('?',1)[0]=='/google/connect':
            from urllib.parse import parse_qs,urlsplit
            import cr_google as G
            q=parse_qs(urlsplit(self.path).query)
            try:
                location=G.authorization_url(q.get('state',[''])[0])
                self.send_response(302)
                self.send_header('Location',location)
                self.send_header('Cache-Control','no-store')
                self.send_header('Referrer-Policy','no-referrer')
                self.send_header('Content-Length','0')
                self.end_headers()
                return
            except Exception:
                return self._send(400,'<title>Crayon Google connection</title><h1>Link expired or unavailable</h1><p>Return to your private Crayon chat and type connect Google for a fresh link.</p>','text/html')
        if self.path.split("?",1)[0] == "/google/callback":
            from urllib.parse import parse_qs,urlsplit
            import cr_google as G, html
            q=parse_qs(urlsplit(self.path).query)
            try:
                if q.get("error"):
                    raise G.GoogleError("Google consent was cancelled. Start again in Telegram if you want to connect.")
                G.complete(q.get("state",[""])[0],q.get("code",[""])[0])
                return self._send(200,"<title>Crayon Google connection</title><h1>Connected</h1><p>Return to your private Crayon chat and use /google_status. If you did not request this, disconnect in Telegram.</p>","text/html")
            except Exception:
                return self._send(400,"<title>Crayon connection failed</title><h1>Connection not confirmed</h1><p>The link expired, consent failed or setup is unavailable. Start again with /connect_google in Telegram.</p>","text/html")
        if self.path in ("/privacy", "/terms"):
            from cr_policy import page
            return self._send(200,page(self.path),"text/html")
        if self.path == "/":
            return self._send(200, '<!doctype html><html><head><meta name="google-site-verification" content="dhan82Y3H9t0tn4VuTh-Bs53DELgy7XEukGcvh0LuSg"><title>Crayon</title></head><body><h1>Crayon Telegram assistant</h1><p>Memory, reminders, search and media reading on free hosting.</p><p>Google integration is in testing mode, only for named test users. Each user connects their own account.</p><p><a href="https://t.me/crayon_v1_bot">Open Crayon</a> | <a href="/privacy">Privacy</a> | <a href="/terms">Terms</a></p>',"text/html")
        if self.path=='/form-fixture':
            return self._send(200,'<!doctype html><title>Crayon controlled form test</title><h1>Controlled test only</h1><p>Free controlled test. No booking or external action.</p><form action="/form-fixture/result" method="post"><label>Test identity<input name="name" type="text" required></label><label>Test note<textarea name="note" required></textarea></label><button type="submit">Submit controlled test</button></form>','text/html')
        if self.path == "/admin-test":
            return self._send(200, """<!doctype html><title>Crayon admin test</title><h1>Captured self-test</h1>
<p>Negative synthetic users only. No Telegram sends.</p>
<form method="post" action="/admin-test"><label>Admin token <input type="password" name="token"></label>
<label>Test JSON <textarea name="body" rows="12" cols="70">{"uid":-7007,"texts":["hello"],"cleanup":true}</textarea></label>
<button>Run captured test</button></form>""", "text/html")
        if self.path.startswith("/health"):
            import cr_db as db
            info = {"ok": True, "version": C.VERSION, "db": db.available(), "mode": C.MODE}
            return self._send(200, json.dumps(info), "application/json")
        return self._send(200, "Crayon host running\n")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n<0 or n>(28000000 if self.path.split('?',1)[0]=='/web/upload' else 1500000):return self._send(413,"too large")
        if self.path.split('?',1)[0]=='/web/upload':
            import cr_web_auth as A, cr_web_http
            try:
                if self.headers.get('Origin')!=A.origin():return self._send(403,'Origin not allowed')
                A.session(self.headers.get('Authorization',''))
            except A.AuthError:
                return cr_web_http.reply(self,401,{'error':'Log in with Telegram first.'},cors=self.headers.get('Origin')==A.origin())
        raw = self.rfile.read(n) if n else b""
        import cr_web_http
        if cr_web_http.handle(self, "POST", raw): return
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        if self.path.split("?",1)[0]=="/whatsapp/webhook":
            import cr_whatsapp as W
            code=W.handle_webhook(raw,self.headers.get("X-Hub-Signature-256"))
            return self._send(code,"ok" if code==200 else "forbidden")
        if self.path in ('/computer/next','/computer/result','/computer/boot'):
            import hmac,cr_computer as K
            token=C.env('CRAYON_BRIDGE_TOKEN')
            got=self.headers.get('Authorization','').removeprefix('Bearer ')
            if not token or not hmac.compare_digest(got,token):return self._send(403,'forbidden')
            if n>1500000:return self._send(413,'too large')
            try:
                if self.path=='/computer/boot':result=K.record_boot(body)
                elif self.path=='/computer/next':result=K.next_job(body)
                else:K.complete(body['id'],body['result']);result={'ok':True}
                return self._send(200,json.dumps(result),'application/json')
            except Exception:return self._send(400,'invalid request')
        if self.path=='/form-fixture/result':
            from urllib.parse import parse_qs
            import html
            values=parse_qs(raw.decode(),keep_blank_values=True)
            if set(values)!={'name','note'} or values['name']!=['TEST ONLY'] or len(values['note'])!=1 or not values['note'][0] or len(values['note'][0])>300:return self._send(400,'Invalid test data')
            return self._send(200,'<!doctype html><title>Crayon test receipt</title><h1>Test form received</h1><p>No booking or external action was made.</p><p>'+html.escape(values['note'][0])+'</p>','text/html')
        if self.path == "/admin-test":
            from urllib.parse import parse_qs
            import html, hmac
            data = parse_qs(raw.decode())
            if not C.ADMIN_TOKEN or not hmac.compare_digest(data.get("token", [""])[0], C.ADMIN_TOKEN):
                return self._send(403, "forbidden")
            try:
                import cr_selftest
                result = cr_selftest.run(json.loads(data.get("body", ["{}"]) [0]))
                return self._send(200, "<title>Crayon self-test result</title><h1>Captured result</h1><pre>" + html.escape(json.dumps(result, indent=2, default=str)) + "</pre>", "text/html")
            except Exception:
                return self._send(500, "Test failed; inspect server logs")
        if self.path == "/selftest":
            if not self._admin():
                return self._send(403, "forbidden")
            import cr_selftest
            return self._send(200, json.dumps(cr_selftest.run(body), default=str), "application/json")
        return self._send(404, "not found")

    def log_message(self, *a):
        pass


def start_server():
    port = int(os.environ.get("PORT", "10000"))
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError:
        log.info("port %s already served by the host wrapper; skipping own server", port)
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("http server on %s", port)
    return srv


def main():
    setup_logging()
    start_server()
    import cr_db as db
    import cr_telegram as tg
    try:
        db.init()
        import cr_google
        cr_google.init()
        import cr_computer
        cr_computer.init()
        import cr_whatsapp
        cr_whatsapp.init()
        import cr_connections
        cr_connections.init()
        import cr_web_app
        cr_web_app.init()
    except Exception as e:
        log.error("database init failed (running without persistence): %s", redact(str(e))[:200])
    if not C.TELEGRAM_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN missing")
        threading.Event().wait()
    tg.set_commands()
    import cr_sched
    cr_sched.start()
    import cr_whatsapp
    cr_whatsapp.start()
    tg.poll_forever()


if __name__ == "__main__":
    main()
