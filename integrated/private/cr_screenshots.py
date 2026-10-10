"""Explicit public screenshot requests, shared by DM and mention-only groups."""
import re,base64

def requested(text):return bool(re.search(r'(?i)\bscreen\s?shots?\b|\bscreen capture\b',text))
def url_from(text):
    m=re.search(r'https://[^\s<>]+|\b(?:[a-zA-Z0-9-]+\.)+(?:com|org|md|in|net|co)(?:/[^\s<>]*)?',text)
    if not m:return None
    url=m.group().rstrip('.,);]');return url if url.startswith('https://') else 'https://'+url

def capture(uid,text):
    import cr_computer as K
    from cr_safety import looks_like_secret
    if looks_like_secret(text):return 'That looks like a secret. I will not open or capture it.',None
    url=url_from(text)
    if not url:return 'Which public webpage should I capture? Send its link.',None
    result=K.execute(uid,'browse',{'url':url})
    image=result.get('screenshot')
    if not image:return 'Screenshot not completed: '+result.get('error','No image returned.'),None
    data=base64.b64decode(image,validate=True)
    if len(data)>1000000:raise ValueError('Screenshot too large')
    final=(result.get('pages') or [{}])[-1].get('url',url)
    note='Webpage screenshot: '+final
    if not result.get('verified'):note+='\nThe capture shows an error or access wall, not a verified page.'
    return note,{'filename':'crayon-screenshot.png','mime':'image/png','data':data}

def group_request(msg,out):
    text=msg.get('text','')
    if not requested(text):return False
    uid=msg.get('from',{}).get('id',0);chat=msg['chat']['id']
    if type(uid) is not int or uid<=0 or msg.get('sender_chat') or any(msg.get(k) for k in ('forward_origin','forward_from','via_bot')):
        out.send(chat,'Tag me directly with a public webpage link for a screenshot.');return True
    try:
        note,item=capture(uid,text)
        if item:
            out.artifact(chat,item)
            out.send(chat,note)
        else:out.send(chat,note)
    except Exception:out.send(chat,'I could not capture or deliver that screenshot. No delivery is confirmed.')
    return True
