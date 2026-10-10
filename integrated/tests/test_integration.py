import importlib.util,pathlib,json
R=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('gateway',R/'gateway.py');G=importlib.util.module_from_spec(spec);spec.loader.exec_module(G)
def test_route_boundaries():
 assert G.route('/web/action-confirm')=='private'
 assert G.route('/api/chat')=='public'
 assert G.route('/api/mail/tool')=='public' # removed public route, never mapped to private mail
 assert G.route('/workspace/../../.env')=='assets' # resolved traversal rejected in handler
 assert G.route('/setup/key')=='blocked'
 assert G.route('/telegram/webhook')=='blocked'
def test_session_cache_namespaced():
 s=(R/'public/app.py').read_text()
 assert 'sessions.get((vid, sid))' in s and 'sessions.pop((vid, sid), None)' in s
 assert 'sessions[sid]' not in s
 assert 'sessions.get(sid)' not in s
 assert 'sessions.pop(sid,' not in s
def test_private_actions_not_in_public_model_route():
 s=(R/'public/app.py').read_text()
 assert "if os.environ.get('CRAYON_INTEGRATED')!='on':\n    app.include_router(gmail_auth.router)" in s
 assert "if os.environ.get('CRAYON_INTEGRATED')!='on':\n    gmail_auth.migrate_if_needed()" in s
def test_canonical_workspace():
 assert 'const API=location.origin' in (R/'private/docs/app.js').read_text()
 assert "CRAYON_INTEGRATED_WEB_ONLY" in (R/'private/main.py').read_text()
def test_inventory_honesty():
 x=json.loads((R/'features.json').read_text())
 assert x['cutover'] is False and 'whatsapp_phone_delivery' in x['not_accepted']
def test_canonical_root_personal_base():
 assert G.route('/')=='assets'
 assert G.route('/app.js')=='assets'
 assert G.route('/integration')=='hub'
def test_focus_port_and_private_boundaries():
 s=(R/'private/docs/app.js').read_text()
 assert "Research focus [" in s and "private-read" in s
 assert 'fetch(\'/api/mail/tool' not in s
def test_research_fanout_focus(monkeypatch):
 import sys;sys.path.insert(0,str(R/'private'))
 import cr_web as W
 calls=[]
 monkeypatch.setattr(W,'search',lambda q,n:calls.append(q) or [{'url':'https://example.test/'+str(len(calls))}])
 monkeypatch.setattr(W,'fetch',lambda u,*a:{'url':u,'text':'Verified fixture content'})
 result=W.research('energy','deep')
 assert len(calls)==3 and len(result['pages'])==3
 calls.clear();W.research('energy','academic');assert 'site:edu' in calls[0]
