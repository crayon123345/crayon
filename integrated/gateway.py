"""Single public origin, separate runtime namespaces. No secrets to client."""
import os, pathlib, subprocess, sys, time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import http.client
ROOT=pathlib.Path(__file__).resolve().parent
PRIVATE_PREFIXES=('/web/','/connections/','/google/','/computer/')
HOP={'connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade'}
def route(path):
    p=path.split('?',1)[0]
    if p=='/':return 'assets'
    if p=='/integration':return 'hub'
    if p in ('/index.html','/app.js','/exports.js','/data-cards.js','/style.css','/crayon.svg','/favicon.svg') or p.startswith('/vendor/'):return 'assets'
    if p=='/integration-status':return 'inventory'
    if p=='/studio':return 'studio'
    if p=='/workspace':return 'redirect'
    if p.startswith('/workspace/'):return 'assets'
    if p.startswith(PRIVATE_PREFIXES):return 'private'
    if p in ('/selftest','/tick','/admin-test','/form-fixture','/whatsapp/webhook','/telegram/webhook','/setup/key','/admin'):return 'blocked'
    return 'public'
class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    def local(self,status,body,ctype='text/plain',extra=None):
        b=body.encode();self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(b)))
        self.send_header('Cache-Control','no-store')
        for k,v in (extra or {}).items():self.send_header(k,v)
        self.end_headers();self.wfile.write(b)
    def handle_request(self):
        target=route(self.path)
        if target=='inventory':return self.local(200,(ROOT/'features.json').read_text(),'application/json')
        if target=='hub':return self.local(200,(ROOT/'index.html').read_text(),'text/html')
        if target=='studio':self.path='/';target='public'
        if target=='blocked':return self.local(404,'Not available in integrated web mode')
        if target=='redirect':return self.local(302,'',extra={'Location':'/workspace/'})
        if target=='assets':
            from urllib.parse import unquote
            root=ROOT/'private'/'docs';name=unquote(self.path.split('?',1)[0]);name=(name[len('/workspace/'):] if name.startswith('/workspace/') else name.lstrip('/')) or 'index.html'
            p=(root/name).resolve()
            if not p.is_relative_to(root.resolve()) or not p.is_file():return self.local(404,'Not found')
            import mimetypes
            b=p.read_bytes();self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(p.name)[0] or 'application/octet-stream');self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(b);return
        try:n=int(self.headers.get('Content-Length','0'))
        except ValueError:return self.local(400,'Invalid size')
        if n<0 or n>28000000:return self.local(413,'Too large')
        body=self.rfile.read(n) if n else None
        port=int(os.environ.get('INTEGRATED_PRIVATE_PORT','10002') if target=='private' else os.environ.get('INTEGRATED_PUBLIC_PORT','10001'))
        started_response=False
        c=http.client.HTTPConnection('127.0.0.1',port,timeout=150)
        headers={k:v for k,v in self.headers.items() if k.lower() not in HOP|{'host'}}
        headers['Host']=self.headers.get('Host','localhost');headers['X-Forwarded-For']=self.client_address[0]
        try:
            c.request(self.command,self.path,body=body,headers=headers);r=c.getresponse();self.send_response(r.status);started_response=True
            for k,v in r.getheaders():
                if k.lower() not in HOP:self.send_header(k,v)
            self.send_header('Connection','close');self.end_headers();self.close_connection=True
            while True:
                chunk=r.read1(65536)
                if not chunk:break
                self.wfile.write(chunk);self.wfile.flush()
        except (OSError,http.client.HTTPException):
            if not started_response:self.local(503,'Feature engine is starting or unavailable. No action completed.')
            self.close_connection=True
        finally:c.close()
    do_GET=do_POST=do_OPTIONS=do_DELETE=handle_request

def main():
    env=os.environ.copy();origin=env.get('INTEGRATED_ORIGIN','')
    if not origin.startswith('https://') and not origin.startswith('http://localhost'):raise SystemExit('Set exact INTEGRATED_ORIGIN first')
    # Runtime-only env isolation. Never start duplicate bot consumers or migrate source DBs.
    public=env.copy();public.update(CRAYON_INTEGRATED='on',PUBLIC_URL=origin,PORT=env.get('INTEGRATED_PUBLIC_PORT','10001'))
    for key in ['TELEGRAM_BOT_TOKEN','WHATSAPP_ACCESS_TOKEN','TWILIO_AUTH_TOKEN','DISCORD_BOT_TOKEN','DISCORD_TOKEN','GOOGLE_CLIENT_SECRET','GOOGLE_CLIENT_ID','DATABASE_URL','NEON_DATABASE_URL']:
        public.pop(key,None)
    private=env.copy();private.update(CRAYON_INTEGRATED_WEB_ONLY='on',CRAYON_PUBLIC_URL=origin,CRAYON_WEB_ORIGIN=origin,PORT=env.get('INTEGRATED_PRIVATE_PORT','10002'))
    processes=[subprocess.Popen([sys.executable,'-m','uvicorn','app:app','--host','127.0.0.1','--port',public['PORT']],cwd=ROOT/'public',env=public),subprocess.Popen([sys.executable,'main.py'],cwd=ROOT/'private',env=private)]
    try:ThreadingHTTPServer(('0.0.0.0',int(env.get('PORT','10000'))),Handler).serve_forever()
    finally:
        for p in processes:p.terminate()
if __name__=='__main__':main()
