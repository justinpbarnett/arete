
import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from arete import dashboard
from tests.ui_action_fixture import server

check = unittest.TestCase()
instance = server()
check.assertEqual(instance.server_address[0], '127.0.0.1')
thread = threading.Thread(target=instance.serve_forever, daemon=True)
thread.start()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
base = f'http://127.0.0.1:{instance.server_port}'
def request(path, body=None, guard=True, key='replayed-key'):
    headers = {'Content-Type':'application/json', 'Idempotency-Key':key}
    if guard:
        headers['X-Arete'] = '1'
    req = urllib.request.Request(base+path, data=None if body is None else json.dumps(body).encode(), headers=headers)
    try:
        response = opener.open(req, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read()
        return response.status, json.loads(raw) if response.headers.get_content_type() == 'application/json' else raw.decode()
try:
    check.assertEqual(request('/ui.html')[0],200)
    check.assertEqual(request('/')[0],200)
    check.assertEqual(request('/__ui_actions/control',{},guard=False)[0],403)
    check.assertEqual(request('/__ui_actions/items/TR-209/reject',{},guard=False)[0],403)
    with mock.patch.object(dashboard.loop_run, 'launch', return_value={'id':'heartbeat-test'}) as launch:
        status, body = request('/api/checks/run',{})
        check.assertEqual((status,body),(200,{'run':'heartbeat-test'}))
        launch.assert_called_once_with('heartbeat')
    current = {'id':'TR-209','version':8,'state':'<b>Changed elsewhere</b>'}
    conflict = {'error':'version_conflict','current':current}
    check.assertEqual(request('/__ui_actions/control',{'status':409,'body':conflict})[0],200)
    data = {'id':'TR-209','reason':'reason','base_version':0}
    check.assertEqual(request('/__ui_actions/items/TR-209/reject',data),(409,conflict))
    check.assertEqual(request('/__ui_actions/items/TR-209/reject',data)[0],200)
    check.assertEqual(request('/__ui_actions/items/TR-209/reject',data)[0],200)
    log = request('/__ui_actions/requests')[1]
    check.assertEqual(len(log),4)
    check.assertEqual(log[0]['response'],{'run':'heartbeat-test'})
    check.assertEqual(log[1]['status'],409)
    check.assertEqual(log[2]['idempotency_key'],log[3]['idempotency_key'])
    check.assertEqual(log[2]['serialized_body'],log[3]['serialized_body'])
    check.assertTrue(all(entry['guard']=='1' for entry in log))
    request('/__ui_actions/control',{'status':500,'body':{'error':'broken'}})
    check.assertEqual(request('/__ui_actions/items/TR-209/remove',data),(500,{'error':'broken'}))
    request('/__ui_actions/control',{'delay':0.01})
    check.assertEqual(request('/__ui_actions/items/TR-209/remove',data)[0],200)
    check.assertEqual(request('/api/usage',{'kind':'visit','tab':'0123456789abcdef','device':'desktop','view':'floor','via':'hash','ms':10})[0],200)
    check.assertEqual(request('/api/usage')[0],200)
finally:
    instance.shutdown()
    thread.join(timeout=5)
    instance.server_close()
