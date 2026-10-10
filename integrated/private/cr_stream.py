"""Provisional visible text only. Never thought blocks or tool arguments."""
from contextvars import ContextVar
import time
from cr_safety import clean_text,redact,looks_like_secret
callback=ContextVar('crayon_stream_callback',default=None)
allowed=ContextVar('crayon_stream_allowed',default=False)
def text_part(chunk):
    content=getattr(chunk,'content','')
    if isinstance(content,str):return content
    if not isinstance(content,list):return ''
    return ''.join(x.get('text','') for x in content if isinstance(x,dict) and x.get('type')=='text' and not x.get('thought'))
def consume(chat,msgs):
    combined=None;visible='';last=0
    for chunk in chat.stream(msgs):
        combined=chunk if combined is None else combined+chunk
        visible+=text_part(chunk)
        if callback.get() and time.monotonic()-last>.35:
            safe='[Sensitive draft withheld]' if looks_like_secret(visible) else clean_text(redact(visible))[:16000]
            callback.get()(safe);last=time.monotonic()
    if combined is None:raise ValueError('Empty model stream')
    return combined
