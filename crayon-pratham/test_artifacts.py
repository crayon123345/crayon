import io,csv
import pytest
from PIL import Image
import cr_artifacts as A

def test_csv_formula_safe():
    rows=list(csv.reader(io.StringIO(A.csv_bytes(['Name','Value'],[['=HYPERLINK("x")','5'],['plain','-4']]).decode('utf-8-sig'))))
    assert rows[1][0].startswith("'=") and rows[2][1]=="'-4"

def test_csv_limits():
    for h,r in [([],[]),(['x'],[['1','2']]),(['x'],[['x'*501]]),(['x'],[['x']]*101)]:
        with pytest.raises(ValueError):A.csv_bytes(h,r)

def test_chart_png():
    b=A.chart_bytes('Study hours',['Maths','Finance','Strategy'],[3,5,2],'hours')
    im=Image.open(io.BytesIO(b));assert im.size==(1000,342)

@pytest.mark.parametrize('values',[[float('nan')],[-1],[1e20]])
def test_chart_rejects(values):
    with pytest.raises(ValueError):A.chart_bytes('Bad',['x'],values)


def test_artifact_transport(monkeypatch):
    import cr_telegram as T
    class Reply:
        def json(self):return {'ok':True,'result':{'message_id':123}}
    class Client:
        def post(self,url,**kw):
            assert url.endswith('/sendDocument')
            assert kw['data']['chat_id']=='10'
            assert kw['files']['document'][0]=='crayon.csv'
            return Reply()
    monkeypatch.setattr(T,'_http',Client())
    assert T.Out().artifact(10,{'filename':'crayon.csv','mime':'text/csv','data':b'a,b'})==123

def test_export_requires_user_request():
    import cr_tools as T
    with pytest.raises(ValueError):T.create_csv({'meta':{'user_text':'hello'}},['x'],[['y']])
    with pytest.raises(ValueError):T.create_bar_chart({'meta':{'user_text':'hello'}},'x',['a'],[1])


def test_real_handle_dispatches_artifact(monkeypatch):
    import cr_telegram as T
    monkeypatch.setattr(T.db,'audit',lambda *a,**k:None)
    monkeypatch.setattr(T,'_over_cap',lambda uid:False)
    monkeypatch.setattr(T.A,'respond',lambda *a:('Here is the file',{'artifacts':[{'filename':'crayon.csv','mime':'text/csv','data':b'x,y'}]}))
    import cr_google_chat as H
    monkeypatch.setattr(H,'handle',lambda *a:False)
    out=T.CaptureOut();T._handle_text(10,10,'Test','create a csv',None,out)
    assert out.sent[-1]['text']=='Attachment: crayon.csv'
