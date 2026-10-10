from pathlib import Path

def test_send_email_routes_to_review_before_model():
    js=Path('docs/app.js').read_text()
    route=js.index('if(pendingCompose||')
    assert 'prepare|send' in js[route:route+180]
    assert "call('connections')" in js[route:route+900]
    assert "call('compose-preview'" in js[route:route+900]
    assert route < js.index("submitAndWait('chat'")
    assert "if(!c.google)" in js[route:route+900]
    assert 'No email sent.' in js[route:route+900]

def test_chat_fallback_has_no_rooms_deletion_deflection():
    src=Path('cr_web_app.py').read_text()
    route=src.index('(?:send|write|draft|compose|prepare)')
    assert 'exact review' in src[route:route+420]
    assert 'Rooms and deletion' not in src[route:route+420]
