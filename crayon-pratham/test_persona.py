import cr_persona as P

def test_style_not_authority(monkeypatch):
    monkeypatch.setattr(P,'preferences',lambda uid:{'style':'coach','nickname':'Sam'})
    assert 'Sam' in P.instruction(1) and 'Never invent' in P.instruction(1)

def test_group_private():
    class Out:
        def send(self,c,t):self.text=t
    out=Out();assert P.handle(1,-2,'/persona coach',out)
    assert 'private' in out.text
