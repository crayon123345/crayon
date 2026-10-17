"""Synthetic transport/security tests. No Discord, Google or model calls."""
import json
import os
import unittest
from unittest.mock import Mock,patch
import cr_discord as D

USER='123456789012345678';CHANNEL='223456789012345678';MESSAGE='323456789012345678'
UID=1000000000000042
ENV={'DISCORD_BOT_TOKEN':'synthetic-token-never-real','CRAYON_DISCORD_ENABLED':'on','CRAYON_DISCORD_ALLOWED_USERS':USER}

class DiscordTests(unittest.TestCase):
    def test_ids_reject_telegram_numbers(self):
        for value in ('1898030949','-10012345678',0,None,'01','1e18','9'*25):
            with self.assertRaises(ValueError):D.snowflake(value)
        self.assertEqual(D.snowflake(USER),USER)

    def test_unconfigured_is_dormant(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertFalse(D.configured());self.assertIsNone(D.destination(UID))

    def test_token_alone_cannot_enable(self):
        with patch.dict(os.environ,{'DISCORD_BOT_TOKEN':'test'},clear=True):
            self.assertFalse(D.configured());self.assertIsNone(D.destination(UID))

    def test_config_needs_database(self):
        with patch.dict(os.environ,ENV,clear=True),patch.object(D.db,'available',return_value=False):self.assertFalse(D.configured())

    def test_config_needs_allowlist(self):
        with patch.dict(os.environ,{'DISCORD_BOT_TOKEN':'test','CRAYON_DISCORD_ENABLED':'on'},clear=True):self.assertFalse(D.configured())

    def test_allowlist_is_exact(self):
        with patch.dict(os.environ,ENV,clear=True):
            self.assertEqual(D.allowed_users(),{USER})
            with self.assertRaises(ValueError):D.account('423456789012345678',CHANNEL)

    def test_disabled_destination_does_not_query(self):
        with patch.dict(os.environ,{},clear=True),patch.object(D.db,'q') as q:
            self.assertIsNone(D.destination(UID));q.assert_not_called()

    def test_destination_rechecks_allowlist(self):
        with patch.dict(os.environ,ENV,clear=True),patch.object(D.db,'available',return_value=True),patch.object(D.db,'q',return_value={'subject':'423456789012345678','dm_channel':CHANNEL}):self.assertIsNone(D.destination(UID))

    def test_destination_database_error_not_misclassified_as_web(self):
        with patch.dict(os.environ,ENV,clear=True),patch.object(D.db,'available',return_value=True),patch.object(D.db,'q',side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):D.destination(UID)

    def test_unicode_chunk_cap_and_exact_reassembly(self):
        for text in ('x'*6000,'😀'*2500,'line\n'*1500):
            parts=list(D.chunks(text));self.assertEqual(''.join(parts),text)
            self.assertTrue(all(len(p.encode('utf-16-le'))//2<=1900 for p in parts))

    def test_transport_disables_mentions_and_no_retries(self):
        client=Mock();client.request.return_value.status_code=200;client.request.return_value.json.return_value={'id':MESSAGE}
        with patch.dict(os.environ,ENV,clear=True):D.Transport(client).message(CHANNEL,'@everyone <@123>')
        body=client.request.call_args.kwargs['json']
        self.assertEqual(body['allowed_mentions'],{'parse':[],'replied_user':False});self.assertTrue(body['enforce_nonce']);self.assertEqual(client.request.call_count,1)

    def test_transport_failure_one_attempt(self):
        client=Mock();client.request.return_value.status_code=429
        with patch.dict(os.environ,ENV,clear=True),self.assertRaises(RuntimeError):D.Transport(client).message(CHANNEL,'hello')
        self.assertEqual(client.request.call_count,1)

    def test_transport_timeout_no_retry(self):
        client=Mock();client.request.side_effect=TimeoutError
        with patch.dict(os.environ,ENV,clear=True),self.assertRaises(TimeoutError):D.Transport(client).message(CHANNEL,'hello')
        self.assertEqual(client.request.call_count,1)

    def test_artifact_rejects_bad_name_and_large_file(self):
        out=D.Out(UID,CHANNEL,Mock())
        for name,data in (('../secret',b'x'),('x',b'x'*2000001)):
            with self.assertRaises(ValueError):out.artifact(UID,{'filename':name,'data':data,'mime':'text/plain'})
        out.transport.message.assert_not_called()

    def test_artifact_audience_bound(self):
        out=D.Out(UID,CHANNEL,Mock())
        with self.assertRaises(ValueError):out.artifact(UID+1,{'filename':'a.txt','data':b'x','mime':'text/plain'})

    def test_text_audience_bound(self):
        out=D.Out(UID,CHANNEL,Mock())
        with self.assertRaises(ValueError):out.send(UID+1,'private')
        out.transport.message.assert_not_called()

    def test_no_false_delete_or_reaction_receipt(self):
        out=D.Out(UID,CHANNEL,Mock());self.assertFalse(out.delete(UID,MESSAGE));self.assertFalse(out.react(UID,MESSAGE,'👍'))
        out.transport.request.assert_not_called()

    def test_review_controls_after_entire_text(self):
        transport=Mock();transport.message.return_value=MESSAGE;out=D.Out(UID,CHANNEL,transport)
        with patch.object(D.db,'q') as q,patch.object(D,'encrypt',side_effect=lambda x:'encrypted:'+x):
            out.send(UID,'a'*3000,{'inline_keyboard':[[{'text':'Send','callback_data':'email_send:abc:hash'}]]})
        self.assertEqual(transport.message.call_count,3)
        buttons=transport.message.call_args.kwargs['components'][0]['components']
        self.assertTrue(buttons[0]['custom_id'].startswith('crayon:'))
        self.assertNotIn('email_send',json.dumps(buttons));self.assertIn("state='ready'",q.call_args.args[0])

    def test_text_failure_never_creates_controls(self):
        transport=Mock();transport.message.side_effect=RuntimeError;out=D.Out(UID,CHANNEL,transport)
        with patch.object(D.db,'q') as q,self.assertRaises(RuntimeError):out.send(UID,'private',{'inline_keyboard':[[{'text':'Send','callback_data':'email_send:abc:hash'}]]})
        q.assert_not_called()

    def test_review_publication_failure_marks_uncertain(self):
        transport=Mock();transport.message.side_effect=[MESSAGE,RuntimeError()];out=D.Out(UID,CHANNEL,transport)
        with patch.object(D.db,'q') as q,patch.object(D,'encrypt',return_value='encrypted'),self.assertRaises(RuntimeError):out.send(UID,'private',{'inline_keyboard':[[{'text':'Send','callback_data':'email_send:a:b'}]]})
        self.assertIn("state='uncertain'",q.call_args.args[0])

    def test_callback_rejects_malformed(self):
        with patch.object(D.db,'q') as q:
            self.assertFalse(D.callback(UID,CHANNEL,MESSAGE,'email_send:injected',Mock()))
        q.assert_not_called()

    def test_callback_sql_binds_all_dimensions_and_expiry(self):
        with patch.object(D.db,'q',return_value=None) as q:
            self.assertFalse(D.callback(UID,CHANNEL,MESSAGE,'crayon:'+'A'*32,Mock()))
        sql,args=q.call_args.args[:2]
        for needle in ('user_id=%s','channel_id=%s','message_id=%s',"state='ready'",'expires_at>now()'):self.assertIn(needle,sql)
        self.assertEqual(args,('A'*32,UID,CHANNEL,MESSAGE))

    def test_callback_one_shot_claim_and_no_telegram_ack(self):
        out=D.Out(UID,CHANNEL,Mock())
        with patch.object(D.db,'q',side_effect=[{'encrypted':'test'},None]),patch.object(D,'decrypt',return_value='ux:keep'),patch('cr_telegram.api') as tg_api:
            self.assertTrue(D.callback(UID,CHANNEL,MESSAGE,'crayon:'+'A'*32,out))
        tg_api.assert_not_called()

    def test_callback_failure_is_uncertain_not_retry(self):
        with patch.object(D.db,'q',side_effect=[{'encrypted':'test'},None]) as q,patch.object(D,'decrypt',return_value='ux:keep'),patch('cr_telegram.handle_callback',side_effect=RuntimeError),self.assertRaises(RuntimeError):D.callback(UID,CHANNEL,MESSAGE,'crayon:'+'A'*32,Mock())
        self.assertIn("state='uncertain'",q.call_args.args[0])

    def test_cleanup_all_discord_owned_records(self):
        with patch.object(D.db,'q') as q:D.cleanup(UID)
        deletes=[x.args for x in q.call_args_list if x.args[0].startswith('DELETE')]
        self.assertEqual(len(deletes),3);self.assertTrue(all(x[1]==(UID,) for x in deletes))

    def test_dedupe_atomic(self):
        with patch.object(D.db,'q',return_value=None) as q:self.assertFalse(D.claim(MESSAGE,UID))
        self.assertIn('ON CONFLICT DO NOTHING',q.call_args.args[0])

    def test_recovery_never_replays(self):
        with patch.object(D.db,'available',return_value=True),patch('cr_accounts.init'),patch.object(D.db,'q') as q:D.init()
        sql='\n'.join(x.args[0] for x in q.call_args_list)
        self.assertIn("SET state='uncertain' WHERE state='processing'",sql);self.assertNotIn("SET state='queued'",sql)

    def test_rate_limit_eight_per_minute(self):
        with patch.object(D.time,'monotonic',return_value=10):
            self.assertFalse(any(D.limited('test-rate') for _ in range(8)));self.assertTrue(D.limited('test-rate'))
        with patch.object(D.time,'monotonic',return_value=71):self.assertFalse(D.limited('test-rate'))

    def test_media_url_rejects_arbitrary_hosts(self):
        for url in ('http://cdn.discordapp.com/attachments/a','https://localhost/a','https://cdn.discordapp.com.evil.test/attachments/a','https://user:pass@cdn.discordapp.com/attachments/a','https://cdn.discordapp.com/notattachments/a'):
            with self.assertRaises(ValueError):D.media(UID,'name',{'url':url,'size':1,'filename':'a'},'',Mock())

    def test_media_size_cap_before_fetch(self):
        for size in (0,20000001,True):
            with self.assertRaises(ValueError):D.media(UID,'name',{'url':'https://cdn.discordapp.com/attachments/a','size':size,'filename':'a'},'',Mock())

    def test_text_input_cap_no_engine(self):
        out=Mock()
        with patch('cr_telegram._handle_text') as handler:D.dispatch_text(UID,'name','a'*4001,MESSAGE,out)
        handler.assert_not_called();out.send.assert_called_once()

    def test_quote_same_channel_only(self):
        msg=Mock();msg.channel.id=1;msg.reference.resolved.channel.id=2
        self.assertIsNone(D.quote_context(msg))

    def test_help_does_not_import_telegram_history(self):
        out=Mock();D.dispatch_text(UID,'name','help',MESSAGE,out)
        self.assertIn('not imported',out.send.call_args.args[1])

class ClientConstructionTests(unittest.TestCase):
    def test_native_command_registered_without_network(self):
        client=D.build_client()
        command=client.crayon_tree.get_commands()[0]
        self.assertEqual(command.name,'crayon')
        self.assertEqual([p.name for p in command.parameters],['text','attachment'])
        self.assertTrue(client.intents.message_content)
        self.assertFalse(client.allowed_mentions.everyone)
        self.assertFalse(client.allowed_mentions.users)
        self.assertFalse(client.allowed_mentions.roles)

if __name__=='__main__':unittest.main()
