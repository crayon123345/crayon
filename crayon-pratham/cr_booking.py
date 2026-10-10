"""Per-account exact review for deployment-configured free public HTML forms."""
import json,secrets,hashlib,base64
import cr_db as db
import cr_google as G
import cr_computer as K
from computer_forms import validate
class BookingError(Exception):pass
def configs():
    import cr_config as C
    raw=C.env('CRAYON_PUBLIC_FORM_ADAPTERS','{}')
    try:
        adapters=json.loads(raw)
        if not isinstance(adapters,dict):raise ValueError()
        adapters['demo']={'url':C.PUBLIC_URL.rstrip('/')+'/form-fixture','action':C.PUBLIC_URL.rstrip('/')+'/form-fixture/result','fields':['name','note'],'confirmation':'Test form received','terms':'Controlled test only. No booking, calendar, external message, fee, cancellation or payment. Use TEST ONLY identity, no personal information.'}
        return adapters
    except Exception:raise BookingError('Form adapter configuration invalid') from None

def init():
    db.q("CREATE TABLE IF NOT EXISTS public_form_drafts(id TEXT PRIMARY KEY,user_id BIGINT NOT NULL,encrypted_content TEXT NOT NULL,content_hash TEXT NOT NULL,status TEXT DEFAULT 'pending',expires_at TIMESTAMPTZ NOT NULL)",fetch='none')
def gate(uid):
    if not K.permitted(uid):raise BookingError('A valid authenticated account is required.')
def store_preview(uid,config,values,result):
    gate(uid);validate(config,values)
    if not result.get('ok') or not result.get('verified') or not result.get('hash'):raise BookingError('Form inspection not confirmed')
    init();content={'config':config,'values':values,'page_hash':result['hash']}
    digest=hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest();ident=secrets.token_hex(8)
    db.q("DELETE FROM public_form_drafts WHERE user_id=%s OR expires_at<now()",(uid,),'none')
    db.q("INSERT INTO public_form_drafts(id,user_id,encrypted_content,content_hash,expires_at) VALUES(%s,%s,%s,%s,now()+interval '10 minutes')",(ident,uid,G.encrypt(uid,content),digest),'none')
    db.kv_set('form_reviewed_'+str(uid),[ident,digest[:12]])
    lines=['Public form preview only - not submitted. Expires in 10 minutes.','Page: '+config['url'],'Submission destination: '+config['action'],'Reviewed adapter terms: '+config['terms'],'Exact disclosed fields:']+[k+': '+v for k,v in values.items()]
    lines+=['Submit once only after reviewing the destination and every field. A form receipt does not itself prove a reservation. No payment, login, extra recipients or automatic retry.']
    return {'id':ident,'hash':digest[:12],'text':'\n'.join(lines),'screenshot':result.get('screenshot')}
def preview(uid,adapter,values):
    gate(uid);config=configs().get(adapter)
    if not config:raise BookingError('That provider is not enabled. Only reviewed, configured free HTML form adapters are supported. Google appointment/Calendly dynamic booking dialogs are not yet accepted.')
    validate(config,values)
    if adapter=='demo' and values.get('name')!='TEST ONLY':raise BookingError('Demo requires name TEST ONLY; do not enter personal information.')
    r=K.execute(uid,'form_inspect',{'config':config})
    return store_preview(uid,config,values,r)
def submit(uid,ident,digest):
    gate(uid);init()
    if db.kv_get('form_reviewed_'+str(uid),None)!=[ident,digest]:raise BookingError('Review the current preview first')
    row=db.q("UPDATE public_form_drafts SET status='submitting' WHERE id=%s AND user_id=%s AND status='pending' AND expires_at>now() AND left(content_hash,12)=%s RETURNING encrypted_content,content_hash",(ident,uid,digest),'one')
    if not row:raise BookingError('Preview expired, changed, or already used. Do not retry an uncertain submission.')
    try:
        content=G.decrypt(uid,row['encrypted_content'])
        if hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()!=row['content_hash']:raise BookingError('Stored preview changed')
        if content['config'] not in configs().values():raise BookingError('Adapter configuration changed')
        validate(content['config'],content['values'])
        r=K.execute(uid,'form_submit',{'config':content['config'],'values':content['values'],'expected_hash':content['page_hash']})
        db.q("UPDATE public_form_drafts SET status=%s WHERE id=%s",('confirmed' if r.get('verified') else 'uncertain',ident),'none')
        return r
    except Exception as e:
        db.q("UPDATE public_form_drafts SET status='stopped' WHERE id=%s",(ident,),'none')
        raise BookingError(str(e) if isinstance(e,BookingError) else 'Submission stopped or uncertain. Check the destination, do not retry.') from None

def cancel(uid,ident):
    gate(uid);init();db.q("DELETE FROM public_form_drafts WHERE id=%s AND user_id=%s AND status='pending'",(ident,uid),'none');return 'Form preview cancelled. No submission made.'
def handle(uid,chat,text,out):
    if not text.startswith('/public_form'):return False
    if uid!=chat:out.send(chat,'Use your private chat for form previews.');return True
    try:
        fields=text.split(' | ',1)
        if len(fields)!=2:raise BookingError('Use /public_form ADAPTER | {"name":"...","email":"..."}. Only deployment-reviewed free HTML forms are enabled. Google appointment/Calendly are not yet accepted. Exact preview before any submission.')
        adapter=fields[0].split(None,1)[1];d=preview(uid,adapter,json.loads(fields[1]))
        out.send(chat,d['text'],markup={'inline_keyboard':[[{'text':'Submit once','callback_data':'form_submit:'+d['id']+':'+d['hash']},{'text':'Cancel','callback_data':'form_cancel:'+d['id']}]]})
        if d.get('screenshot'):out.artifact(chat,{'filename':'form-preview.png','mime':'image/png','data':base64.b64decode(d['screenshot'],validate=True)})
    except Exception as e:out.send(chat,'Form preview not ready: '+str(e)[:250])
    return True
