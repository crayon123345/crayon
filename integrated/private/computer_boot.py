"""Stdlib-only boot supervisor. Reports to the existing private bridge, never GitHub."""
import json, os, pathlib, re, subprocess, sys, threading, time, urllib.request, urllib.error
ROOT = pathlib.Path(__file__).resolve().parent
STATE = pathlib.Path.home() / '.config/crayon'
MAX_LOG = 6000

def scrub(text, token=''):
    text = str(text)
    values = [token] + [v for k, v in os.environ.items() if any(s in k.upper() for s in ('TOKEN','SECRET','KEY','PASSWORD','DATABASE_URL')) and len(v)>5]
    for value in sorted(values, key=len, reverse=True):
        if value: text = text.replace(value, '[redacted]')
    text = re.sub(r'(?i)(?:https?://|postgres(?:ql)?://)\S+', '[url removed]', text)
    text = re.sub(r'(?i)(?:github_pat_|gh[pousr]_|AIza|sk-|npg_)[A-Za-z0-9_-]{10,}', '[redacted]', text)
    text = re.sub(r'(?i)(token|password|secret|api.key)\s*[:=]\s*\S+', r'\1=[redacted]', text)
    return text[-MAX_LOG:]

def revision():
    try: return subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, timeout=3, text=True).strip()
    except Exception: return 'unknown'

def publish(report, token):
    safe = {k: scrub(v, token) if isinstance(v,str) else v for k,v in report.items()}
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = STATE / 'boot-report.json'
    with path.open('w') as f: json.dump(safe, f)
    os.chmod(path, 0o600)
    if not token: return False
    base = os.environ.get('CRAYON_BRIDGE_URL','https://crayon-v1.onrender.com').rstrip('/')
    # Boot data and credentials only go to the owner's fixed production bridge.
    if base != 'https://crayon-v1.onrender.com': return False
    try:
        req = urllib.request.Request(base+'/computer/boot', data=json.dumps(safe).encode(), headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=8) as response: return response.status == 200
    except Exception: return False

def run():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = os.environ.get('CRAYON_BRIDGE_TOKEN','')
    if not token:
        try: token=(STATE/'bridge-token').read_text().strip()
        except OSError: pass
    report = {'started_at':time.time(),'revision':revision(),'state':'checking','log':'','pid':os.getpid()}
    enabled = os.environ.get('CRAYON_COMPUTER_AUTOSTART','off')
    if enabled == 'off':
        try: enabled=(STATE/'autostart').read_text().strip()
        except OSError: pass
    if enabled != 'on':
        report.update(state='disabled',reason='autostart_not_on');publish(report,token);return
    if not token:
        report.update(state='failed',reason='bridge_credential_missing');publish(report,token);return
    # Do not spawn a duplicate worker or supervisor.
    try:
        if subprocess.run(['pgrep','-f','^python3 computer_worker.py$'], stdout=subprocess.DEVNULL).returncode == 0:
            report.update(state='already_running',reason='existing_worker');publish(report,token);return
    except OSError: pass
    report.update(state='launching');publish(report,token)
    try:
        child=subprocess.Popen(['python3','computer_worker.py'],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
    except Exception as e:
        report.update(state='failed',reason=type(e).__name__);publish(report,token);return
    report.update(worker_pid=child.pid,state='running_no_heartbeat')
    lock=threading.Lock()
    def capture():
        for line in child.stdout:
            with lock:
                report['log']=scrub(report['log']+line,token)
                if 'Crayon bridge heartbeat confirmed' in line:
                    report.update(state='ready',ready_at=time.time())
    threading.Thread(target=capture,daemon=True).start()
    while child.poll() is None:
        with lock: snapshot=dict(report)
        snapshot['updated_at']=time.time();publish(snapshot,token)
        time.sleep(10)
    with lock: report.update(state='exited',exit_code=child.returncode,ended_at=time.time())
    publish(report,token)

if __name__ == '__main__':
    try: run()
    except Exception as e:
        # A failure of the supervisor itself is kept local without raw exception data.
        try: publish({'state':'supervisor_failed','reason':type(e).__name__,'started_at':time.time(),'revision':revision()},'')
        except Exception: pass
