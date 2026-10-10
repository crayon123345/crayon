"""Explicit shared public-context rooms, never personal memory or connected accounts."""
import hashlib,secrets
import cr_db as db
import cr_web_auth as A
from cr_safety import looks_like_secret

def init():
    db.q('''CREATE TABLE IF NOT EXISTS web_rooms(id TEXT PRIMARY KEY,name TEXT NOT NULL,
      owner_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
      invite_hash TEXT NOT NULL,created_at TIMESTAMPTZ DEFAULT now());
      CREATE TABLE IF NOT EXISTS web_room_members(room_id TEXT NOT NULL REFERENCES web_rooms(id) ON DELETE CASCADE,
      user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,PRIMARY KEY(room_id,user_id));
      CREATE TABLE IF NOT EXISTS web_room_messages(id BIGSERIAL PRIMARY KEY,
      room_id TEXT NOT NULL REFERENCES web_rooms(id) ON DELETE CASCADE,
      author_id BIGINT,role TEXT NOT NULL,encrypted TEXT NOT NULL,created_at TIMESTAMPTZ DEFAULT now());''',fetch='none')

def allowed(uid,room):
    init()
    if not isinstance(room,str) or not A.PATTERN.fullmatch(room):raise ValueError('Invalid room ID')
    row=db.q('SELECT r.id,r.name,r.owner_id FROM web_rooms r JOIN web_room_members m ON m.room_id=r.id WHERE r.id=%s AND m.user_id=%s',(room,uid),'one')
    if not row:raise ValueError('You are not a member of this room.')
    return row

def list_for(uid):
    init()
    rows=db.q('SELECT r.id,r.name,r.owner_id FROM web_rooms r JOIN web_room_members m ON m.room_id=r.id WHERE m.user_id=%s ORDER BY r.created_at DESC LIMIT 20',(uid,))
    return [{'id':r['id'],'name':r['name'],'is_owner':r['owner_id']==uid} for r in rows]

AUDIENCE='This room is shared. Every message here is visible to all current and future members. Anyone you give the invite token can join after reviewing this notice. No personal memory, files, Gmail, Calendar or other connected-account data is imported. Room answers support public information only; personal actions are not enabled. Do not post secrets.'

def create(uid,name,accept):
    if accept is not True or not isinstance(name,str) or not 1<=len(name.strip())<=80 or looks_like_secret(name):raise ValueError('Review the shared audience notice and use a short room name.')
    init();conn=db._conn()
    with conn.transaction():
        db.q('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('web-rooms:'+str(uid),),'none')
        if db.q('SELECT count(*) AS n FROM web_room_members WHERE user_id=%s',(uid,),'one')['n']>=10:raise ValueError('Room limit10 per account.')
        room=secrets.token_urlsafe(32);invite=secrets.token_urlsafe(32)
        db.q('INSERT INTO web_rooms(id,name,owner_id,invite_hash) VALUES(%s,%s,%s,%s)',(room,name.strip(),uid,A.digest(invite)),'none')
        db.q('INSERT INTO web_room_members(room_id,user_id) VALUES(%s,%s)',(room,uid),'none')
    return {'id':room,'name':name.strip(),'invite':invite,'notice':AUDIENCE}

def inspect(invite):
    init()
    if not isinstance(invite,str) or not A.PATTERN.fullmatch(invite):raise ValueError('Invalid room invite')
    row=db.q('SELECT id,name FROM web_rooms WHERE invite_hash=%s',(A.digest(invite),),'one')
    if not row:raise ValueError('Room invitation unavailable')
    return {**row,'notice':AUDIENCE}

def join(uid,invite,accept):
    if accept is not True:raise ValueError('Review the shared audience before joining.')
    room=inspect(invite)
    with db._conn().transaction():
        db.q('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('web-rooms:'+str(uid),),'none')
        if db.q('SELECT count(*) AS n FROM web_room_members WHERE user_id=%s',(uid,),'one')['n']>=10:raise ValueError('Room limit10 per account.')
        db.q('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('web-room-members:'+room['id'],),'none')
        if db.q('SELECT count(*) AS n FROM web_room_members WHERE room_id=%s',(room['id'],),'one')['n']>=20:raise ValueError('Room member limit20.')
        db.q('INSERT INTO web_room_members(room_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING',(room['id'],uid),'none')
    return room

def history(uid,room):
    allowed(uid,room)
    rows=db.q('SELECT id,author_id,role,encrypted,created_at FROM web_room_messages WHERE room_id=%s ORDER BY id DESC LIMIT 50',(room,))
    import cr_web_app as W
    return [{'id':r['id'],'author_label':('Member '+hashlib.sha256((room+':'+str(r['author_id'])).encode()).hexdigest()[:8]) if r['author_id'] is not None else 'Crayon','role':r['role'],'text':W.decode(r['encrypted'])['text'],'created_at':str(r['created_at'])} for r in reversed(rows)]

def say(uid,room,text):
    allowed(uid,room)
    if not isinstance(text,str) or not 1<=len(text.strip())<=2000 or looks_like_secret(text):raise ValueError('Use a short room message without secrets.')
    if db.q("SELECT count(*) AS n FROM web_room_messages WHERE author_id=%s AND role='user' AND created_at>now()-interval '24 hours'",(uid,),'one')['n']>=30:raise ValueError('Shared-room limit30 messages/account/day.')
    # No ambient room/personal history or tools are supplied to the answerer.
    import cr_group as G,cr_web_app as W
    reply=G.answer({'text':'@crayon_v1_bot '+text})
    with db._conn().transaction():
        db.q('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('web-room-members:'+room,),'none')
        db.q('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('web-room-messages:'+str(uid),),'none')
        allowed(uid,room)
        if db.q("SELECT count(*) AS n FROM web_room_messages WHERE author_id=%s AND role='user' AND created_at>now()-interval '24 hours'",(uid,),'one')['n']>=30:raise ValueError('Shared-room daily limit reached.')
        db.q('INSERT INTO web_room_messages(room_id,author_id,role,encrypted) VALUES(%s,%s,%s,%s)',(room,uid,'user',W.encode({'text':text})),'none')
        db.q('INSERT INTO web_room_messages(room_id,author_id,role,encrypted) VALUES(%s,NULL,%s,%s)',(room,'assistant',W.encode({'text':reply})),'none')
        db.q('DELETE FROM web_room_messages WHERE room_id=%s AND id NOT IN (SELECT id FROM web_room_messages WHERE room_id=%s ORDER BY id DESC LIMIT 500)',(room,room),'none')
    return {'text':reply}

def leave(uid,room):
    r=allowed(uid,room)
    if r['owner_id']==uid:raise ValueError('Owner must use Delete room review instead of leaving.')
    with db._conn().transaction():
        db.q('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('web-room-members:'+room,),'none')
        db.q('DELETE FROM web_room_members WHERE room_id=%s AND user_id=%s',(room,uid),'none')
    return {'text':'Left room. Prior shared messages remain visible to members.'}

def delete(uid,room,accept):
    r=allowed(uid,room)
    if r['owner_id']!=uid or accept is not True:raise ValueError('Only the owner can confirm deletion of this room for everyone.')
    with db._conn().transaction():
        db.q('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('web-room-members:'+room,),'none')
        db.q('DELETE FROM web_rooms WHERE id=%s AND owner_id=%s',(room,uid),'none')
    return {'text':'Room and its stored shared messages deleted.'}
