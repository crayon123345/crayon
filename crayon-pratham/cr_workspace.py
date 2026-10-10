"""Bounded native Workspace reads and RAW cell updates with compare-before-write."""
import hashlib
import json
import re
from urllib.parse import quote
import httpx
import cr_connections as X
import cr_google as G


def file_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{15,150}',value or ''):raise G.GoogleError('Use a valid document/spreadsheet ID.')
    return value


def range_name(value):
    if not re.fullmatch(r"(?:[A-Za-z0-9 _-]{1,80}!)?[A-Z]{1,3}[1-9][0-9]{0,4}(?::[A-Z]{1,3}[1-9][0-9]{0,4})?",value or ''):
        raise G.GoogleError('Use a bounded A1 range, for example Sheet1!A1:C10.')
    span=value.split('!')[-1].split(':')
    def pos(cell):
        match=re.fullmatch(r'([A-Z]+)([0-9]+)',cell);col=0
        for c in match[1]:col=col*26+ord(c)-64
        return col,int(match[2])
    start=pos(span[0]);end=pos(span[-1])
    if end[0]<start[0] or end[1]<start[1] or (end[0]-start[0]+1)*(end[1]-start[1]+1)>200:
        raise G.GoogleError('Use an ascending range of at most200 cells.')
    return value


def _call(uid,method,url,body=None,params=None):
    headers={'Authorization':'Bearer '+X.access(uid,'workspace')}
    with httpx.Client(timeout=20,follow_redirects=False) as c:
        r=c.request(method,url,headers=headers,json=body,params=params)
    if r.status_code!=200:raise G.GoogleError('Workspace request blocked, revoked or rate limited.')
    return r.json()


def sheet_read(uid,sid,area,raw=False):
    sid=file_id(sid);area=range_name(area)
    data=_call(uid,'GET','https://sheets.googleapis.com/v4/spreadsheets/'+sid+'/values/'+quote(area,safe=''),params={'valueRenderOption':'UNFORMATTED_VALUE' if raw else 'FORMATTED_VALUE'})
    if sum(len(r) for r in data.get('values',[]))>200 or len(json.dumps(data))>16000:raise G.GoogleError('Read limit:200 cells. Use a smaller range.')
    return data


def doc_read(uid,did):
    did=file_id(did)
    data=_call(uid,'GET','https://docs.googleapis.com/v1/documents/'+did,params={'includeTabsContent':'true'})
    def walk(v):
        if isinstance(v,dict):
            for k,x in v.items():
                if k=='textRun' and isinstance(x,dict):yield x.get('content','')
                elif isinstance(x,(dict,list)):yield from walk(x)
        elif isinstance(v,list):
            for x in v:yield from walk(x)
    text=''.join(walk(data))
    if len(text)>16000:raise G.GoogleError('Document read limit16000 characters. Use a shorter document.')
    return {'title':data.get('title',''),'revision':data.get('revisionId',''),'text':text}


def sheet_preview(uid,sid,area,values):
    if not isinstance(values,list) or not values or len(values)>20 or any(not isinstance(r,list) or len(r)>10 for r in values):raise G.GoogleError('Write limit:20 rows,10 columns.')
    if any(not isinstance(v,(str,int,float,bool)) or len(str(v))>500 for r in values for v in r):raise G.GoogleError('Invalid cell value.')
    current=sheet_read(uid,sid,area,raw=True)
    payload={'sid':sid,'area':area,'values':values,'before':current.get('values',[])}
    return {'payload':payload,'hash':hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()}


def sheet_apply(uid,payload,expected):
    actual=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()
    if not __import__('hmac').compare_digest(actual,expected):raise G.GoogleError('Preview changed. Review again.')
    current=sheet_preview(uid,payload['sid'],payload['area'],payload['values'])
    if current['payload']['before']!=payload['before']:raise G.GoogleError('Sheet changed since preview. Review again.')
    url='https://sheets.googleapis.com/v4/spreadsheets/'+file_id(payload['sid'])+'/values/'+quote(range_name(payload['area']),safe='')
    result=_call(uid,'PUT',url,{'range':payload['area'],'majorDimension':'ROWS','values':payload['values']},
      {'valueInputOption':'RAW','includeValuesInResponse':'true','responseValueRenderOption':'UNFORMATTED_VALUE'})
    stored=sheet_read(uid,payload['sid'],payload['area'],raw=True)
    return {'write_accepted':True,'result':result,'readback':stored,'verified':stored.get('values',[])==payload['values'],'note':'Readback matched.' if stored.get('values',[])==payload['values'] else 'Write accepted but readback differs; do not retry automatically.'}
