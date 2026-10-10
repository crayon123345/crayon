import pytest
import cr_utilities as U

def test_units():
    assert U.convert('1','mi','km')['result']=='1.609344'
    assert U.convert('1','mile','kilometers')['result']=='1.609344'
    assert U.convert('1','hour','minutes')['result']=='6E+1'
    assert U.convert('100','celsius','fahrenheit')['result']=='212'
    assert U.convert('1','lb','g')['result']=='453.59237'

def test_rejects_bad():
    for value,source,target in [('NaN','m','km'),('1','m','kg'),('-274','c','f')]:
        with pytest.raises(ValueError):U.convert(value,source,target)

def test_rate_bound_to_pair(monkeypatch):
    class Response:
        status_code=200
        def json(self):return {'base':'EUR','quote':'USD','date':'2026-10-08','rate':1.1}
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get(self,url):return Response()
    monkeypatch.setattr(U.httpx,'Client',Client)
    assert U.currency('2','EUR','USD')['result']=='2.2'
    with pytest.raises(RuntimeError):U.currency('2','GBP','USD')
