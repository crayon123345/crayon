"""Synthetic canonical voice state reference. NOT mounted or enabled in runtime.
Store is ephemeral for adapter contract tests only. Production needs atomic durable DB
and authenticated HTTP/ASGI routing, quota checks and real device/provider acceptance.
"""
import hashlib,hmac,secrets,threading,time,json
class VoiceError(Exception):pass
class VoiceState:
    def __init__(self,origin,session_live,clock=time.time):
        from urllib.parse import urlsplit
        p=urlsplit(origin)
        if p.scheme!='https' or not p.hostname or p.path or p.query or p.fragment or p.username or p.port:raise VoiceError('origin')
        self.origin=origin;self.live=session_live;self.clock=clock;self.lock=threading.RLock();self.tickets={};self.leases={};self.finals={};self.rates={}
    @staticmethod
    def digest(x):return hashlib.sha256(x.encode()).hexdigest()
    def require_origin(self,origin):
        if not isinstance(origin,str) or not hmac.compare_digest(origin,self.origin):raise VoiceError('origin')
    def issue(self,owner,session,conversation,origin,mode,consented=False):
        self.require_origin(origin)
        if mode not in {'transcribe','speak','live'} or not consented:raise VoiceError('consent')
        if not self.live(owner,session):raise VoiceError('revoked')
        now=self.clock()
        with self.lock:
            key=(owner,session);hits=[x for x in self.rates.get(key,[]) if x>now-60]
            if len(hits)>=6:raise VoiceError('rate_limit')
            hits.append(now);self.rates[key]=hits
            ticket=secrets.token_urlsafe(32);rid=secrets.token_urlsafe(24)
            self.tickets[self.digest(ticket)]={'owner':owner,'session':session,'conversation':conversation,'origin':origin,'mode':mode,'request_id':rid,'expires':now+120,'redeemed':False}
            return {'ticket':ticket,'request_id':rid,'expires_at':now+120}
    def redeem(self,ticket,origin,mode,adapter):
        self.require_origin(origin)
        with self.lock:
            b=self.tickets.get(self.digest(ticket));now=self.clock()
            if not b or b['redeemed']:raise VoiceError('already_redeemed')
            if now>=b['expires']:raise VoiceError('expired')
            if b['origin']!=origin or b['mode']!=mode:raise VoiceError('binding')
            if not self.live(b['owner'],b['session']):raise VoiceError('revoked')
            b['redeemed']=True;lease=secrets.token_urlsafe(32)
            self.leases[lease]={**b,'adapter':adapter,'start':now,'bytes':0,'seq':-1,'revoked':False,'events':[]}
            return {'lease_id':lease,'request_id':b['request_id'],'conversation_id':b['conversation'],'mode':mode,'origin':origin,'expires_at':now+600,'revalidate_seconds':15,'audio_limits':{'max_seconds':600,'max_bytes':20000000}}
    def active(self,lease,adapter):
        b=self.leases.get(lease)
        if not b or b['adapter']!=adapter:raise VoiceError('unknown')
        if b['revoked'] or not self.live(b['owner'],b['session']):raise VoiceError('revoked')
        if self.clock()>=b['start']+600:raise VoiceError('expired')
        return b
    def revalidate(self,lease,adapter):
        with self.lock:self.active(lease,adapter);return {'active':True,'next_check_seconds':15}
    def audio(self,lease,adapter,n):
        if type(n) is not int or n<0:raise VoiceError('size')
        with self.lock:
            b=self.active(lease,adapter)
            if b['bytes']+n>20000000:b['revoked']=True;raise VoiceError('audio_size')
            b['bytes']+=n
    def progress(self,lease,adapter,event):
        types={'accepted','progress','transcript_partial','transcript_final','answer_partial','answer_final','review_required','artifact','blocked','done'}
        if set(event)-{'version','seq','type','state','text'} or event.get('version')!=1 or event.get('type') not in types or event.get('state') not in {'queued','running','awaiting_review','blocked','done'} or type(event.get('seq')) is not int:raise VoiceError('malformed')
        if len(event.get('text',''))>16000:raise VoiceError('size')
        with self.lock:
            b=self.active(lease,adapter)
            if event['seq']<=b['seq']:raise VoiceError('sequence_conflict')
            if (b['owner'],b['request_id']) in self.finals:raise VoiceError('already_final')
            b['seq']=event['seq']
            if event['type'] not in {'transcript_partial','answer_partial'}:b['events'].append(dict(event))
            b['events']=b['events'][-50:]
    def final(self,lease,adapter,event):
        if set(event)-{'version','request_id','seq','type','state','transcript_text','answer_text','error_code'} or event.get('version')!=1 or event.get('type')!='done' or event.get('state') not in {'done','blocked'} or type(event.get('seq')) is not int:raise VoiceError('malformed')
        if any(len(event.get(k,''))>16000 for k in ['transcript_text','answer_text']):raise VoiceError('size')
        with self.lock:
            b=self.active(lease,adapter)
            if event['request_id']!=b['request_id']:raise VoiceError('binding')
            key=(b['owner'],b['request_id']);digest=self.digest(json.dumps(event,sort_keys=True))
            old=self.finals.get(key)
            if old:
                if old['digest']!=digest:raise VoiceError('final_conflict')
                return {'accepted':True,'duplicate':True}
            if event['seq']<=b['seq']:raise VoiceError('sequence_conflict')
            b['seq']=event['seq'];self.finals[key]={'digest':digest,'expires':self.clock()+300,'session':b['session'],'conversation':b['conversation'],'origin':b['origin'],'events':list(b['events'])+[dict(event)]}
            return {'accepted':True,'duplicate':False}
    def recover(self,owner,session,rid,conversation,origin):
        self.require_origin(origin)
        if not self.live(owner,session):raise VoiceError('revoked')
        with self.lock:
            b=self.finals.get((owner,rid))
            if not b or b['session']!=session or b['conversation']!=conversation:raise VoiceError('unknown')
            if self.clock()>=b['expires']:raise VoiceError('expired')
            return {'request_id':rid,'conversation_id':conversation,'events':list(b['events']),'expires_at':b['expires']}
    def revoke(self,owner,session,rid):
        with self.lock:
            found=False
            for b in self.leases.values():
                if b['owner']==owner and b['session']==session and b['request_id']==rid:b['revoked']=True;found=True
            for b in self.tickets.values():
                if b['owner']==owner and b['session']==session and b['request_id']==rid:b['redeemed']=True;found=True
            if not found:raise VoiceError('unknown')
            return {'revoked':True}
