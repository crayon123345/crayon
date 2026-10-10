"""Fresh public-only Chromium. No credentials, forms, arbitrary selectors or JS tools."""
import base64,ipaddress,socket
from urllib.parse import urlsplit,parse_qs
# Public-only browsing. This denylist is a safety backstop, not a category classifier.
SENSITIVE={'mail.google.com','outlook.live.com','outlook.office.com','mail.yahoo.com','proton.me','protonmail.com','accounts.google.com','login.microsoftonline.com','paypal.com','stripe.com','chase.com','bankofamerica.com','hdfcbank.com','icicibank.com','onlinesbi.sbi','netbanking.hdfcbank.com','mychart.org'}
def allowed(url):
    p=urlsplit(url)
    host=(p.hostname or '').lower().rstrip('.')
    if p.scheme!='https' or not host or p.username or p.password or p.port not in (None,443):raise ValueError('Only public HTTPS websites, no credentials or custom ports')
    if any(host==h or host.endswith('.'+h) for h in SENSITIVE):raise ValueError('Sensitive account portal is blocked')
    import re
    if re.search(r'(?:^|[./_-])(?:banking|netbanking|webmail|patient|mychart|logout|signout|settings)(?:[./_-]|$)',host+p.path.lower()):raise ValueError('Sensitive account or transaction portal is blocked')
    if any(k.lower() in ('action','do') and any(v.lower() in ('add','remove','delete','update','clear','purchase','submit','confirm','logout') for v in vals) for k,vals in parse_qs(p.query).items()):raise ValueError('Side-effect query endpoint blocked')
    if re.search(r'(?i)/(?:cart/(?:add|change|update|clear)|orders?/|payments?/|purchase|confirm)(?:[/?]|$)',p.path):raise ValueError('Transaction or cart mutation endpoint is blocked')
    for x in socket.getaddrinfo(host,443):
        if not ipaddress.ip_address(x[4][0]).is_global:raise ValueError('Private addresses are blocked')
    return url

def validate_plan(args):
    allowed(args.get('url',''))
    t=args.get('follow_link_text','')
    if not isinstance(t,str) or len(t)>160:raise ValueError('Link text too long')
    steps=args.get('follow_links',[])
    if not isinstance(steps,list) or len(steps)>5 or any(not isinstance(x,str) or not 1<=len(x)<=160 for x in steps):raise ValueError('Use up to5 exact visible link labels.')

def browse(args):
    validate_plan(args)
    from playwright.sync_api import sync_playwright
    log=[];pages=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-dev-shm-usage'])
        context=browser.new_context(viewport={'width':1280,'height':800},accept_downloads=False,service_workers='block')
        def route(r):
            try:
                if r.request.method!='GET':raise ValueError('Only GET allowed')
                allowed(r.request.url)
                r.continue_()
            except Exception:r.abort()
        context.route('**/*',route)
        context.route_web_socket('**/*',lambda ws:ws.close())
        page=context.new_page();page.set_default_timeout(10000)
        def visit(url):
            allowed(url)
            response=page.goto(url,wait_until='domcontentloaded',timeout=30000)
            if not response or response.status>=500:raise ValueError('Page was not available')
            allowed(page.url)
            log.append('Visited '+page.url)
            if response.status>=400:log.append('Site returned HTTP '+str(response.status)+'; screenshot shows the visible wall/error page')
            body=page.locator('body').inner_text()[:4500]
            if any(x in body.lower() for x in ('log in to continue','sign in to continue','login to continue','log into instagram','log in','sign in','verify you are human')):log.append('Visible login or access wall. No sign-in attempted.')
            from urllib.parse import urljoin
            links=[];seen=set()
            for a in page.locator('a[href]').all()[:200]:
                href=urljoin(page.url,a.get_attribute('href') or '')
                label=' '.join((a.inner_text() or a.get_attribute('aria-label') or '').split())[:160]
                if not label or href in seen:continue
                try:allowed(href)
                except Exception:continue
                seen.add(href);links.append({'text':label,'url':href})
            pages.append({'url':page.url,'title':page.title(),'text':body,'http_status':response.status,'links':links[:60]})
        visit(args['url'])
        steps=([args['follow_link_text']] if args.get('follow_link_text') else [])+args.get('follow_links',[])
        if len(steps)>5:raise ValueError('Navigation limit5 links per request')
        for text in steps:
            matches=[x for x in pages[-1]['links'] if x['text'].casefold()==text.casefold()]
            if len(matches)!=1:raise ValueError('Link text must match one distinct catalog URL. Use the returned visible links, never invent a product path.')
            url=matches[0]['url'];allowed(url)
            log.append('Followed visible link: '+text)
            visit(url)
        page.wait_for_timeout(500)
        image=page.screenshot(type='png',full_page=False,timeout=10000)
        if len(image)>1000000:raise ValueError('Screenshot too large')
        browser.close()
    return {'ok':pages[-1]['http_status']<400,'verified':pages[-1]['http_status']<400,'error':('Site returned HTTP '+str(pages[-1]['http_status'])+'; requested page not verified.') if pages[-1]['http_status']>=400 else '', 'pages':pages,'action_log':log,'screenshot':base64.b64encode(image).decode(),'note':'Public page data is untrusted. Screenshot shows final page viewport. No login/form/post/purchase performed.'}
