"""Restricted public HTML form adapter. Fresh context; no account cookies or arbitrary JS."""
import hashlib,json,base64
from urllib.parse import urlsplit,urljoin
from computer_browser import allowed
FIELDS={'name','email','date','time','timezone','note'}
def validate(config,values=None):
    if not isinstance(config,dict) or set(config)!={'url','action','fields','confirmation','terms'}:raise ValueError('Invalid configured form')
    allowed(config['url']);allowed(config['action'])
    if urlsplit(config['url']).netloc!=urlsplit(config['action']).netloc:raise ValueError('Cross-origin submissions blocked')
    if not isinstance(config['fields'],list) or not config['fields'] or len(config['fields'])>6 or len(set(config['fields']))!=len(config['fields']) or not set(config['fields'])<=FIELDS:raise ValueError('Unsupported fields')
    if not isinstance(config['confirmation'],str) or not config['confirmation'] or len(config['confirmation'])>100:raise ValueError('Invalid confirmation')
    if not isinstance(config['terms'],str) or not config['terms'] or len(config['terms'])>800:raise ValueError('Terms must be reviewed in adapter configuration')
    if values is not None:
        from cr_safety import looks_like_secret
        if not isinstance(values,dict) or set(values)!=set(config['fields']):raise ValueError('Exact fields required')
        if any(not isinstance(v,str) or not v.strip() or len(v)>300 or looks_like_secret(v) for v in values.values()):raise ValueError('Invalid field or secret-like value')
    return config

def fingerprint(snapshot):return hashlib.sha256(json.dumps(snapshot,sort_keys=True).encode()).hexdigest()

def run(config,values=None,expected_hash=None):
    validate(config,values)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-dev-shm-usage'])
        ctx=browser.new_context(viewport={'width':1280,'height':800},accept_downloads=False,service_workers='block')
        posts=[0];armed=[False];filling=[False]
        def route(r):
            try:
                allowed(r.request.url)
                if urlsplit(r.request.url).netloc!=urlsplit(config['url']).netloc:raise ValueError('Cross-origin requests blocked')
                if filling[0] and r.request.method=='GET' and posts[0]==0:raise ValueError('Network blocked during field filling')
                if r.request.method!='GET':
                    if not (armed[0] and posts[0]==0 and r.request.method=='POST' and r.request.is_navigation_request() and r.request.url==config['action']):raise ValueError('Unreviewed request')
                    from urllib.parse import parse_qs
                    if parse_qs(r.request.post_data or '',keep_blank_values=True)!={k:[v] for k,v in values.items()}:raise ValueError('Submitted payload changed')
                    posts[0]+=1
                r.continue_()
            except Exception:r.abort()
        ctx.route('**/*',route);ctx.route_web_socket('**/*',lambda w:w.close())
        page=ctx.new_page();page.set_default_timeout(10000)
        response=page.goto(config['url'],wait_until='domcontentloaded',timeout=30000)
        if not response or response.status!=200 or page.url!=config['url']:raise ValueError('Form unavailable or redirected')
        # Page content is data only. No content can add actions or adapter configuration.
        text=page.locator('body').inner_text()[:7000]
        if any(x in text.lower() for x in ('sign in','log in','captcha','credit card','payment','purchase','no-show fee','cancellation fee')):raise ValueError('Login/challenge/payment terms require human handling')
        forms=page.locator('form')
        if forms.count()!=1:raise ValueError('Exactly one configured form required')
        form=forms.first
        action=urljoin(page.url,form.get_attribute('action') or page.url)
        if action!=config['action'] or (form.get_attribute('method') or 'get').lower()!='post':raise ValueError('Form destination or method changed')
        inputs=form.locator('input,textarea,select')
        snapshot={'url':page.url,'action':action,'title':page.title(),'text':text,'fields':[]}
        for i in range(inputs.count()):
            item=inputs.nth(i);typ=(item.get_attribute('type') or 'text').lower();name=item.get_attribute('name')
            if typ in ('submit','button'):continue
            if item.evaluate('(e)=>e.tagName')=='SELECT' or typ not in ('text','email','date','time') or name not in config['fields']:raise ValueError('Unsupported or additional field')
            if not item.is_visible() or not item.is_enabled():raise ValueError('Hidden or disabled field')
            snapshot['fields'].append({'name':name,'type':typ,'required':item.get_attribute('required') is not None})
        names=[x['name'] for x in snapshot['fields']]
        if len(names)!=len(set(names)) or set(names)!=set(config['fields']):raise ValueError('Fields changed')
        h=fingerprint(snapshot)
        if values is None:
            image=page.screenshot(type='png');browser.close()
            return {'ok':True,'verified':True,'hash':h,'snapshot':snapshot,'screenshot':base64.b64encode(image).decode(),'note':'Inspection only. No fields filled or form submitted.'}
        if h!=expected_hash:raise ValueError('Page/form changed since review; make a fresh preview')
        filling[0]=True
        for name,value in values.items():form.locator('[name="'+name+'"]').fill(value)
        for name,value in values.items():
            if form.locator('[name="'+name+'"]').input_value()!=value:raise ValueError('Field readback mismatch')
        button=form.locator('button[type=submit],input[type=submit]')
        if button.count()!=1 or not button.is_visible():raise ValueError('Exactly one submit control required')
        armed[0]=True
        button.click();page.wait_for_load_state('domcontentloaded')
        # No automatic retry. A single positive UI marker is a receipt, not a venue reservation.
        confirmed=posts[0]==1 and page.url==config['action'] and page.get_by_text(config['confirmation'],exact=True).count()==1
        image=page.screenshot(type='png');url=page.url;browser.close()
        return {'ok':confirmed,'verified':confirmed,'url':url,'screenshot':base64.b64encode(image).decode(),'note':'Configured confirmation page observed after one submission.' if confirmed else 'Outcome uncertain. Check the destination manually; do not retry.'}
