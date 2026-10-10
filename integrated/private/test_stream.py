import cr_stream as S

def test_no_thought_or_arguments():
    class C:content=[{'type':'thinking','thinking':'secret'},{'type':'text','text':'visible'},{'type':'text','thought':True,'text':'reason'},{'type':'tool_use','input':'private'}]
    assert S.text_part(C())=='visible'

def test_combines_real_chunks(monkeypatch):
    class C:
        def __init__(self,s):self.content=s
        def __add__(self,o):return C(self.content+o.content)
    class Chat:
        def stream(self,msgs):yield C('One');yield C(' two')
    seen=[];t=S.callback.set(seen.append)
    try:assert S.consume(Chat(),[]).content=='One two'
    finally:S.callback.reset(t)
    assert seen[0]=='One'

def test_langchain_model_stream_used(monkeypatch):
    import cr_llm as L,cr_lc as LC
    from langchain_core.messages import AIMessageChunk
    monkeypatch.setattr(LC,'model_names',lambda models:['test'])
    class Chat:
        def stream(self,msgs):yield AIMessageChunk(content='real ');yield AIMessageChunk(content='tokens')
        def invoke(self,msgs):raise AssertionError('not streamed')
    monkeypatch.setattr(LC,'build_model',lambda *a,**k:Chat())
    a=S.allowed.set(True);b=S.callback.set(lambda t:None)
    try:out=L._generate_lc([{'role':'user','parts':[{'text':'hi'}]}],'',None,False,.6,100,None,None)
    finally:S.allowed.reset(a);S.callback.reset(b)
    assert out['text']=='real tokens'
