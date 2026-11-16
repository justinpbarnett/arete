
gate.set()
a = approval()
dashboard.POSTS_IN_FLIGHT.close()
refused = post(a)
during = {"status": status_of(a), "listed": a in [x["id"] for x in approvals_store.approvals()],
          "count": dashboard.POSTS_IN_FLIGHT.count}
foreign = post(a, host="foreign.invalid")
who[0] = "someone@else.test"
stranger = post(a)
who[0] = OWNER
unmarked = post(a, header=None)
lengths = {"missing": raw(a, ""), "invalid": raw(a, "Content-Length: abc\r\n")}
reading = page()
dashboard.POSTS_IN_FLIGHT.reopen()
again = post(a)
result(refused=refused, text=dashboard.RESTARTING, during=during, foreign=foreign["status"],
       stranger=stranger["status"], unmarked=unmarked["status"], lengths=lengths, reading=reading,
       again=again, status=status_of(a), count=dashboard.POSTS_IN_FLIGHT.count, kept=status_of(a))
