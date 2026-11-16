
import json, threading, unittest, urllib.request, urllib.error
from tests.ui_action_fixture import server
check = unittest.TestCase()
instance = server(live=True)
threading.Thread(target=instance.serve_forever, daemon=True).start()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
base = f'http://127.0.0.1:{instance.server_port}'
def request(path, body=None, guard=True):
    headers = {'Content-Type': 'application/json'}
    if guard:
        headers['X-Arete'] = '1'
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
    try:
        response = opener.open(req, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.read().decode()
try:
    status, raw = request('/api/live')
    check.assertEqual((status, json.loads(raw)['live']['rows']['runs']), (200, []))
    check.assertEqual(request('/__ui_live/control', {'scenario': 'populated'}, guard=False)[0], 403)
    check.assertEqual(request('/__ui_live/control', {'scenario': 'populated', 'revision': 2, 'age_seconds': 61})[0], 200)
    runs = json.loads(request('/api/live')[1])['live']['rows']['runs']
    check.assertEqual(len(runs), 40)
    check.assertIn('step 2', runs[0]['step'])
    side = json.loads(request('/api/sidebar')[1])
    check.assertEqual((side['serve']['age_seconds'], side['needs_you']), (61, 2))
    request('/__ui_live/control', {'scenario': 'error', 'status': 503})
    check.assertEqual(request('/api/sidebar')[0], 503)
    request('/__ui_live/control', {'scenario': 'malformed'})
    status, raw = request('/api/live')
    check.assertEqual(status, 200)
    with check.assertRaises(ValueError):
        json.loads(raw)
    request('/__ui_live/control', {'scenario': 'delay', 'delay': 0.2})
    check.assertEqual(request('/api/sidebar')[0], 200)
    check.assertEqual(request('/__ui_live/control', {'scenario': 'nothing'})[0], 400)
    log = json.loads(request('/__ui_live/requests')[1])
    check.assertEqual([entry['path'] for entry in log[:2]], ['/api/live', '/api/live'])
    check.assertEqual(len(log), 6)
    check.assertTrue(all(isinstance(entry['at'], float) for entry in log))
finally:
    instance.shutdown()
    instance.server_close()
