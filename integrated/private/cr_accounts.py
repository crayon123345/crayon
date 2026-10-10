"""Provider-independent IDs. Legacy Telegram storage is never moved by sign-in.
New account IDs lie above provider-number ranges. Email is metadata, never a lookup key.
Populated-account merges are deliberately not implemented here.
"""
import re
import cr_db as db

STANDALONE_MIN=10**15
SCHEMA='''CREATE SEQUENCE IF NOT EXISTS crayon_account_ids START WITH 1000000000000000 MINVALUE 1000000000000000;
CREATE TABLE IF NOT EXISTS account_identities(
 provider TEXT NOT NULL,subject TEXT NOT NULL,user_id BIGINT NOT NULL,
 display_email TEXT NOT NULL DEFAULT '',created_at TIMESTAMPTZ DEFAULT now(),
 PRIMARY KEY(provider,subject),UNIQUE(provider,user_id));'''

def init():
    for s in SCHEMA.split(';'):
        if s.strip():db.q(s,fetch='none')

def standalone(uid):return type(uid) is int and uid>=STANDALONE_MIN

def telegram_destination(uid):
    if not standalone(uid):return uid
    row=db.q("SELECT subject FROM account_identities WHERE provider='telegram' AND user_id=%s",(uid,),'one')
    return int(row['subject']) if row else None

def identity_account(provider,identity):
    """Verified provider subject only. Preserve every previously linked legacy account."""
    if provider not in ('google','firebase_email'):raise ValueError('Unknown identity provider.')
    subject=identity['subject']
    if not isinstance(subject,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,255}',subject):raise ValueError('Invalid verified identity.')
    init()
    # Lock per subject inside one transaction: a race cannot allocate two identities.
    with db._conn().transaction():
        db.q('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('crayon-'+provider+':'+subject,),'none')
        if provider=='google':
            row=db.q('SELECT g.user_id,g.name FROM web_google_identities g JOIN users u ON u.user_id=g.user_id WHERE g.subject=%s',(subject,),'one')
            if row:return row
        row=db.q("SELECT i.user_id,u.name FROM account_identities i JOIN users u ON u.user_id=i.user_id WHERE i.provider=%s AND i.subject=%s",(provider,subject),'one')
        if row:return row
        uid=db.q("SELECT nextval('crayon_account_ids') AS id",fetch='one')['id']
        name=identity.get('google_name',identity.get('name',''))[:100]
        db.q('INSERT INTO users(user_id,name) VALUES(%s,%s)',(uid,name),'none')
        db.q("INSERT INTO account_identities(provider,subject,user_id,display_email) VALUES(%s,%s,%s,%s)",(provider,subject,uid,identity['email']),'none')
        return {'user_id':uid,'name':name}

def link_legacy_google(identity,uid,name):
    """Serialize with first-login creation. Never absorb another populated account."""
    init()
    with db._conn().transaction():
        db.q('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('crayon-google:'+identity['subject'],),'none')
        existing=db.q("SELECT user_id FROM account_identities WHERE provider='google' AND subject=%s",(identity['subject'],),'one')
        if existing:raise ValueError('That Google login already has a Crayon account. Separate accounts need a reviewed merge; nothing was moved.')
        row=db.q('INSERT INTO web_google_identities(subject,user_id,email,name) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING user_id',(identity['subject'],uid,identity['email'],name),'one')
        if not row:raise ValueError('Google or Crayon account already linked. No accounts merged or overwritten.')


def google_account(identity):return identity_account('google',identity)
