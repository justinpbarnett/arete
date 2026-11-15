
loop = make_loop(webhook=True)
state.migrate()
webhooks.secret("intake")
server = web()
body = b'{"title": "race"}'
codes = []
try:
    for n in range(50):
        gate, got = threading.Barrier(2), []
        headers = signed("intake", body, f"id-{n}")
        def send():
            gate.wait(30)
            got.append(post(server, "intake", body, headers)[0])
        threads = [threading.Thread(target=send) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        codes.append(sorted(got))
finally:
    server.shutdown()
    server.server_close()
finish({"codes": codes, "snap": snap()})
