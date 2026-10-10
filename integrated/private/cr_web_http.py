"""HTTP surface for Pages frontend. Exact origins, short-lived server sessions."""
import html
import logging
import json
import secrets
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit
import cr_config as C
import cr_web_auth as A
import cr_web_app as W


def reply(h, status, body, ctype='application/json', cors=False, extra=None):
    path=urlsplit(h.path).path
    if status>=400 and ctype=='application/json' and path in ('/web/auth/start','/web/auth/callback','/web/google/auth/start','/web/google/auth/callback'):
        nonce=secrets.token_urlsafe(24)
        message=str(body.get('error','Sign-in could not be completed.'))[:250]
        target=A.origin()+'/crayon/'
        title='Sign-in could not be completed'
        body='<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Crayon sign-in</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#fbfaf8;color:#1f1a1d;font:16px/1.6 system-ui}main{width:min(360px,calc(100vw - 64px));padding:28px;border:1px solid #ece7e2;border-radius:20px;background:white}h1{font-size:24px;line-height:1.3}p{overflow-wrap:anywhere}a{display:block;padding:12px;border-radius:10px;background:#1f1a1d;color:white;text-align:center;text-decoration:none}</style><main><p>Crayon</p><h1>'+title+'</h1><p>'+html.escape(message)+'</p><p>Your stored conversations were not changed. Return to the original Crayon tab to try again.</p><a href="'+html.escape(target,quote=True)+'">Return to Crayon</a></main><script nonce="'+nonce+'">if(window.opener){window.opener.postMessage('+json.dumps({'type':'crayon-login-error','error':message}).replace('<','\\u003c')+','+json.dumps(A.origin())+');}</script></html>'
        ctype='text/html'
        extra={**(extra or {}),'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-"+nonce+"'; base-uri 'none'; frame-ancestors 'none'"}
        logging.getLogger('crayon.auth').warning('web_auth_failure path=%s status=%s category=%s',path,status,'provider_query' if message.startswith('Google returned') else 'auth_validation')
    data = json.dumps(body).encode() if ctype == 'application/json' else body.encode()
    h.send_response(status)
    h.send_header('Content-Type',ctype)
    h.send_header('Content-Length',str(len(data)))
    h.send_header('Cache-Control','no-store')
    h.send_header('Referrer-Policy','no-referrer')
    h.send_header('X-Content-Type-Options','nosniff')
    h.send_header('X-Frame-Options','DENY')
    if cors:
        h.send_header('Access-Control-Allow-Origin',A.origin())
        h.send_header('Vary','Origin')
    for k,v in (extra or {}).items(): h.send_header(k,v)
    h.end_headers()
    h.wfile.write(data)


def handle(h, method, raw=b''):
    p=urlsplit(h.path)
    if not p.path.startswith('/web/'):
        return False
    origin=h.headers.get('Origin')
    try:
        if method=='GET' and p.path=='/web/auth/start':
            q=parse_qs(p.query)
            if set(q)=={'merge_state'} and len(q['merge_state'])==1:url,cookie=A.start_merge(q['merge_state'][0])
            elif set(q)=={'challenge'} and len(q['challenge'])==1:url,cookie=A.begin(q['challenge'][0])
            else:raise ValueError('Invalid login start.')
            reply(h,302,'','text/plain',extra={'Location':url,'Set-Cookie':'crayon_oidc='+cookie+'; Path=/web/auth; Max-Age=300; Secure; HttpOnly; SameSite=Lax'})
            return True
        if method=='GET' and p.path=='/web/auth/callback':
            q=parse_qs(p.query)
            cookies=SimpleCookie(h.headers.get('Cookie',''))
            if 'error' in q: raise A.AuthError('Telegram login was cancelled.')
            if set(q)!={'code','state'} or any(len(v)!=1 for v in q.values()): raise A.AuthError('Invalid login response.')
            code=A.callback(q['state'][0],q['code'][0],cookies['crayon_oidc'].value if 'crayon_oidc' in cookies else '')
            nonce=secrets.token_urlsafe(24)
            body='<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Crayon login</title><p>Login checked. Return to the original Crayon tab to finish signing in.</p><script nonce="'+nonce+'">if(window.opener){window.opener.postMessage('+json.dumps({'type':'crayon-login','code':code})+','+json.dumps(A.origin())+');window.close();}</script>'
            reply(h,200,body,'text/html',extra={'Set-Cookie':'crayon_oidc=; Path=/web/auth; Max-Age=0; Secure; HttpOnly; SameSite=Lax', 'Content-Security-Policy':"default-src 'none'; script-src 'nonce-"+nonce+"'; base-uri 'none'; frame-ancestors 'none'"})
            return True
        if method=='GET' and p.path in ('/web/google/auth/start','/web/google/auth/callback'):
            import cr_web_google_auth as GA
            if len(p.query)>16384:raise A.AuthError('Sign-in response was too large. Try again from Crayon.')
            q=parse_qs(p.query,keep_blank_values=True,max_num_fields=64)
            if p.path.endswith('/start'):
                if set(q)!={'state'} or len(q['state'])!=1:raise A.AuthError('Invalid Google login start.')
                url,cookie=GA.start(q['state'][0])
                reply(h,302,'','text/plain',extra={'Location':url,'Set-Cookie':'crayon_google_oidc='+cookie+'; Path=/web/google/auth; Max-Age=300; Secure; HttpOnly; SameSite=Lax'})
            else:
                if 'error' in q:raise A.AuthError('Google returned a sign-in error. No account was linked. Try again from Crayon.')
                if any(len(q.get(k,[]))!=1 or not q[k][0] for k in ('code','state')):raise A.AuthError('Google sign-in response was incomplete. Try again from Crayon.')
                cookies=SimpleCookie(h.headers.get('Cookie',''));GA.callback(q['state'][0],q['code'][0],cookies['crayon_google_oidc'].value if 'crayon_google_oidc' in cookies else '')
                nonce=secrets.token_urlsafe(24)
                page='<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Crayon Google login</title><p>Google identity checked. Return to the original Crayon tab to finish signing in or review the account link.</p><script nonce="'+nonce+'">if(window.opener){window.opener.postMessage({type:"crayon-login"},'+json.dumps(A.origin())+');window.close();}</script>'
                reply(h,200,page,'text/html',extra={'Set-Cookie':'crayon_google_oidc=; Path=/web/google/auth; Max-Age=0; Secure; HttpOnly; SameSite=Lax','Content-Security-Policy':"default-src 'none'; script-src 'nonce-"+nonce+"'; base-uri 'none'; frame-ancestors 'none'"})
            return True
        # Same-origin public status has no private contents. Cross-origin API requires exact origin.
        if origin != A.origin():
            reply(h,403,{'error':'Origin not allowed.'})
            return True
        if method=='OPTIONS':
            reply(h,204,'','text/plain',True,{'Access-Control-Allow-Methods':'GET, POST, OPTIONS','Access-Control-Allow-Headers':'Authorization, Content-Type','Access-Control-Max-Age':'600'})
            return True
        if method=='GET' and p.path=='/web/status':
            reply(h,200,{'stage':'private foundation','login_configured':A.configured(),'google_login_configured':__import__('cr_web_google_auth').configured(),'email_login_configured':C.env('CRAYON_EMAIL_AUTH_ENABLED')=='on','rooms':True,'external_actions':'exact web review only','uploads':True,'computer':True,'notifications':True,'work_queue':True},cors=True)
            return True
        if method=='GET' and p.path=='/web/email/config':
            reply(h,200,__import__('cr_web_email_auth').config(),cors=True)
            return True
        if method=='POST':
            if h.headers.get('Content-Type','').split(';')[0]!='application/json' or len(raw)>(28000000 if p.path=='/web/upload' else 40000):
                raise ValueError('JSON body required; request size limit exceeded.')
            body=json.loads(raw)
            if not isinstance(body,dict): raise ValueError('Invalid JSON body.')
        else: body={}
        if method=='POST' and p.path=='/web/session':
            if set(body)!={'code','verifier'}: raise ValueError('Invalid exchange.')
            reply(h,200,A.exchange(body['code'],body['verifier']),cors=True)
            return True
        if method=='POST' and p.path=='/web/login-poll':
            if set(body)!={'verifier'}: raise ValueError('Invalid login poll.')
            reply(h,200,A.poll_login(body['verifier']),cors=True)
            return True
        if method=='POST' and p.path=='/web/google/login-start':
            if set(body)!={'challenge'}:raise ValueError('Invalid Google login request.')
            reply(h,200,{'url':__import__('cr_web_google_auth').begin(body['challenge'])},cors=True)
            return True
        if method=='POST' and p.path=='/web/email/session':
            reply(h,200,__import__('cr_web_email_auth').exchange(body),cors=True)
            return True
        user=A.session(h.headers.get('Authorization',''))
        if p.path=='/web/proactive':
            import cr_proactive as P
            if method=='GET':result=P.web_settings(user['user_id'])
            elif method=='POST':result=P.web_update(user['user_id'],body)
            else:raise ValueError('Invalid settings action.')
            reply(h,200,result,cors=True)
        elif p.path=='/web/mcp':
            import cr_mcp_user as U
            uid=user['user_id']
            if method=='GET':result={'connectors':U.connectors(uid),'notice':U.NOTICE}
            elif method=='POST' and set(body)=={'action','name','url','accept'} and body['action']=='add':result=U.add(uid,body['name'],body['url'],body['accept'])
            elif method=='POST' and set(body)=={'action','name'} and body['action']=='remove':result=U.remove(uid,body['name'])
            elif method=='POST' and set(body)=={'action','review_id','decision'} and body['action']=='confirm':result=U.confirm(uid,body['review_id'],body['decision'])
            else:raise ValueError('Invalid connector action.')
            reply(h,200,result,cors=True)
        elif p.path.startswith('/web/rooms'):
            import cr_web_rooms as R
            uid=user['user_id']
            if method=='GET' and p.path=='/web/rooms':result={'rooms':R.list_for(uid),'notice':R.AUDIENCE}
            elif method=='POST' and p.path=='/web/rooms/create' and set(body)=={'name','accept'}:result=R.create(uid,body['name'],body['accept'])
            elif method=='POST' and p.path=='/web/rooms/inspect' and set(body)=={'invite'}:result=R.inspect(body['invite'])
            elif method=='POST' and p.path=='/web/rooms/join' and set(body)=={'invite','accept'}:result=R.join(uid,body['invite'],body['accept'])
            elif method=='POST' and p.path=='/web/rooms/history' and set(body)=={'room'}:result={'messages':R.history(uid,body['room'])}
            elif method=='POST' and p.path=='/web/rooms/say' and set(body)=={'room','text'}:result=R.say(uid,body['room'],body['text'])
            elif method=='POST' and p.path=='/web/rooms/leave' and set(body)=={'room'}:result=R.leave(uid,body['room'])
            elif method=='POST' and p.path=='/web/rooms/delete' and set(body)=={'room','accept'}:result=R.delete(uid,body['room'],body['accept'])
            else:raise ValueError('Invalid room action.')
            reply(h,200,result,cors=True)
        elif p.path.startswith('/web/account-merge/'):

            if C.env('CRAYON_ACCOUNT_MERGE_ENABLED')!='on':raise ValueError('Reviewed account consolidation is not enabled yet. Nothing moved.')
            import cr_account_merge as M
            if method!='POST':raise ValueError('Invalid account merge method.')
            if p.path=='/web/account-merge/start':
                if set(body)!={'challenge'}:raise ValueError('Invalid merge start.')
                url,cookie=A.begin(body['challenge'],user,h.headers.get('Authorization',''))
                reply(h,200,{'url':url},cors=True)
            elif p.path=='/web/account-merge/poll':
                if set(body)!={'verifier'}:raise ValueError('Invalid merge poll.')
                reply(h,200,M.poll_verified_review(user,h.headers.get('Authorization',''),body['verifier']),cors=True)
            elif p.path=='/web/account-merge/confirm':reply(h,200,M.confirm_verified_review(user,h.headers.get('Authorization',''),body),cors=True)
            else:raise ValueError('Invalid merge route.')
        elif method=='POST' and p.path=='/web/email/link-preview':
            reply(h,200,__import__('cr_web_email_auth').link_preview(user,h.headers.get('Authorization',''),body),cors=True)
        elif method=='POST' and p.path=='/web/email/link-confirm':
            reply(h,200,__import__('cr_web_email_auth').link_confirm(user,h.headers.get('Authorization',''),body),cors=True)
        elif method=='POST' and p.path=='/web/google/link-start':
            if set(body)!={'challenge'}:raise ValueError('Invalid Google link request.')
            reply(h,200,{'url':__import__('cr_web_google_auth').begin(body['challenge'],user,h.headers.get('Authorization',''))},cors=True)
        elif method=='POST' and p.path=='/web/google/link-poll':
            if set(body)!={'verifier'}:raise ValueError('Invalid Google link poll.')
            reply(h,200,__import__('cr_web_google_auth').review(user,h.headers.get('Authorization',''),body['verifier']),cors=True)
        elif method=='POST' and p.path=='/web/google/link-confirm':
            reply(h,200,__import__('cr_web_google_auth').confirm(user,h.headers.get('Authorization',''),body),cors=True)
        elif method=='POST' and p.path=='/web/logout':
            A.logout(h.headers.get('Authorization',''))
            reply(h,200,{'ok':True},cors=True)
        elif method=='GET' and p.path=='/web/me':
            reply(h,200,{'id':user['user_id'],'name':user['name'],'expires_at':user['expires_at'].timestamp()},cors=True)
        elif method=='GET' and p.path=='/web/history':
            q=parse_qs(p.query)
            if set(q)-{'before'} or any(len(v)!=1 for v in q.values()):raise ValueError('Invalid history query.')
            reply(h,200,W.history(user['user_id'],q.get('before',[None])[0]),cors=True)
        elif method=='GET' and p.path=='/web/notifications':
            reply(h,200,{'notifications':__import__('cr_web_notifications').list_for(user['user_id'])},cors=True)
        elif method=='POST' and p.path=='/web/notification-seen':
            if set(body)!={'id'}:raise ValueError('Invalid notification acknowledgement.')
            __import__('cr_web_notifications').acknowledge(user['user_id'],body['id'])
            reply(h,200,{'ok':True},cors=True)
        elif method=='GET' and p.path=='/web/conversations':
            reply(h,200,{'conversations':__import__('cr_conversations').list_for(user['user_id'])},cors=True)
        elif method=='POST' and p.path=='/web/conversation-create':
            if set(body)!={'title'}:raise ValueError('Invalid conversation request.')
            reply(h,200,__import__('cr_conversations').create(user['user_id'],body['title']),cors=True)
        elif method=='GET' and p.path=='/web/conversation-history':
            q=parse_qs(p.query)
            if set(q)!={'id'} or len(q['id'])!=1:raise ValueError('Invalid conversation query.')
            reply(h,200,__import__('cr_conversations').history(user['user_id'],q['id'][0]),cors=True)
        elif method=='GET' and p.path=='/web/activity':
            reply(h,200,{'activity':W.activity(user['user_id'])},cors=True)
        elif method=='GET' and p.path=='/web/connections':
            reply(h,200,__import__('cr_web_actions').connections(user['user_id']),cors=True)
        elif method=='POST' and p.path in ('/web/action-preview','/web/compose-preview','/web/action-confirm','/web/private-read','/web/connect'):
            import cr_web_actions as X
            if p.path=='/web/compose-preview':result=X.compose_preview(user['user_id'],h.headers.get('Authorization',''),body)
            elif p.path=='/web/action-preview':result=X.preview(user['user_id'],h.headers.get('Authorization',''),body)
            elif p.path=='/web/action-confirm':result=X.confirm(user['user_id'],h.headers.get('Authorization',''),body)
            elif p.path=='/web/private-read':result=X.read(user['user_id'],body,h.headers.get('Authorization',''))
            else:result=X.connect(user['user_id'],body)
            reply(h,200,result,cors=True)
        elif method=='POST' and p.path=='/web/upload':
            reply(h,202,W.upload(user,body),cors=True)
        elif method=='POST' and p.path=='/web/chat':
            reply(h,202,W.submit(user,body),cors=True)
        elif method=='GET' and p.path=='/web/result':
            q=parse_qs(p.query)
            if set(q)!={'id'} or len(q['id'])!=1: raise ValueError('Invalid result request.')
            reply(h,200,W.result(user['user_id'],q['id'][0]),cors=True)
        else:
            reply(h,404,{'error':'Web feature not enabled.'},cors=True)
    except __import__('cr_google').GoogleError as e:
        reply(h,400,{'error':str(e)[:250]},cors=origin==A.origin())
    except A.AuthError as e:
        reply(h,401,{'error':str(e)},cors=origin==A.origin())
    except (UnicodeError, json.JSONDecodeError):
        reply(h,503,{'error':'Telegram response could not be checked. Start login again.'},cors=origin==A.origin())
    except ValueError as e:
        reply(h,400,{'error':str(e)[:200]},cors=origin==A.origin())
    except Exception:
        reply(h,503,{'error':'Web request stopped. No outcome confirmed.'},cors=origin==A.origin())
    return True
