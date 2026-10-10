import io
import zipfile
import pytest
import cr_media as M


def test_large_limit_and_unknown():
    assert M.MAX_BYTES==20_000_000
    assert "can't decode" in M.analyze(b'abc','application/octet-stream',filename='thing.bin')
    with pytest.raises(ValueError):
        M.analyze(b'x'*(M.MAX_BYTES+1),'text/plain')


def test_zip_listing_does_not_execute(monkeypatch):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        z.writestr('../../attack.sh','echo dangerous')
    text=M.readable_text(b.getvalue(),'application/zip','demo.zip')
    assert 'listing only' in text and 'attack.sh' in text and 'echo dangerous' not in text


def test_docx_extract():
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        z.writestr('word/document.xml','<r><t>Hello office</t></r>')
    assert 'Hello office' in M.readable_text(b.getvalue(),'application/zip','demo.docx')


def test_xml_entities_rejected():
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        z.writestr('word/document.xml','<!DOCTYPE foo><r/>')
    with pytest.raises(ValueError):
        M.readable_text(b.getvalue(),'application/zip','demo.docx')


def test_no_secret_forwarding(monkeypatch):
    monkeypatch.setattr(M.llm,'generate',lambda *a,**kw:pytest.fail('secret sent'))
    with pytest.raises(ValueError):
        M.analyze(b'api_key=sk-abcdefghijklmnopqrstuvwxyz123456789','text/plain')


def test_long_text_discloses_truncation(monkeypatch):
    def fake(contents,**kw):
        assert 'Only the first 24,000' in contents[0]['parts'][0]['text']
        return {'text':'Summary'}
    monkeypatch.setattr(M.llm,'generate',fake)
    assert 'Reading limit:' in M.analyze(b'a'*30000,'text/plain')


def test_caption_media_and_video_routing(monkeypatch):
    import cr_telegram as T
    monkeypatch.setattr(T.db,'kv_set',lambda *a:None)
    seen=[]
    monkeypatch.setattr(T,'_handle_media',lambda *a:seen.append(a))
    T.handle_update({'message':{'chat':{'id':-5},'from':{'id':-5},'text':'caption','video':{'file_id':'abc'}}},T.CaptureOut())
    assert seen


def test_clean_every_sender():
    from cr_safety import clean_text
    from cr_telegram import CaptureOut
    t='## Title\n**Bold** \u2014 \u201cwords\u201d\n\u2022 one'
    assert clean_text(t)=='Title\nBold  -  "words"\n- one'
    o=CaptureOut()
    o.send(-1,t)
    assert '**' not in o.sent[0]['text'] and '\u2014' not in o.sent[0]['text']
