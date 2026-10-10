"""Bounded public GitHub digests. No broad private repo grant or writes."""
import re
import httpx
import cr_connections as X
import cr_google as G
import cr_db as db


def digest(uid,repo):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}',repo or '') or '..' in repo:
        raise G.GoogleError('Use owner/repository, not a URL or path.')
    headers={'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}
    if X.configured('github') and X.status(uid,'github'):headers['Authorization']='Bearer '+X.access(uid,'github')
    n=db.q("SELECT count(*) AS n FROM audit WHERE user_id=%s AND event='github_digest' AND ts>now()-interval '24 hours'",(uid,),'one')['n']
    if n>=30:raise G.GoogleError('Daily GitHub digest limit reached.')
    db.audit(uid,'github_digest')
    with httpx.Client(timeout=20,follow_redirects=False) as c:
        base='https://api.github.com/repos/'+repo
        r=c.get(base,headers=headers)
        if r.status_code!=200 or r.json().get('private') is not False:raise G.GoogleError('Public repository not verified. Private access is not enabled yet.')
        meta=r.json();items=[]
        for path,params in (('/commits',{'per_page':5}),('/pulls',{'state':'open','per_page':5,'sort':'updated'})):
            r=c.get(base+path,params=params,headers=headers)
            if r.status_code!=200:raise G.GoogleError('GitHub read failed or was rate limited.')
            for v in r.json():
                title=v.get('title') or v.get('commit',{}).get('message','').split('\n')[0]
                items.append({'type':'PR' if path=='/pulls' else 'commit','id':v.get('number') or v.get('sha','')[:7],
                   'title':re.sub(r'[\x00-\x1f]',' ',title)[:180],'url':v.get('html_url','')})
    return {'repo':meta['full_name'],'url':meta['html_url'],'items':items}
