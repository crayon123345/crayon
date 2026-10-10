"""Per-account bounded outbound computer bridge. No shell or shared files."""
import json,os,time,uuid
import cr_db as db
OWNER=1898030949
def permitted(uid):return type(uid) is int and 0<uid<2**63
SCHEMA="""CREATE TABLE IF NOT EXISTS computer_jobs(id TEXT PRIMARY KEY,operation TEXT NOT NULL,args JSONB NOT NULL,status TEXT NOT NULL DEFAULT 'pending',result JSONB,created_at TIMESTAMPTZ DEFAULT now());"""
def init():
    db.q(SCHEMA,(),"none")
    db.q("ALTER TABLE computer_jobs ADD COLUMN IF NOT EXISTS user_id BIGINT",(),"none")
    db.q("CREATE TABLE IF NOT EXISTS computer_usage(day DATE,user_id BIGINT,used INT DEFAULT 0,PRIMARY KEY(day,user_id))",(),"none")
def reserve(uid):
    # A single transaction lock serializes global/user checks across threads/processes.
    conn=db._connect()
    try:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(39272411)")
            row=conn.execute("SELECT COALESCE(sum(used),0) AS total FROM computer_usage WHERE day=CURRENT_DATE").fetchone()
            own=conn.execute("SELECT used FROM computer_usage WHERE day=CURRENT_DATE AND user_id=%s",(uid,)).fetchone()
            cap=20 if uid==OWNER else 5
            if row['total']>=30 or (own and own['used']>=cap):return False
            conn.execute("INSERT INTO computer_usage(day,user_id,used) VALUES(CURRENT_DATE,%s,1) ON CONFLICT(day,user_id) DO UPDATE SET used=computer_usage.used+1",(uid,))
            conn.execute("DELETE FROM computer_usage WHERE day<CURRENT_DATE-7")
            return True
    finally:conn.close()
def validate(operation,args):
    if operation not in ('status','calculate','write_text','read_text','list_files','browse','form_inspect','form_submit'):raise ValueError('Unsupported computer operation')
    if not isinstance(args,dict):raise ValueError('Invalid arguments')
    if operation in ('form_inspect','form_submit'):
        from computer_forms import validate as form_validate
        form_validate(args.get('config'),args.get('values') if operation=='form_submit' else None)
        if operation=='form_submit' and not __import__('re').fullmatch(r'[a-f0-9]{64}',args.get('expected_hash','')):raise ValueError('Reviewed page hash required')
    if operation=='browse':
        from computer_browser import validate_plan
        validate_plan(args)
    if len(json.dumps(args))>20000:raise ValueError('Computer input too large')
    if operation in ('read_text','write_text'):
        import re
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',args.get('filename','')):raise ValueError('Use a plain filename, no folders')
    if operation=='write_text' and (not isinstance(args.get('text'),str) or len(args['text'])>12000):raise ValueError('Text limit is12000 characters')
    if operation=='calculate' and (not isinstance(args.get('expression'),str) or len(args['expression'])>200):raise ValueError('Use a short arithmetic expression')
    return args

def status(uid):
    if not permitted(uid):return {'ok':False,'error':'A valid authenticated account is required.'}
    row=db.kv_get('computer_heartbeat') or {}
    fresh=db.kv_get('computer_lifecycle_state')!='stopping' and time.time()-row.get('at',0)<60
    return {'ok':fresh,'verified':fresh,'state':'awake' if fresh else 'offline or asleep','computer':row.get('info',{}) if uid==OWNER else {},'note':'Wake-on-demand and idle-stop apply. Free allowance, shared capacity and sleep can delay results.'}

def execute(uid,operation,args,test_wake=False):
    if not permitted(uid):return {'ok':False,'error':'A valid authenticated account is required'}
    validate(operation,args)
    import cr_wake as wake
    # Serialize readiness, activity and enqueue with idle-stop. Never hold while waiting for a job.
    with wake.lock:
        if not status(uid)['ok']:
            try:awake=wake.ensure(lambda:status(uid)['ok'],test=True) if test_wake else wake.ensure(lambda:status(uid)['ok'])
            except Exception as e:return {'ok':False,'verified':False,'error':str(e)[:180]}
            if not awake:return {'ok':False,'error':'Computer did not become ready. Wake/configuration or shared host availability may need administrator recovery. No task completed.'}
        if operation=='browse' and (db.kv_get('computer_heartbeat') or {}).get('info',{}).get('navigation_protocol')!=3:
            return {'ok':False,'verified':False,'error':'Browser worker upgrade pending. No navigation performed.'}
        if operation in ('list_files','read_text','write_text') and (db.kv_get('computer_heartbeat') or {}).get('info',{}).get('account_files_protocol')!=2:
            return {'ok':False,'verified':False,'error':'Private file worker upgrade pending. No file access performed.'}
        if not reserve(uid):return {'ok':False,'error':'Daily computer beta cap reached.5 jobs per user,20 owner,30 total. No quota or paid-budget increase.'}
        wake.touch()
        job=uuid.uuid4().hex
        db.q('INSERT INTO computer_jobs(id,user_id,operation,args) VALUES(%s,%s,%s,%s::jsonb)',(job,uid,operation,json.dumps(args)),'none')
    for _ in range(85 if operation in ('browse','form_inspect','form_submit') else 25):
        row=db.q('SELECT status,result FROM computer_jobs WHERE id=%s AND user_id=%s',(job,uid),'one')
        if row and row['status']=='done':wake.touch();return row['result']
        time.sleep(1)
    db.q("UPDATE computer_jobs SET status='expired' WHERE id=%s AND user_id=%s AND status='pending'",(job,uid),'none')
    return {'ok':False,'verified':False,'error':'No completion confirmed. Do not retry writes automatically.'}

def next_job(info):
    # Browser payloads and task text are transient, not durable memory.
    db.q("DELETE FROM computer_jobs WHERE created_at<now()-interval '30 minutes'",(),'none')
    db.kv_set('computer_heartbeat',{'at':time.time(),'info':info})
    if info.get('verified') and db.kv_get('computer_lifecycle_state')=='starting':db.kv_set('computer_lifecycle_state','ready')
    supports_files=info.get('account_files_protocol')==2
    supports_navigation=info.get('navigation_protocol')==3
    return db.q("UPDATE computer_jobs SET status='running' WHERE id=(SELECT id FROM computer_jobs WHERE status='pending' AND created_at>now()-interval '40 seconds' AND (%s OR operation!='browse') AND (%s OR operation NOT IN ('read_text','write_text','list_files')) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING id,user_id,operation,args",(supports_navigation,supports_files),'one')

def complete(job,result):
    if not isinstance(result,dict) or len(json.dumps(result))>1500000:raise ValueError('Invalid result')
    db.q("UPDATE computer_jobs SET status='done',result=%s::jsonb WHERE id=%s AND status='running'",(json.dumps(result),job),'none')


def record_boot(body):
    # Only authenticated bridge POST can call this. Never turn log data into instructions.
    from computer_boot import scrub
    if not isinstance(body,dict):raise ValueError('Invalid boot report')
    allowed={'started_at','updated_at','ready_at','ended_at','revision','state','reason','exit_code','pid','worker_pid','log'}
    report={k:v for k,v in body.items() if k in allowed}
    for k,v in report.items():
        if isinstance(v,str):report[k]=scrub(v)[:6000]
        elif not isinstance(v,(int,float,bool)) and v is not None:raise ValueError('Invalid boot field')
    report['received_at']=time.time()
    db.kv_set('computer_boot_report',report)
    return {'ok':True}
