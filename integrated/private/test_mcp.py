import pytest,cr_mcp as M

def test_default_no_servers(monkeypatch):
    monkeypatch.delenv('CRAYON_MCP_SERVERS',raising=False)
    assert M.catalog()=={}
    with pytest.raises(ValueError):M.call('evil','send',{})

def test_approval_and_url_required(monkeypatch):
    monkeypatch.setenv('CRAYON_MCP_SERVERS','{"x":{"url":"http://localhost","approved":true,"public_readonly":true,"tools":{}}}')
    assert M.catalog()=={}

def test_argument_scope(monkeypatch):
    monkeypatch.setattr(M,'catalog',lambda:{'s':{'tools':{'t':{'fields':['city']}},'url':'https://example.com/mcp'}})
    with pytest.raises(ValueError):M.call('s','t',{'chat_history':'private'})

def test_legacy_handshake_and_public_tool(monkeypatch):
    import cr_web
    monkeypatch.setattr(M,'catalog',lambda:{'s':{'tools':{'weather':{'fields':['city']}},'url':'https://example.com/mcp'}})
    monkeypatch.setattr(cr_web,'_safe_host',lambda host:True)
    calls=[]
    class Response:
        content=b'{}';headers={'content-type':'application/json'}
        def __init__(self,obj):self.obj=obj
        def raise_for_status(self):pass
        def json(self):return self.obj
    class Client:
        def __init__(self,**kw):assert kw['follow_redirects'] is False
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def post(self,url,headers,json):
            calls.append(json)
            result={'protocolVersion':'2025-03-26'} if json['method']=='initialize' else {'tools':[{'name':'weather','annotations':{'readOnlyHint':True}}]} if json['method']=='tools/list' else {'content':[{'type':'text','text':'Sunny'}]}
            return Response({'id':json.get('id'),'result':result})
    monkeypatch.setattr(M.httpx,'Client',Client)
    out=M.call('s','weather',{'city':'Delhi'})
    assert out['verified'] and 'Sunny' in out['untrusted_public_result']
    assert [x['method'] for x in calls]==['initialize','notifications/initialized','tools/list','tools/call']
    assert calls[-1]['params']['arguments']=={'city':'Delhi'}
