import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
import cr_connections as X
import cr_google as G
from cryptography.fernet import Fernet

class Connections(unittest.TestCase):
    def setUp(self):
        self.env=patch.object(X.C,'env',side_effect=lambda k: {'GOOGLE_TOKEN_ENCRYPTION_KEY':Fernet.generate_key().decode()}.get(k,'client'));self.env.start();self.addCleanup(self.env.stop)
    def test_unknown_provider_closed(self):
        self.assertFalse(X.configured('evil'))
    def test_identity_and_state_scoped(self):
        with patch.object(X.db,'q',return_value={'user_id':77,'encrypted_verifier':'test'}) as q,patch.object(G,'decrypt',return_value={'provider':'workspace','verifier':'a'*64}):
            u=X.authorization_url('a'*40,'workspace');params=parse_qs(urlsplit(u).query)
            self.assertEqual(params['prompt'],['select_account consent'])
            self.assertNotIn('gmail',params['scope'][0]);self.assertNotIn('calendar',params['scope'][0])
            self.assertIn('provider=%s',q.call_args.args[0]);self.assertEqual(q.call_args.args[1][1],'workspace')
    def test_github_narrow(self):
        with patch.object(X.db,'q',return_value={'user_id':77,'encrypted_verifier':'test'}),patch.object(G,'decrypt',return_value={'provider':'github','verifier':'a'*64}):
            params=parse_qs(urlsplit(X.authorization_url('a'*40,'github')).query)
            self.assertEqual(params['scope'],['read:user'])
            self.assertEqual(params['code_challenge_method'],['S256'])
            self.assertEqual(len(params['code_challenge'][0]),43)
    def test_consumed_atomically(self):
        with patch.object(X.db,'q',return_value={'user_id':77}) as q:
            self.assertEqual(X._state('a'*40,'github',True)['user_id'],77)
            self.assertIn('UPDATE',q.call_args.args[0]);self.assertIn('used=false',q.call_args.args[0])
    def test_expired_closed(self):
        with patch.object(X.db,'q',return_value=None):
            with self.assertRaises(G.GoogleError):X._state('a'*40,'github')
    def test_bad_state_closed(self):
        for s in ('','/../','x'*101):
            with self.assertRaises(G.GoogleError):X._state(s,'github')
    def test_provider_confusion_closed(self):
        with patch.object(X.db,'q',return_value={'encrypted_tokens':'abc'}),patch.object(G,'decrypt',return_value={'provider':'workspace','tokens':{}}):
            with self.assertRaises(G.GoogleError):X.access(5,'github')
    def test_reconnect_required_client_changed(self):
        with patch.object(X.db,'q',return_value={'encrypted_tokens':'abc'}),patch.object(G,'decrypt',return_value={'provider':'github','tokens':{'client_id':'old'}}):
            with self.assertRaises(G.GoogleError):X.access(5,'github')
    def test_digest_invalid_repo(self):
        import cr_github
        for r in ('https://github.com/x/y','../secret','user/repo/path','user/repo?x'):
            with self.assertRaises(G.GoogleError):cr_github.digest(77,r)
    def test_new_secrets_redacted(self):
        import os
        import cr_safety
        for k in ('GOOGLE_WORKSPACE_CLIENT_SECRET','GITHUB_OAUTH_CLIENT_SECRET','CRAYON_VAULT_MASTER_KEY'):
            with patch.dict(os.environ,{k:'synthetic-confidential-value'}):
                self.assertEqual(cr_safety.redact('synthetic-confidential-value'),'[redacted]')
    def test_workspace_callback_wrong_scope_never_stores(self):
        class Reply:
            status_code=200
            def json(self):return {'scope':'openid email','refresh_token':'synthetic','access_token':'synthetic'}
        class Client:
            def __init__(self,*a,**k):pass
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def post(self,*a,**k):return Reply()
        with patch.object(X,'_state',return_value={'user_id':77,'encrypted_verifier':'cipher'}),patch.object(G,'decrypt',return_value={'provider':'workspace','verifier':'v'}),patch.object(X.httpx,'Client',Client),patch.object(X.db,'q') as q:
            with self.assertRaises(G.GoogleError):X.complete('a'*40,'code','workspace')
            q.assert_not_called()
    def test_github_callback_broad_scope_never_stores(self):
        class Reply:
            status_code=200
            def json(self):return {'scope':'repo,read:user','access_token':'synthetic'}
        class Client:
            def __init__(self,*a,**k):pass
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def post(self,*a,**k):return Reply()
        with patch.object(X,'_state',return_value={'user_id':77,'encrypted_verifier':'cipher'}),patch.object(G,'decrypt',return_value={'provider':'github','verifier':'v'}),patch.object(X.httpx,'Client',Client),patch.object(X.db,'q') as q:
            with self.assertRaises(G.GoogleError):X.complete('a'*40,'code','github')
            q.assert_not_called()
