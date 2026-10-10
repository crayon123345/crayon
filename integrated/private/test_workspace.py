import pytest
import cr_workspace as W
import cr_google as G


def test_bad_ranges():
    for r in ('A:A','1:50','A0','A1;DROP','https://example.com','Sheet1!A1:Z999999'):
        with pytest.raises(G.GoogleError):W.range_name(r)


def test_bad_files():
    for r in ('https://evil.com','../secret','abc'):
        with pytest.raises(G.GoogleError):W.file_id(r)


def test_sheet_big_read(monkeypatch):
    monkeypatch.setattr(W,'_call',lambda *a,**k:{'values':[['a']*201]})
    with pytest.raises(G.GoogleError):W.sheet_read(1,'a'*20,'A1')


def test_sheet_write_preview_hash_and_race(monkeypatch):
    monkeypatch.setattr(W,'sheet_read',lambda *a,**k:{'values':[['old']]})
    p=W.sheet_preview(1,'a'*20,'A1',[['=SUM(1,2)']])
    with pytest.raises(G.GoogleError):W.sheet_apply(1,p['payload'],'a'*64)
    monkeypatch.setattr(W,'sheet_read',lambda *a,**k:{'values':[['changed']]})
    with pytest.raises(G.GoogleError):W.sheet_apply(1,p['payload'],p['hash'])


def test_raw_write_verified_and_no_formulas(monkeypatch):
    calls=[]
    responses=iter([{'values':[['old']]},{'values':[['old']]},{'values':[['=SUM(1,2)']]}])
    monkeypatch.setattr(W,'sheet_read',lambda *a,**k:next(responses))
    def call(*a,**k):calls.append(a);return {'updatedCells':1}
    monkeypatch.setattr(W,'_call',call)
    p=W.sheet_preview(1,'a'*20,'A1',[['=SUM(1,2)']])
    result=W.sheet_apply(1,p['payload'],p['hash'])
    assert result['verified']
    assert calls[0][-1]['valueInputOption']=='RAW'


def test_doc_nested_tabs_read(monkeypatch):
    monkeypatch.setattr(W,'_call',lambda *a,**k:{'title':'Test','tabs':[{'documentTab':{'body':{'content':[{'paragraph':{'elements':[{'textRun':{'content':'Hello'}}]}}]}}}]})
    assert W.doc_read(1,'a'*20)['text']=='Hello'
