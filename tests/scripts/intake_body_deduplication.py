
loop = make_loop(webhook=True)
foundation.migrate()
a, b = b'{"title": "a"}', b'{"title": "b"}'
source = "intake#webhook"
def age(key):
    with foundation.db() as c:
        c.execute("UPDATE seen SET ts = ? WHERE source = ? AND key = ?",
                  (util.iso(util.now() - timedelta(minutes=11)), source, key))
server = web()
try:
    out = {}
    out["a"] = [post(server, "intake", a, token("intake")) for _ in range(2)]
    out["b"] = post(server, "intake", b, token("intake"))
    key = out["a"][0][1]["key"]
    age(key)
    out["a_aged"] = [post(server, "intake", a, token("intake")) for _ in range(2)]
    out["id"] = [post(server, "intake", a, token("intake", **{"X-Request-Id": "req-1"}))[0]]
    age("req-1")
    out["id"].append(post(server, "intake", a, token("intake", **{"X-Request-Id": "req-1"}))[0])
    out["bare"] = [post(server, "intake", a, token("intake", **{"X-Request-Id": "sha256:feed"}))[0]]
    age("sha256:feed")
    out["bare"].append(post(server, "intake", a, token("intake", **{"X-Request-Id": "sha256:feed"}))[0])
    out["expected"] = "sha256:" + hashlib.sha256(a).hexdigest()
    out["snap"] = snap()
finally:
    server.shutdown()
    server.server_close()
finish(out)
