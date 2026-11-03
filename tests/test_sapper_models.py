"""Local wire-contract tests; no provider keys or paid inference required."""
import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import sapper_models as models
from sapper_web import Console, WebServer, WebError


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self):
        self.calls = []
        self.fail = False
        self.broken = False
        self.proof = None
        super().__init__(('127.0.0.1', 0), FixtureHandler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
    @property
    def url(self):
        return f'http://127.0.0.1:{self.server_port}/v1'
    def close(self):
        self.shutdown(); self.server_close(); self.thread.join()


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def send_json(self, value, status=200):
        raw=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        self.server.calls.append((self.path,dict(self.headers),None))
        if self.path.endswith('/redirect/models'):
            self.send_response(302);self.send_header('Location',self.server.url+'/models');self.end_headers();return
        if self.server.fail:
            return self.send_json({'error':{'message':'echo synthetic-provider-secret'}},401)
        if 'after_id=' in self.path:
            return self.send_json({'data':[{'id':'model-beta','display_name':'Beta'}],'has_more':False})
        return self.send_json({'data':[{'id':'model-alpha','display_name':'Alpha'},{'id':'model-zeta'}], 'has_more':self.headers.get('anthropic-version') is not None,'last_id':'model-zeta'})
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append((self.path,dict(self.headers),body))
        if self.server.fail:
            return self.send_json({'error':{'message':'echo synthetic-provider-secret'}},401)
        responses=self.path.endswith('/responses')
        tools=body.get('tools',[])
        name='write_proof'
        if self.server.proof:
            names=[t.get('name') or t.get('function',{}).get('name') for t in tools]
            name=next((n for n in ('Write','Bash') if n in names),name)
        history=json.dumps(body.get('input',body.get('messages',[])))
        tool_done='function_call_output' in history or '"role": "tool"' in history
        use_tool=bool(tools) and not tool_done
        args={'file_path':str(self.server.proof),'content':'ORIGINAL_CLAUDE_TOOL_OK\n'} if name=='Write' else {'command':f"printf ORIGINAL_CLAUDE_TOOL_OK > '{self.server.proof}'"} if name=='Bash' else {'value':42}
        tool={'type':'function_call','call_id':'call-fixture','name':name,'arguments':json.dumps(args)}
        answer='OK' if not tool_done else 'Tool result received. ORIGINAL_CLAUDE_TOOL_OK'
        if self.path.endswith('/messages'):
            return self.send_json({'id':'msg-fixture','type':'message','role':'assistant','model':body['model'],'content':[{'type':'text','text':answer}],'stop_reason':'end_turn','usage':{'input_tokens':7,'output_tokens':2}})
        if responses:
            output=[tool] if use_tool else [{'type':'message','role':'assistant','content':[{'type':'output_text','text':answer}]}]
            result={'id':'resp-fixture','status':'completed','output':output,'usage':{'input_tokens':7,'output_tokens':2}}
        else:
            tc={'id':tool['call_id'],'type':'function','function':{'name':name,'arguments':tool['arguments']}}
            result={'choices':[{'message':{'role':'assistant','content':None if use_tool else answer,**({'tool_calls':[tc]} if use_tool else {})},'finish_reason':'tool_calls' if use_tool else 'stop'}],'usage':{'prompt_tokens':7,'completion_tokens':2}}
        if not body.get('stream'):
            return self.send_json(result)
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
        def event(obj):
            self.wfile.write(('data: '+json.dumps(obj)+'\n\n').encode());self.wfile.flush()
        if responses:
            if not use_tool:event({'type':'response.output_text.delta','delta':answer})
            if not self.server.broken:event({'type':'response.completed','response':result})
        else:
            if use_tool:
                for part in (tool['arguments'][:5],tool['arguments'][5:]):
                    event({'choices':[{'delta':{'tool_calls':[{'index':0,**({'id':'call-fixture','function':{'name':name,'arguments':part}} if part==tool['arguments'][:5] else {'function':{'arguments':part}})}]},'finish_reason':None}]})
            else:event({'choices':[{'delta':{'content':answer},'finish_reason':None}]})
            if not self.server.broken:event({'choices':[{'delta':{},'finish_reason':'tool_calls' if use_tool else 'stop'}],'usage':result['usage']})
            self.wfile.write(b'data: [DONE]\n\n')


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='sapper model contract ')
        self.base=Path(self.temp.name)
        self.app=Console(self.base/'tasks',self.base/'private')
        self.upstream=FixtureServer()
    def tearDown(self):
        for ready in (self.app.state/'model-bridges').glob('*/ready.json'):
            with contextlib.suppress(OSError):os.kill(json.loads(ready.read_text())['pid'],signal.SIGTERM)
        self.upstream.close();self.temp.cleanup()
    def profile(self,protocol='responses',**extra):
        return {'name':'fixture','model':'model-alpha','protocol':protocol,'api_key':'synthetic-provider-secret','base_url':self.upstream.url,**extra}
    def bridge(self,protocol):
        server=models.BridgeServer(self.profile(protocol),'local-bridge-token')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        return server
    def call(self,server,body,token='local-bridge-token'):
        req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/messages',data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=5) as r:return r.read().decode()

    def test_migrate_old_profiles_protocol_alias_and_validate(self):
        old=self.app.save_profile({'name':'old','model':'old-model'})
        self.assertEqual(old['protocol'],'anthropic')
        p=self.app.save_profile(self.profile('response'))
        self.assertEqual(p['protocol'],'responses');self.assertNotIn('api_key',p)
        self.app.save_profile({**p,'api_key':''})
        self.assertEqual(self.app.config()['profiles'][-1]['api_key'],'synthetic-provider-secret')
        self.app.save_profile({**p,'clear_key':True})
        self.assertEqual(self.app.config()['profiles'][-1]['api_key'],'')
        for change in ({'protocol':'invalid'},{'base_url':'https://u:p@example.com/v1'},{'base_url':'https://example.com/?key=secret'},{'api_key':'unsafe\nheader'},{'timeout_seconds':1},{'engine_prefix':'wrapper'}):
            with self.subTest(change=change),self.assertRaises((ValueError,WebError)):self.app.save_profile(self.profile(**change))

    def test_draft_models_all_protocols_pagination_keys_and_generation(self):
        for protocol in models.PROTOCOLS:
            p=self.app.save_profile(self.profile(protocol))
            draft={**p,'api_key':''}
            result=self.app.profile_probe(draft,'models')
            self.assertEqual(len(result['models']),3 if protocol=='anthropic' else 2)
            self.assertTrue(self.app.profile_probe(draft,'test')['ok'])
        self.assertTrue(all(c[1]['Authorization']=='Bearer synthetic-provider-secret' for c in self.upstream.calls))
        self.assertNotIn('synthetic-provider-secret',json.dumps(self.app.profiles()))
        self.assertEqual(stat.S_IMODE(self.app.config_path.stat().st_mode),0o600)

    def test_endpoint_normalization_and_transport_errors_do_not_leak_keys(self):
        p=models.resolved(self.profile())
        self.assertEqual(models.endpoint({**p,'base_url':self.upstream.url+'/responses'},'models'),self.upstream.url+'/models')
        self.assertEqual(models.endpoint({**p,'base_url':'https://example.com'},'responses'),'https://example.com/v1/responses')
        native=models.resolved(self.profile('anthropic',base_url='https://example.com/proxy'))
        self.assertEqual(models.endpoint(native,'messages'),'https://example.com/proxy/v1/messages')
        env={};models.apply_profile(env,{**native,'base_url':'https://example.com/proxy/v1'},self.app.state)
        self.assertEqual(env['ANTHROPIC_BASE_URL'],'https://example.com/proxy')
        inherited={};models.apply_profile(inherited,{'model':'original-model'},self.app.state)
        self.assertNotIn('CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST',inherited)
        with self.assertRaises(models.ModelError):models.list_models({**p,'base_url':self.upstream.url+'/redirect'})
        self.assertEqual(len(self.upstream.calls),1)
        self.upstream.fail=True
        with self.assertRaisesRegex(models.ModelError,'HTTP 401') as err:models.list_models(p)
        self.assertNotIn(p['api_key'],str(err.exception))

    def test_messages_tools_images_and_reasoning_keep_order(self):
        for protocol in ('responses','chat_completions'):
            p=models.resolved(self.profile(protocol,max_output_tokens=100))
            body={'model':'override-model','max_tokens':500,'system':[{'type':'text','text':'system instructions','cache_control':{'type':'ephemeral'}}],
                  'tools':[{'name':'check','input_schema':{'type':'object','properties':{'n':{'type':'integer'}}}}],
                  'tool_choice':{'type':'any','disable_parallel_tool_use':True},'messages':[
                    {'role':'user','content':'check this'},
                    {'role':'system','content':[{'type':'text','text':'mid-turn reminder'}]},
                    {'role':'assistant','content':[{'type':'tool_use','id':'c1','name':'check','input':{'n':42}}]},
                    {'role':'user','content':[{'type':'tool_result','tool_use_id':'c1','content':[{'type':'text','text':'result 42'},{'type':'image','source':{'type':'base64','media_type':'image/png','data':'AA=='}}]}]}]}
            result=models.to_openai(body,p,{'c1':[{'type':'reasoning','id':'r1','encrypted_content':'opaque'}]})
            self.assertEqual(result['model'],'override-model');self.assertEqual(result['max_output_tokens' if protocol=='responses' else 'max_completion_tokens'],100)
            self.assertEqual(result['tool_choice'],'required');self.assertFalse(result['parallel_tool_calls'])
            encoded=json.dumps(result);self.assertIn('data:image/png;base64,AA==',encoded);self.assertIn('c1',encoded)
            if protocol=='responses':self.assertIn('opaque',encoded);self.assertFalse(result['tools'][0]['strict'])
            else:self.assertEqual(next(m for m in result['messages'] if m['role']=='assistant')['tool_calls'][0]['function']['name'],'check')
            self.assertIn('mid-turn reminder',encoded)
        with self.assertRaises(models.ModelError):models.tool_block('c','check','{"broken"')
        with self.assertRaises(models.ModelError):models.media({'type':'unknown'})

    def test_both_protocols_stream_tool_round_trip_and_count_estimate(self):
        for protocol in ('responses','chat_completions'):
            server=self.bridge(protocol)
            tool={'name':'write_proof','input_schema':{'type':'object','properties':{'value':{'type':'integer'}}}}
            body={'model':'model-alpha','max_tokens':100,'stream':True,'tools':[tool],'messages':[{'role':'user','content':'use tool'}]}
            output=self.call(server,body)
            events=list(models.sse_events(output.encode().splitlines(keepends=True)))
            starts=[e for e in events if e['type']=='content_block_start']
            self.assertEqual(starts[0]['content_block']['type'],'tool_use')
            arg=next(e['delta']['partial_json'] for e in events if e['type']=='content_block_delta')
            self.assertEqual(json.loads(arg),{'value':42})
            self.assertEqual(next(e['delta']['stop_reason'] for e in events if e['type']=='message_delta'),'tool_use')
            body['messages'] += [{'role':'assistant','content':[{'type':'tool_use','id':'call-fixture','name':'write_proof','input':{'value':42}}]},{'role':'user','content':[{'type':'tool_result','tool_use_id':'call-fixture','content':'42'}]}]
            output=self.call(server,body)
            self.assertIn('Tool result received.',output);self.assertIn('message_stop',output)
            body['stream']=False
            self.assertEqual(json.loads(self.call(server,body))['stop_reason'],'end_turn')
            with self.assertRaises(urllib.error.HTTPError) as err:self.call(server,body,token='bad')
            self.assertEqual(err.exception.code,401)

    def test_truncated_stream_is_error_not_success_or_tool_execution(self):
        self.upstream.broken=True
        for protocol in ('responses','chat_completions'):
            server=self.bridge(protocol)
            body={'model':'model-alpha','max_tokens':100,'stream':True,'tools':[{'name':'write_proof','input_schema':{'type':'object'}}],'messages':[{'role':'user','content':'use tool'}]}
            output=self.call(server,body)
            self.assertIn('event: error',output)
            self.assertNotIn('event: message_stop',output)
            self.assertNotIn('"type": "tool_use"',output)

    def test_detached_bridge_reused_env_isolated_and_launch_secret_removed(self):
        p=self.profile();before=dict(os.environ)
        env={'ANTHROPIC_API_KEY':'old','CLAUDE_CODE_OAUTH_TOKEN':'old-oauth','CLAUDE_CODE_USE_BEDROCK':'1'}
        models.apply_profile(env,p,self.app.state)
        second={};models.apply_profile(second,p,self.app.state)
        self.assertEqual(env['ANTHROPIC_BASE_URL'],second['ANTHROPIC_BASE_URL'])
        self.assertNotIn('ANTHROPIC_API_KEY',env);self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN',env)
        self.assertEqual(env['ANTHROPIC_DEFAULT_HAIKU_MODEL'],'model-alpha');self.assertEqual(os.environ,before)
        self.assertEqual(env['CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST'],'1')
        self.assertFalse(list((self.app.state/'model-bridges').glob('*/launch.json')))
        self.assertEqual(self.app.redact(env['ANTHROPIC_AUTH_TOKEN']),'[已隐藏密钥]')
        req=urllib.request.Request(env['ANTHROPIC_BASE_URL']+'/v1/messages',data=json.dumps({'model':'model-alpha','max_tokens':20,'messages':[{'role':'user','content':'hi'}]}).encode(),headers={'Authorization':'Bearer '+env['ANTHROPIC_AUTH_TOKEN']})
        with urllib.request.urlopen(req) as r:self.assertEqual(json.load(r)['content'][0]['text'],'OK')
        bridges=models.bridge_operation(self.app.state)
        self.assertEqual(len(bridges),1)
        self.assertNotIn('token',bridges[0])
        self.assertTrue(models.bridge_operation(self.app.state,bridges[0]['id'])['ok'])
        self.assertEqual(models.bridge_operation(self.app.state),[])


if __name__=='__main__':unittest.main()
