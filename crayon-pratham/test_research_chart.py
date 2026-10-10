import json,pytest
import cr_research_chart as R
def fixture():return [{'total':3,'lastupdated':'2026-07-13'},[{'countryiso3code':c,'date':'2024','indicator':{'id':'NY.GDP.PCAP.CD'},'value':v} for c,v in [('USA',80000),('IND',2000),('CHN',13000)]]]
def test_exact_source_records():
    values,updated=R.extract(json.dumps(fixture()))
    assert values==[2000,13000,80000]
    assert updated=='2026-07-13'
def test_source_mismatch_failclosed():
    for field,value in [('date','2025'),('countryiso3code','GBR'),('value',None),('indicator',{'id':'other'})]:
        f=fixture();f[1][0][field]=value
        with pytest.raises(ValueError):R.extract(json.dumps(f))
def test_duplicate_failclosed():
    f=fixture();f[1][1]=f[1][0]
    with pytest.raises(ValueError):R.extract(json.dumps(f))
def test_recipe_scope(monkeypatch):
    import cr_tools as T,cr_computer as K
    calls=[]
    monkeypatch.setattr(K,'execute',lambda *a:calls.append(a) or {'ok':False})
    ctx={'uid':K.OWNER,'meta':{'user_text':'run world bank research chart demo'}}
    T.computer_browse(ctx,R.URL)
    assert len(calls)==1
    assert ctx['meta']['user_text']=='run world bank research chart demo'
    assert not T.computer_browse(ctx,'https://example.com')['ok']
    assert len(calls)==1
    assert not T.computer_browse(ctx,R.URL,'Other page')['ok']
    assert len(calls)==1
