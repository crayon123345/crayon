"""Exact-reviewed standalone -> legacy Telegram consolidation.
Reviewed consolidation. Disabled unless CRAYON_ACCOUNT_MERGE_ENABLED is on.
"""
import hashlib,json
import cr_db as db
import cr_accounts as K

# The first path never transfers provider credentials, sends, background grants or settings.
BLOCKING_TABLES=('reminders','tasks','pending_actions','google_connections','google_email_drafts',
 'google_oauth_states','service_connections','service_oauth_states','google_calendar_drafts',
 'public_form_drafts','work_jobs','computer_usage')
SCHEMA='''CREATE TABLE IF NOT EXISTS account_merge_reviews(
 id TEXT PRIMARY KEY,source_id BIGINT NOT NULL,target_id BIGINT NOT NULL,
 session_hash TEXT NOT NULL,challenge TEXT NOT NULL,encrypted TEXT NOT NULL,
 content_hash TEXT NOT NULL,expires_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS account_redirects(source_id BIGINT PRIMARY KEY,target_id BIGINT,
 merged_at TIMESTAMPTZ NOT NULL DEFAULT now());'''

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()

def init():
    for stmt in SCHEMA.split(';'):
        if stmt.strip():db.q(stmt,fetch='none')
    db.q('ALTER TABLE account_redirects ALTER COLUMN target_id DROP NOT NULL',fetch='none')
    db.q('DELETE FROM account_merge_reviews WHERE expires_at<now()',fetch='none')

def snapshot(source,target):
    if not K.standalone(source) or type(target) is not int or not 0<target<10**13:raise ValueError('Only standalone to verified Telegram consolidation is supported.')
    users=db.q('SELECT * FROM users WHERE user_id IN (%s,%s) ORDER BY user_id',(source,target))
    by_id={r['user_id']:r for r in users}
    if source not in by_id:raise ValueError('Source account no longer exists.')
    source_user=by_id[source];target_user=by_id.get(target)
    # A fresh Telegram identity might not yet have a users row. Never invent an ID.
    if source_user.get('settings'):raise ValueError('Source account has settings or background grants. Manual review is required; nothing moved.')
    owned=db.q("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' AND column_name IN ('user_id','sender','recipient') ORDER BY table_name")
    if any(not __import__('re').fullmatch(r'[a-z_]+',r['table_name']) for r in owned):raise ValueError('Unknown storage identifier requires review.')
    tables={r['table_name'] for r in owned if r['column_name']=='user_id'}
    known=set(BLOCKING_TABLES)|{'users','facts','messages','notes','audit','account_identities','web_google_identities','web_sessions','web_login_codes','web_email_sessions','web_email_reviews','web_google_reviews','web_action_reviews','web_requests','channel_history'}
    if tables-known:raise ValueError('Unknown account-owned storage requires a migration review; nothing moved.')
    for row in owned:
        if row['column_name'] in ('sender','recipient') and db.q('SELECT 1 FROM '+row['table_name']+' WHERE '+row['column_name']+'=%s LIMIT 1',(str(source),),'one'):raise ValueError('Source account has channel transport state. Manual review is required.')
    for table in BLOCKING_TABLES:
        if table in tables and db.q('SELECT 1 FROM '+table+' WHERE user_id=%s LIMIT 1',(source,),'one'):
            raise ValueError('Source account has '+table+' records. This merge path will not transfer credentials, pending actions or grants.')
    if 'web_requests' in tables and db.q("SELECT 1 FROM web_requests WHERE user_id IN (%s,%s) AND state IN ('queued','running') LIMIT 1",(source,target),'one'):raise ValueError('Web work is still running on one of these accounts. Wait until it finishes.')
    for table,statuses in [('google_email_drafts',('sending','uncertain')),('google_calendar_drafts',('creating','stopped')),('public_form_drafts',('submitting','uncertain')),('web_action_reviews',('claimed','stopped'))]:
        if table in tables and db.q('SELECT 1 FROM '+table+' WHERE user_id IN (%s,%s) AND status IN (%s,%s) LIMIT 1',(source,target,*statuses),'one'):raise ValueError('An external action is in progress or uncertain. Check that outcome before consolidation.')
    for table in ('pending_actions','google_email_drafts','google_calendar_drafts','public_form_drafts','web_action_reviews'):
        if table in tables and db.q('SELECT 1 FROM '+table+' WHERE user_id=%s LIMIT 1',(target,),'one'):raise ValueError('Destination has pending or historical action reviews. Resolve or clear those before consolidation.')
    group_grants=db.q("SELECT key FROM kv WHERE key LIKE %s AND value='true'::jsonb",('group_audience_v1_'+str(target)+'_%',))
    if group_grants:raise ValueError('Telegram account has group-audience permissions. Expanded merged memory needs a separate audience review; nothing moved.')
    # Keyed private state, including encrypted compose/signature data, may encode user bindings.
    private=db.q("SELECT key FROM kv WHERE (key LIKE %s OR key LIKE %s) AND value IS NOT NULL AND value<>'null'::jsonb AND value<>'false'::jsonb",('%_'+str(source),'%_'+str(source)+'_%'))
    if private:raise ValueError('Source account has private keyed state. This merge path will not silently move grants or pending actions.')
    rows={}
    for table in ('facts','messages','notes','account_identities','web_google_identities','web_requests','channel_history'):
        rows[table]=db.q('SELECT * FROM '+table+' WHERE user_id IN (%s,%s) ORDER BY user_id',(source,target)) if table in tables else []
    source_requests={r['id'] for r in rows['web_requests'] if r['user_id']==source}
    target_requests={r['id'] for r in rows['web_requests'] if r['user_id']==target}
    if source_requests & target_requests:raise ValueError('Both accounts have matching web request IDs. Manual storage review is required.')
    if len(rows['facts'])>300 or len(rows['messages'])>20000 or len(rows['web_requests'])>1000:raise ValueError('These accounts exceed the bounded merge review size. Manual migration review is required.')
    target_facts={r['key']:r for r in rows['facts'] if r['user_id']==target}
    conflicts=[{'key':r['key'],'source':r['value'],'target':target_facts[r['key']]['value']} for r in rows['facts'] if r['user_id']==source and r['key'] in target_facts and r['value']!=target_facts[r['key']]['value']]
    identities=[]
    for table,provider,email in [('account_identities',None,'display_email'),('web_google_identities','google','email')]:
        identities += [{'provider':provider or r['provider'],'email':r.get(email,''),'account':r['user_id'],'subject':r['subject']} for r in rows[table]]
    for provider in ('google','firebase_email'):
        if len([i for i in identities if i['provider']==provider])>1:raise ValueError('Both accounts have '+provider+' login identities. Identity-conflict selection is not implemented; nothing moved.')
    destination_connections=[]
    if 'google_connections' in tables:
        destination_connections += [{'provider':'Gmail/Calendar','identity':r['email']} for r in db.q('SELECT email FROM google_connections WHERE user_id=%s',(target,))]
    if 'service_connections' in tables:
        destination_connections += db.q('SELECT provider,identity FROM service_connections WHERE user_id=%s ORDER BY provider',(target,))
    rows={t:sorted(v,key=lambda r:json.dumps(r,sort_keys=True,default=str)) for t,v in rows.items()}
    users=sorted(users,key=lambda r:r['user_id'])
    snapshot={'destination_connections':destination_connections,'source':source,'target':target,'users':users,'rows':rows,'conflicts':sorted(conflicts,key=lambda r:r['key']),'identities':sorted(identities,key=lambda r:(r['provider'],r['subject']))}
    return snapshot,digest(snapshot)

def review_text(data):
    source,target=data['source'],data['target'];lines=['Merge these two Crayon accounts?','Standalone account: '+str(source),'Verified Telegram account: '+str(target),
      'Destination: existing Telegram account '+str(target)+'. Its data and existing provider connections stay in place.']
    for connection in data['destination_connections']:lines.append('Existing destination connection: '+connection['provider']+' '+connection['identity']+'. New linked logins will be able to access it through this same account; external writes still need their own exact review.')
    target_user=next((u for u in data['users'] if u['user_id']==target),{})
    lines.append('Destination background/settings kept: '+json.dumps(target_user.get('settings',{}),sort_keys=True))
    for row in data['identities']:lines.append('Login: '+row['provider']+' '+row['email']+' (currently account '+str(row['account'])+')')
    for table in ('messages','facts','notes','web_requests','channel_history'):
        rows=data['rows'][table];lines.append(table+': move '+str(sum(r['user_id']==source for r in rows))+' source records; keep '+str(sum(r['user_id']==target for r in rows))+' destination records.')
    lines+=['Source display name/timezone and automatic summary will not replace Telegram settings. Both automatic summaries will be cleared and rebuilt from combined recorded messages.',
      'All active web sessions, unfinished login handoffs and review tickets for both accounts will be revoked. Sign in again after merging.',
      'No provider credential, pending send, background grant or group audience permission moves from the source. No external message is sent. Facts and history are data, not new permission.',
      'This cannot be undone automatically. Exact fact conflict choices are required. A changed account snapshot invalidates this review.']
    for row in data['rows']['facts']:
        if row['user_id']==source:lines.append('Source fact '+row['key']+': '+row['value'])
    for row in data['conflicts']:lines.append('Conflict '+row['key']+': source='+row['source']+'; destination='+row['target'])
    return '\n'.join(lines)

def apply_local_review(source,target,expected,choices,session_hash=None):
    """Atomic core. Caller must establish both identities and consume exact session review.
    Called only after both identities and the exact review are checked.
    """
    import cr_web_auth as A
    import cr_memory as mem
    if not isinstance(choices,dict) or any(v not in ('source','target') for v in choices.values()):raise ValueError('Explicit fact choices required.')
    with mem.user_lock(source),mem.user_lock(target),db._conn().transaction():
        # Block writes during the final snapshot+move. No copy-and-delete race or partial merge.
        init()
        tables=db.q("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name")
        names=[r['table_name'] for r in tables]
        if any(not __import__('re').fullmatch(r'[a-z_]+',n) for n in names):raise ValueError('Unexpected storage identifier.')
        db.q("SET LOCAL lock_timeout='3s'",fetch='none')
        db.q("SET LOCAL statement_timeout='10s'",fetch='none')
        for name in names:db.q('LOCK TABLE '+name+' IN SHARE ROW EXCLUSIVE MODE',fetch='none')
        # Install database-level guards before changing the source account. They also
        # catch delayed background writes and stale session creation after commit.
        db.q('''CREATE OR REPLACE FUNCTION reject_retired_crayon_account() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF EXISTS (SELECT 1 FROM account_redirects WHERE source_id=NEW.user_id) THEN RAISE EXCEPTION 'Account was consolidated; sign in again'; END IF; RETURN NEW; END $$''',fetch='none')
        columns=db.q("SELECT table_name FROM information_schema.columns WHERE table_schema='public' AND column_name='user_id'")
        for column in columns:
            table=column['table_name']
            if not __import__('re').fullmatch(r'[a-z_]+',table):raise ValueError('Unexpected storage identifier.')
            db.q('DROP TRIGGER IF EXISTS reject_retired_account ON '+table,fetch='none')
            db.q('CREATE TRIGGER reject_retired_account BEFORE INSERT OR UPDATE OF user_id ON '+table+' FOR EACH ROW EXECUTE FUNCTION reject_retired_crayon_account()',fetch='none')
        if session_hash is not None and not db.q('SELECT user_id FROM web_sessions WHERE token_hash=%s AND user_id=%s AND expires_at>now()',(session_hash,source),'one'):raise ValueError('Original session ended before commit. Nothing moved.')
        data,current=snapshot(source,target)
        if not __import__('hmac').compare_digest(current,expected):raise ValueError('Account data changed. Review a fresh exact snapshot; nothing moved.')
        required={r['key'] for r in data['conflicts']}
        if set(choices)!=required:raise ValueError('Choose a source or destination value for every fact conflict.')
        if not any(r['user_id']==target for r in data['users']):
            db.q('INSERT INTO users(user_id,name) VALUES(%s,%s)',(target,'Telegram user'),'none')
        source_facts=[r for r in data['rows']['facts'] if r['user_id']==source]
        for fact in source_facts:
            existing=next((r for r in data['rows']['facts'] if r['user_id']==target and r['key']==fact['key']),None)
            if existing:
                if choices.get(fact['key'])=='source':db.q('UPDATE facts SET value=%s,category=%s,source=%s,updated_at=%s WHERE id=%s',(fact['value'],fact['category'],fact['source'],fact['updated_at'],existing['id']),'none')
                db.q('DELETE FROM facts WHERE id=%s',(fact['id'],),'none')
            else:db.q('UPDATE facts SET user_id=%s WHERE id=%s',(target,fact['id']),'none')
        for table in ('messages','notes','web_requests','channel_history','audit'):
            if table in names:db.q('UPDATE '+table+' SET user_id=%s WHERE user_id=%s',(target,source),'none')
        db.q('UPDATE account_identities SET user_id=%s WHERE user_id=%s',(target,source),'none')
        db.q('UPDATE web_google_identities SET user_id=%s WHERE user_id=%s',(target,source),'none')
        db.q('DELETE FROM account_merge_reviews WHERE source_id IN (%s,%s) OR target_id IN (%s,%s)',(source,target,source,target),'none')
        for table in ('web_sessions','web_login_codes','web_email_sessions','web_email_reviews','web_google_reviews','web_action_reviews'):
            if table in names:db.q('DELETE FROM '+table+' WHERE user_id IN (%s,%s)',(source,target),'none')
        # Deactivate all in-progress session-bound identity linking from both account IDs.
        for table in ('web_login_states','web_google_states'):
            if table in names:
                for row in db.q('SELECT state_hash,encrypted FROM '+table):
                    state=json.loads(A._cipher().decrypt(row['encrypted'].encode()))
                    if state.get('uid') in (source,target):db.q('DELETE FROM '+table+' WHERE state_hash=%s',(row['state_hash'],),'none')
        db.q('UPDATE users SET summary=%s,summary_upto=0,memory_epoch=memory_epoch+1 WHERE user_id=%s',('',target),'none')
        db.q('DELETE FROM users WHERE user_id=%s',(source,),'none')
        db.q('INSERT INTO account_redirects(source_id,target_id) VALUES(%s,%s)',(source,target),'none')
        db.audit(target,'account_merge',str(source)+' -> '+str(target))
        if db.q('SELECT user_id FROM users WHERE user_id=%s',(source,),'one'):raise ValueError('Merge readback failed.')
        if any(db.q('SELECT 1 FROM '+t+' WHERE user_id=%s LIMIT 1',(source,),'one') for t in ('messages','notes','facts','account_identities','web_google_identities')):raise ValueError('Merge storage readback failed.')
        return {'text':'Accounts merged into Telegram account '+str(target)+'. Sign in again. No external message or permission transfer occurred.'}

# Only a verified Telegram OIDC callback supplies the destination identity.
# All routes remain disabled without the explicit server configuration flag.
def prepare_verified_review(user,header,challenge,verified_target,verified_name,session_hash=None):
    import cr_web_auth as A,secrets,re
    if session_hash is None:
        if not isinstance(header,str) or not header.startswith('Bearer ') or not A.PATTERN.fullmatch(header[7:]):raise ValueError('Original session required.')
        session_hash=A.digest(header[7:])
    if not isinstance(session_hash,str) or not re.fullmatch(r'[a-f0-9]{64}',session_hash):raise ValueError('Original session binding required.')
    if not isinstance(challenge,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',challenge):raise ValueError('Exact browser challenge required.')
    init();source=user['user_id']
    active=db.q('SELECT user_id FROM web_sessions WHERE token_hash=%s AND user_id=%s AND expires_at>now()',(session_hash,source),'one')
    if not active:raise ValueError('Original session ended. Start again.')
    data,stamp=snapshot(source,verified_target)
    # Name is from signed Telegram profile, not a matching email/name search.
    data['verified_telegram_name']=str(verified_name)[:100]
    text=review_text(data)+'\nVerified Telegram display name: '+data['verified_telegram_name']
    payload={'snapshot':data,'snapshot_hash':stamp,'text':text};content_hash=digest(payload);ident=secrets.token_urlsafe(32)
    db.q("INSERT INTO account_merge_reviews(id,source_id,target_id,session_hash,challenge,encrypted,content_hash,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,now()+interval '5 minutes')",(ident,source,verified_target,session_hash,challenge,A._cipher().encrypt(json.dumps(payload,default=str).encode()).decode(),content_hash),'none')
    return {'review_id':ident,'hash':content_hash,'text':text,'conflicts':data['conflicts']}

def confirm_verified_review(user,header,body):
    import cr_web_auth as A,re,hmac
    if set(body)!={'review_id','hash','decision','choices'} or not isinstance(body['review_id'],str) or not A.PATTERN.fullmatch(body['review_id']) or not isinstance(body['hash'],str) or not re.fullmatch(r'[a-f0-9]{64}',body['hash']) or body['decision'] not in ('confirm','cancel'):raise ValueError('Invalid exact merge review.')
    init()
    # Claim once outside the merge transaction. A stale/failed review must not re-fire.
    row=db.q('DELETE FROM account_merge_reviews WHERE id=%s AND source_id=%s AND session_hash=%s AND content_hash=%s AND expires_at>now() RETURNING target_id,encrypted,content_hash',(body['review_id'],user['user_id'],A.digest(header[7:]),body['hash']),'one')
    if not row:raise ValueError('Merge review expired, changed or already used.')
    if body['decision']=='cancel':return {'text':'Merge cancelled. No account data moved.'}
    payload=json.loads(A._cipher().decrypt(row['encrypted'].encode()))
    if not hmac.compare_digest(digest(payload),row['content_hash']):raise ValueError('Stored exact review changed.')
    active=db.q('SELECT user_id FROM web_sessions WHERE token_hash=%s AND user_id=%s AND expires_at>now()',(A.digest(header[7:]),user['user_id']),'one')
    if not active:raise ValueError('Original session ended. Start again.')
    A.session(header) # Refresh email-provider disabled/revocation checks when applicable.
    return apply_local_review(user['user_id'],row['target_id'],payload['snapshot_hash'],body['choices'],session_hash=A.digest(header[7:]))


def poll_verified_review(user,header,verifier):
    import cr_web_auth as A
    if not isinstance(verifier,str) or not A.PATTERN.fullmatch(verifier):raise ValueError('Invalid review verifier.')
    init()
    row=db.q('SELECT id,encrypted,content_hash FROM account_merge_reviews WHERE source_id=%s AND session_hash=%s AND challenge=%s AND expires_at>now()', (user['user_id'],A.digest(header[7:]),A.challenge(verifier)),'one')
    if not row:return {'pending':True}
    payload=json.loads(A._cipher().decrypt(row['encrypted'].encode()))
    return {'review_id':row['id'],'hash':row['content_hash'],'text':payload['text'],'conflicts':payload['snapshot']['conflicts']}


def delete_review_data(uid):
    """Erase private review snapshots and unlink retired IDs on account deletion.
    Minimal retired source IDs remain solely to reject delayed/replayed writes.
    """
    init()
    db.q('DELETE FROM account_merge_reviews WHERE source_id=%s OR target_id=%s',(uid,uid),'none')
    db.q('UPDATE account_redirects SET target_id=NULL WHERE target_id=%s',(uid,),'none')
