"""Schema consistency, no provider calls."""
import json,pathlib

def test_success_codes_and_enums():
 d=json.loads(pathlib.Path(__file__).with_name('openapi.json').read_text())
 for path,methods in d['paths'].items():
  for method,op in methods.items():
   expected={'201'} if path=='/voice/session' else {'200','201'} if path=='/voice/internal/final' else {'200'}
   assert {x for x in op['responses'] if x.startswith('2')}==expected
 final=d['paths']['/voice/internal/final']['post']
 assert final['requestBody']['content']['application/json']['schema']['properties']['state']['enum']==['done','blocked']
 progress=d['paths']['/voice/internal/progress']['post']
 assert {'transcript_final','answer_final'}<=set(progress['requestBody']['content']['application/json']['schema']['properties']['type']['enum'])
 assert {'401','403','404'}<=set(d['paths']['/voice/result']['get']['responses'])
