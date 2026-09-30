"""Check portable source-local runtime paths and pairing identity."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

class RuntimePathTests(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location('isolated_bridge', Path(__file__).with_name('bridge.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_runtime_is_source_directory(self):
        self.assertEqual(self.load().ROOT, Path(__file__).resolve().parent)

    def test_explicit_codex_paths(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'CODEX_HOME': directory, 'CODEX_CLI': '/example/codex',
            'CODEX_THREAD_DB': directory+'/threads.sqlite'
        }):
            module=self.load()
            self.assertEqual(module.THREAD_DB, Path(directory)/'threads.sqlite')
            self.assertEqual(module.CODEX, '/example/codex')

    def test_setup_records_bot_identity_for_reply_validation(self):
        import test_bridge
        b = test_bridge.b
        with tempfile.TemporaryDirectory() as directory, patch.object(b,'ROOT',Path(directory)), \
             patch.object(b.getpass,'getpass',return_value='fake:test'), \
             patch('builtins.input',return_value=''), patch('builtins.print'), \
             patch.object(b.secrets,'token_urlsafe',return_value='test-challenge'), \
             patch.object(b,'deliver'):
            class API:
                def __init__(self,cfg): pass
                def call(self,method,**kwargs):
                    if method=='getMe': return {'id':456,'username':'example_bot'}
                    if method=='getWebhookInfo': return {}
                    if method=='getUpdates': return [{'update_id':1,'message':{'text':'/start test-challenge',
                        'chat':{'id':123,'type':'private'},'from':{'id':123,'is_bot':False}}}]
                    raise AssertionError(method)
            with patch.object(b,'API',API): b.setup()
            self.assertEqual(b.config()['bot_id'],456)
            self.assertFalse(b.config()['execution_enabled'])

if __name__=='__main__': unittest.main()
