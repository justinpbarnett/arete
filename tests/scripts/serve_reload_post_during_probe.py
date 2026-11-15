
n, started, execs = approval(), [], []
def probing(argv, **kw):
    if argv[-1] == serve.IMPORT_CHECK and not started:
        started.append(background(lambda: post(n)))
        wait_for(lambda: dashboard.POSTS_IN_FLIGHT.count == 1 and sending.is_set())
    return passing(argv, **kw)
def record(*a):
    execs.append({"count": dashboard.POSTS_IN_FLIGHT.count, "closed": dashboard.POSTS_IN_FLIGHT.closed,
                  "status": status_of(n)})
    raise OSError("fixture refuses to exec")
server, said = reload_server()
first = attempt(server, probing, record)
held = {"execs": len(execs), "said": [s for s in said if "new code waits to load" in s],
        "closed": dashboard.POSTS_IN_FLIGHT.closed, "status": status_of(n), "error": first}
gate.set()
answer = finish(started[0])
after = {"status": status_of(n), "count": dashboard.POSTS_IN_FLIGHT.count}
second = attempt(server, probing, record)
result(held=held, answer=answer, after=after, second=second, execs=execs,
       closed=dashboard.POSTS_IN_FLIGHT.closed, inheritable=os.get_inheritable(server.lock.fileno()))
