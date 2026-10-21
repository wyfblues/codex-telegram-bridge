import json
import os
import sys
import socket
import struct
import threading
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import codex_control as control


class ControlTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audit = self.root / 'requests.jsonl'

    def tearDown(self):
        self.temp.cleanup()

    def client(self, mode='normal', timeout=3, sock=None):
        script = self.root / 'fake-codex'
        script.write_text('''#!PYTHON
import json, os, sys, time
mode = MODE
audit = AUDIT
def record(v):
    with open(audit, 'a') as f: f.write(json.dumps(v) + '\\n')
def send(v):
    sys.stdout.write(json.dumps(v) + '\\n'); sys.stdout.flush()
record({'argv': sys.argv[1:]})
if mode == 'missing': sys.exit(2)
for line in sys.stdin:
    request = json.loads(line); record(request)
    if request['method'] == 'initialize':
        if mode == 'init_timeout': time.sleep(5)
        if mode == 'init_rejected':
            send({'id': request['id'], 'error': {'code': -32600, 'message': 'private detail'}})
            continue
        send({'id': request['id'], 'result': {'userAgent': 'test'}})
    elif request['method'] == 'initialized': continue
    elif 'id' not in request: continue
    elif mode == 'timeout': time.sleep(5)
    elif mode == 'disconnect': sys.exit(2)
    elif mode == 'rejected': send({'id':request['id'], 'error': {'code': -32602, 'message':'secret server message'}})
    elif mode == 'malformed':
        sys.stdout.write('not json\\n'); sys.stdout.flush()
    elif mode == 'null': send({'id': request['id'], 'result': None})
    elif mode == 'flood':
        sys.stdout.write('x' * (9 * 1024 * 1024)); sys.stdout.flush()
    elif mode == 'split':
        raw = (json.dumps({'id': request['id'], 'result': {'text': '你好'}},ensure_ascii=False)+'\\n').encode()
        for char in raw: os.write(sys.stdout.fileno(), bytes([char]))
    else:
        send({'method': 'thread/tokenUsage/updated', 'params': {'private':'ignored'}})
        send({'id': 'other', 'result': {'wrong': True}})
        send({'id': request['id'], 'result': {'ok':True,'method':request['method'],'params': request.get('params')}})
'''.replace('PYTHON',sys.executable).replace('MODE', repr(mode)).replace('AUDIT', repr(str(self.audit))))
        script.chmod(0o700)
        return control.ProxyClient(codex=str(script), timeout=timeout, sock=sock)

    def records(self):
        return [json.loads(line) for line in self.audit.read_text().splitlines()]

    def test_uses_proxy_handshake_and_one_requested_rpc(self):
        result = self.client(sock='/tmp/existing.sock').read_thread('thread-one')
        self.assertTrue(result['ok'])
        entries = self.records()
        self.assertEqual(entries[0]['argv'], ['app-server', 'proxy', '--sock', '/tmp/existing.sock'])
        self.assertEqual([r['method'] for r in entries[1:]], ['initialize', 'initialized', 'thread/read'])
        self.assertEqual(entries[-1]['params'], {'threadId':'thread-one', 'includeTurns':False})
        self.assertTrue(entries[1]['params']['capabilities']['experimentalApi'])

    def test_notifications_and_unrelated_responses_are_ignored(self):
        self.assertEqual(self.client().models()['method'], 'model/list')

    def test_utf8_frames_can_be_split_at_any_byte(self):
        self.assertEqual(self.client('split').models(), {'text':'你好'})

    def test_spawn_failure_is_unavailable(self):
        with self.assertRaises(control.ControlError) as caught:
            control.ProxyClient(codex=str(self.root / 'absent')).rate_limits()
        self.assertEqual(caught.exception.code, 'unavailable')
        self.assertFalse(caught.exception.uncertain)

    def test_missing_running_socket_never_launches_a_daemon(self):
        with self.assertRaises(control.ControlError) as caught:
            self.client('missing').interrupt('thread', 'turn')
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]['argv'], ['app-server', 'proxy'])

    def test_read_timeout_is_bounded_and_has_no_mutation_uncertainty(self):
        started = time.monotonic()
        with self.assertRaises(control.ControlError) as caught:
            self.client('timeout', timeout=.6).rate_limits()
        self.assertLess(time.monotonic()-started, 2)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertFalse(caught.exception.uncertain)

    def test_mutation_timeout_is_uncertain_and_never_retried(self):
        with self.assertRaises(control.ControlError) as caught:
            self.client('timeout', timeout=.6).steer('thread','turn','Please wait')
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual([r['method'] for r in self.records()[1:]], ['initialize','initialized','turn/steer'])

    def test_initialization_timeout_cannot_have_submitted_mutation(self):
        with self.assertRaises(control.ControlError) as caught:
            self.client('init_timeout', timeout=.6).interrupt('thread','turn')
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual([r['method'] for r in self.records()[1:]], ['initialize'])

    def test_server_rejection_is_definite_and_redacted(self):
        with self.assertRaises(control.ControlError) as caught:
            self.client('rejected').interrupt('thread','turn')
        self.assertEqual(caught.exception.code, 'rejected')
        self.assertEqual(caught.exception.rpc_code, -32602)
        self.assertFalse(caught.exception.uncertain)
        self.assertNotIn('secret', str(caught.exception))

    def test_disconnect_after_mutation_is_uncertain(self):
        with self.assertRaises(control.ControlError) as caught:
            self.client('disconnect').update_settings('thread', effort='high')
        self.assertEqual(caught.exception.code, 'disconnected')
        self.assertTrue(caught.exception.uncertain)

    def test_malformed_result_after_mutation_is_uncertain(self):
        for mode in ['malformed','null']:
            with self.subTest(mode=mode), self.assertRaises(control.ControlError) as caught:
                self.client(mode).interrupt('thread','turn')
            self.assertTrue(caught.exception.uncertain)
            self.assertEqual(caught.exception.code, 'protocol')

    def test_output_is_bounded(self):
        with self.assertRaises(control.ControlError) as caught:
            self.client('flood').models()
        self.assertEqual(caught.exception.code,'protocol')

    def test_daemon_and_thread_lifecycle_methods_are_not_allowed(self):
        for method in ['thread/start','thread/resume','turn/start','account/login/start','config/value/write']:
            with self.subTest(method=method), self.assertRaises(control.ControlError) as caught:
                self.client().rpc(method,{})
            self.assertEqual(caught.exception.code,'unsupported_method')
        self.assertFalse(self.audit.exists())

    def test_invalid_and_large_payloads_never_start_proxy(self):
        client=self.client()
        for params in [[], {'text':float('nan')}, {'text':'x'*control.MAX_REQUEST_BYTES}]:
            with self.subTest(params_type=type(params)), self.assertRaises(control.ControlError):
                client.rpc('turn/steer',params)
        self.assertFalse(self.audit.exists())

    def test_invalid_timeout_rejected(self):
        for timeout in [0,-1,31,float('nan'),float('inf'),'3']:
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                control.ProxyClient(timeout=timeout)


class ControlWrapperTests(unittest.TestCase):
    def setUp(self):
        self.client=control.ProxyClient()
        self.rpc=patch.object(self.client,'rpc',return_value={}).start()
        self.addCleanup(patch.stopall)

    def test_steer_has_exact_turn_precondition_and_client_marker(self):
        self.rpc.return_value={'turnId':'turn-B'}
        self.client.steer('thread-A','turn-B','Change the requirement',request_id='telegram-request-123')
        self.rpc.assert_called_once_with('turn/steer',{
            'threadId':'thread-A','expectedTurnId':'turn-B',
            'input':[{'type':'text','text':'Change the requirement'}],
            'clientUserMessageId':'telegram-request-123'})

    def test_steer_missing_or_different_receipt_is_uncertain_and_never_retried(self):
        for receipt in ({},{'turnId':''},{'turnId':'different'}):
            with self.subTest(receipt=receipt):
                self.rpc.reset_mock()
                self.rpc.return_value=receipt
                with self.assertRaises(control.ControlError) as caught:
                    self.client.steer('thread-A','turn-B','Requirement')
                self.assertTrue(caught.exception.uncertain)
                self.rpc.assert_called_once()

    def test_interrupt_addresses_exact_thread_and_turn(self):
        self.client.interrupt('thread-A','turn-B')
        self.rpc.assert_called_once_with('turn/interrupt',{'threadId':'thread-A','turnId':'turn-B'})

    def test_settings_only_updates_explicit_model_and_effort(self):
        self.client.update_settings('thread-A',model='gpt-model')
        self.rpc.assert_called_once_with('thread/settings/update',{'threadId':'thread-A','model':'gpt-model'})
        self.rpc.reset_mock()
        self.client.update_settings('thread-A',effort='high')
        self.rpc.assert_called_once_with('thread/settings/update',{'threadId':'thread-A','effort':'high'})

    def test_empty_identifiers_and_empty_steering_are_rejected(self):
        for call in [lambda:self.client.read_thread(''),
                     lambda:self.client.steer('a','b',' '),
                     lambda:self.client.interrupt('a','b\nc'),
                     lambda:self.client.update_settings('a')]:
            with self.assertRaises(control.ControlError):call()
        self.rpc.assert_not_called()

    def test_list_pagination_and_rate_limits_have_bounded_request_scope(self):
        self.client.models('next')
        self.rpc.assert_called_with('model/list',{'cursor':'next','limit':100,'includeHidden':False})
        self.client.rate_limits()
        self.rpc.assert_called_with('account/rateLimits/read',{'excludeResetCreditDetails':True})


class DesktopControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=str(Path(self.temp.name)/'ipc.sock')
        self.server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(1)
        self.addCleanup(self.server.close)
        self.requests=[]
        self.discovery=[]
        self.mode='normal'
        self.thread=None

    @staticmethod
    def send(connection, value):
        raw=json.dumps(value).encode()
        connection.sendall(struct.pack('<I',len(raw))+raw)

    @staticmethod
    def read(connection):
        def readn(count):
            data=b''
            while len(data)<count:
                chunk=connection.recv(count-len(data))
                if not chunk:raise EOFError
                data+=chunk
            return data
        return json.loads(readn(struct.unpack('<I',readn(4))[0]))

    def serve(self):
        connection,_=self.server.accept()
        with connection:
            try:
                while True:
                    request=self.read(connection)
                    if request['type']=='client-discovery-response':
                        self.discovery.append(request)
                        continue
                    self.requests.append(request)
                    method=request['method']
                    result={'clientId':'client-test'} if method=='initialize' else {}
                    owner='client-test' if method=='initialize' else 'owner-test'
                    if method=='thread-owner-discovery' and self.mode=='not_loaded':
                        self.send(connection,{'type':'response','requestId':request['requestId'],
                                              'resultType':'error','error':'no-client-found'})
                        continue
                    if method.startswith('thread-follower-'):
                        if self.mode=='timeout':time.sleep(.6);return
                        if self.mode=='disconnect':return
                        if self.mode=='router_reject' or self.mode=='runtime_error':
                            self.send(connection,{'type':'response','requestId':request['requestId'],
                                'resultType':'error','error':'no-handler-for-request' if self.mode=='router_reject' else 'private runtime detail'})
                            continue
                        if self.mode=='wrong_owner':owner='different-owner'
                        self.send(connection,{'type':'client-discovery-request','requestId':'discovery',
                                              'request':{'method':'thread-owner-discovery'}})
                        if method=='thread-follower-read-model-settings':
                            result={'settings':{'model':'gpt-model','reasoningEffort':'high','resumeState':'resumed'}}
                        elif method=='thread-follower-interrupt-turn':
                            result={'ok':True,'interruptedTurnId': None if self.mode=='stale' else request['params']['expectedTurnId']}
                        elif method=='thread-follower-update-thread-settings':
                            result={'applied':self.mode!='stale'}
                        elif method=='thread-follower-steer-turn':
                            result={'result':{'turnId':'accepted-turn'}}
                    self.send(connection,{'type':'response','requestId':request['requestId'],
                        'resultType':'success','method':method,'handledByClientId':owner,'result':result})
            except (EOFError,OSError):pass

    def client(self, mode='normal'):
        self.mode=mode
        self.thread=threading.Thread(target=self.serve,daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join,1)
        return control.DesktopClient(sock=self.path,timeout=.3)

    def test_read_settings_pins_the_discovered_owner(self):
        settings=self.client().read_settings('original-thread')
        self.thread.join(.2)
        self.assertEqual(settings['model'],'gpt-model')
        self.assertEqual([r['method'] for r in self.requests],
                         ['initialize','thread-owner-discovery','thread-follower-read-model-settings'])
        self.assertEqual(self.requests[-1]['targetClientId'],'owner-test')
        self.assertEqual(self.requests[-1]['version'],1)
        self.assertEqual(self.requests[-1]['sourceClientId'],'client-test')
        self.assertFalse(self.discovery[0]['response']['canHandle'])

    def test_interrupt_has_exact_turn_guard_and_known_result(self):
        result=self.client().interrupt('original-thread','original-turn')
        self.assertEqual(result,{'ok':True,'interruptedTurnId':'original-turn','goalPauseFailed':False})
        request=self.requests[-1]
        self.assertEqual(request['version'],4)
        self.assertEqual(request['params'],{'conversationId':'original-thread','mode':'user-stop','expectedTurnId':'original-turn'})

    def test_settings_preserve_omitted_fields_and_use_compare_condition(self):
        self.client().update_settings('thread',effort='ultra',expected={'model':'old-model','reasoningEffort':'high'})
        request=self.requests[-1]
        self.assertEqual(request['version'],2)
        self.assertEqual(request['params']['threadSettings'],{'effort':'ultra'})
        self.assertEqual(request['params']['condition'],{'ifModelEquals':'old-model','ifEffortEquals':'high'})
        self.assertIsNone(request['params']['activeTurnId'])

    def test_explicit_service_tier_clear_is_distinct_from_omission(self):
        self.client().update_settings('thread',service_tier=None)
        self.assertEqual(self.requests[-1]['params']['threadSettings'],{'serviceTier':None})

    def test_steer_stays_in_original_thread_and_reports_ipc_turn_limit(self):
        result=self.client().steer('original-thread','requested-turn','New direction',request_id='telegram-123')
        request=self.requests[-1]
        self.assertEqual(request['params']['conversationId'],'original-thread')
        self.assertEqual(request['params']['clientUserMessageId'],'telegram-123')
        self.assertEqual(request['params']['restoreMessage']['text'],'New direction')
        self.assertEqual(result,{'turnId':'accepted-turn','requestedTurnId':'requested-turn','atomicTurnGuard':False})
        self.assertNotIn('expectedTurnId',request['params'])

    def test_unknown_delivery_does_not_retry_or_discover_another_owner(self):
        with self.assertRaises(control.ControlError) as caught:
            self.client('disconnect').interrupt('thread','turn')
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(self.requests),3)

    def test_desktop_timeout_is_bounded_and_uncertain_after_mutation(self):
        start=time.monotonic()
        with self.assertRaises(control.ControlError) as caught:
            self.client('timeout').interrupt('thread','turn')
        self.assertLess(time.monotonic()-start,.5)
        self.assertEqual(caught.exception.code,'timeout')
        self.assertTrue(caught.exception.uncertain)

    def test_router_rejection_is_definite(self):
        with self.assertRaises(control.ControlError) as caught:
            self.client('router_reject').interrupt('thread','turn')
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.code,'rejected')

    def test_runtime_error_can_follow_applied_mutation_and_is_redacted(self):
        with self.assertRaises(control.ControlError) as caught:
            self.client('runtime_error').interrupt('thread','turn')
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn('private',str(caught.exception))

    def test_no_owner_never_resumes_thread(self):
        with self.assertRaises(control.ControlError) as caught:
            self.client('not_loaded').interrupt('thread','turn')
        self.assertEqual(caught.exception.code,'not_loaded')
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(len(self.requests),2)

    def test_stale_turn_is_not_reported_as_stopped(self):
        with self.assertRaises(control.ControlError) as caught:
            self.client('stale').interrupt('thread','turn')
        self.assertEqual(caught.exception.code,'stale_turn')

    def test_stale_settings_are_not_reported_as_applied(self):
        with self.assertRaises(control.ControlError) as caught:
            self.client('stale').update_settings('thread',model='new-model')
        self.assertEqual(caught.exception.code,'stale_settings')

    def test_reply_from_different_owner_is_not_accepted(self):
        with self.assertRaises(control.ControlError) as caught:
            self.client('wrong_owner').interrupt('thread','turn')
        self.assertEqual(caught.exception.code,'protocol')
        self.assertTrue(caught.exception.uncertain)


class RuntimeClientTests(unittest.TestCase):
    def test_failed_desktop_mutation_never_falls_back_to_proxy(self):
        client=control.Client()
        with patch.object(client,'_desktop_available',return_value=True), \
             patch.object(client.desktop,'interrupt',side_effect=control.ControlError('timeout',uncertain=True)), \
             patch.object(client.proxy,'interrupt') as proxy:
            with self.assertRaises(control.ControlError):client.interrupt('thread','turn')
        proxy.assert_not_called()

    def test_catalog_server_rejects_every_thread_or_mutation_method(self):
        client=control.CatalogClient()
        for method in ['thread/read','thread/resume','turn/start','turn/steer','thread/settings/update']:
            with self.subTest(method=method), self.assertRaises(control.ControlError) as caught:
                client.rpc(method,{})
            self.assertEqual(caught.exception.code,'unsupported_method')


if __name__=='__main__':
    unittest.main()
