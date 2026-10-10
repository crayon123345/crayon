"""Run in Codespaces. Outbound-only, fixed bounded operations, isolated data folder."""
import ast,json,math,operator,os,pathlib,platform,time,urllib.request
BASE=os.environ.get('CRAYON_BRIDGE_URL','https://crayon-v1.onrender.com').rstrip('/')
TOKEN=os.environ.get('CRAYON_BRIDGE_TOKEN') or (pathlib.Path.home()/'.config/crayon/bridge-token').read_text().strip()
ROOT=pathlib.Path.home()/'crayon-files';ROOT.mkdir(exist_ok=True)
def arithmetic(text):
    nodes=ast.parse(text,mode='eval');budget=[0]
    ops={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv,ast.Mod:operator.mod}
    def calc(n):
        budget[0]+=1
        if budget[0]>80:raise ValueError('Too many terms')
        if isinstance(n,ast.Constant) and type(n.value) in (int,float):v=n.value
        elif isinstance(n,ast.UnaryOp) and isinstance(n.op,(ast.UAdd,ast.USub)):v=calc(n.operand)*(1 if isinstance(n.op,ast.UAdd) else -1)
        elif isinstance(n,ast.BinOp) and type(n.op) in ops:v=ops[type(n.op)](calc(n.left),calc(n.right))
        else:raise ValueError('Only basic arithmetic, no code execution')
        if not math.isfinite(v) or abs(v)>1e15:raise ValueError('Number too large')
        return v
    return calc(nodes.body)
def file_root(uid):
    # Ownership comes only from the authenticated bridge job envelope, never args.
    if type(uid) is not int or not 0<uid<2**63:raise ValueError('Authenticated file owner required')
    if ROOT.is_symlink():raise ValueError('Links are blocked')
    if uid==1898030949:return ROOT
    users=ROOT/'accounts'
    if users.is_symlink():raise ValueError('Links are blocked')
    users.mkdir(mode=0o700,exist_ok=True)
    folder=users/str(uid)
    if folder.is_symlink():raise ValueError('Links are blocked')
    folder.mkdir(mode=0o700,exist_ok=True)
    return folder

def run(op,args,uid=None):
    import re
    if op in ('form_inspect','form_submit'):
        from computer_forms import run
        return run(args['config'],args.get('values') if op=='form_submit' else None,args.get('expected_hash'))
    if op=='browse':
        from computer_browser import browse
        return browse(args)
    if op=='status':return {'ok':True,'verified':True,'system':platform.system(),'cpu':os.cpu_count(),'python':platform.python_version(),'account_files_protocol':2,'navigation_protocol':3}
    if op=='calculate':return {'ok':True,'verified':True,'value':arithmetic(args['expression'])}
    root=file_root(uid)
    if op=='list_files':return {'ok':True,'verified':True,'files':[p.name for p in root.iterdir() if p.is_file() and not p.is_symlink()][:100]}
    name=args.get('filename','')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',name):raise ValueError('Invalid filename')
    path=root/name
    if path.is_symlink():raise ValueError('Links are blocked')
    if op=='write_text':
        text=args['text']
        if not isinstance(text,str) or len(text)>12000:raise ValueError('Too large')
        if len(list(root.glob('*')))>=100:raise ValueError('Private file limit100 reached')
        if sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file() and not p.is_symlink())+len(text.encode())>10_000_000:raise ValueError('Shared free file storage cap reached')
        # Never overwrite an existing file silently.
        with path.open('x') as f:f.write(text)
        return {'ok':True,'verified':path.read_text()==text,'filename':name,'characters':len(text)}
    if op=='read_text':
        if path.stat().st_size>20000:raise ValueError('Too large')
        return {'ok':True,'verified':True,'filename':name,'text':path.read_text()[:12000]}
    raise ValueError('Unsupported operation')
def post(path,body):
    req=urllib.request.Request(BASE+path,data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=20) as r:return json.load(r)
def main():
    print('Crayon computer worker active. No public ports. Stop with Ctrl+C.',flush=True)
    # Deployment-controlled session cap, never set through a bot job. Default25min.
    raw=int(os.environ.get('CRAYON_WORKER_SESSION_SECONDS','1500'))
    until=time.monotonic()+max(60,min(raw,21600))
    confirmed=False
    while time.monotonic()<until:
        try:
            job=post('/computer/next',run('status',{}))
            if not confirmed:
                print('Crayon bridge heartbeat confirmed',flush=True);confirmed=True
            if job:
                try:result=run(job['operation'],job['args'],job.get('user_id'))
                except Exception as e:result={'ok':False,'verified':False,'error':str(e)[:160]}
                post('/computer/result',{'id':job['id'],'result':result})
        except Exception as e:
            print('Crayon bridge error: '+type(e).__name__+' status='+str(getattr(e,'code','none')),flush=True)
        time.sleep(5)
if __name__=='__main__':main()
