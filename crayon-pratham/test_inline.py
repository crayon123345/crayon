import io
from PIL import Image
import cr_telegram as T

def test_png_inline_csv_file(monkeypatch):
    calls=[]
    class Resp:
        def json(self):return {'ok':True,'result':{'message_id':1}}
    monkeypatch.setattr(T._http,'post',lambda url,**kw:calls.append((url,kw)) or Resp())
    b=io.BytesIO();Image.new('RGB',(1280,800),'white').save(b,format='PNG')
    T.Out().artifact(1,{'filename':'screen.png','mime':'image/png','data':b.getvalue()})
    assert calls[-1][0].endswith('/sendPhoto') and 'photo' in calls[-1][1]['files']
    T.Out().artifact(1,{'filename':'data.csv','mime':'text/csv','data':b'x,y'})
    assert calls[-1][0].endswith('/sendDocument')
