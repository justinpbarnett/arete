import re
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, PropertyMock, patch

from arete import (
    code_review,
    config,
    factory,
    flows,
    gh,
    jira,
    loops,
    orchestration,
    pipeline,
    record,
    scratch,
    sources,
    spec_delivery,
    spec_retro,
    tracker,
    util,
    workspace,
)
from arete.store import asks, runs, specs
from arete.store import orchestration as ledger
from arete.verify import Verified
from tests.test_process_limits import PollFixture


class SpecDeliveryTests(PollFixture):
    head = "b" * 40
    tree = "c" * 40
    merged = "d" * 40
    criterion = "The checkout command greets the operator with Hello."
    decision = "Use Hello on the command line."
    question = "What message should the checkout command show to the operator?"

    def configure(self, kind):
        self.local.label(self.ticket, add=["closed"])
        self.kind = kind
        self.repo = replace(self.repo, tracker=kind, github="org/product", jira_project="APP",
                            parts=[config.Part("", "org/product")])
        self.cfg.repos = [self.repo]
        self.cfg.replay = config.ReplayConfig(all=True)
        self.cfg.jira = config.JiraConfig(project="APP", transitions={"pr": "Done"},
                                         landed={"main": "Preview", "release": "Done"})
        self.repo.layer_dir.mkdir()
        (self.repo.layer_dir / "config.toml").write_text('[factory]\npath = "classic"\n')
        self.patch(config, "load", lambda: self.cfg)
        self.patch(scratch, "process_ids", dict)
        self.patch(scratch, "service_labels", dict)
        self.patch(gh, "me", lambda: "publisher")
        self.patch(gh, "ensure_label", lambda *a, **kw: "")
        self.patch(pipeline.alerts, "ask_notice", Mock())
        self.patch(pipeline.webpush, "ask_opened", lambda *a: [])
        self.patch(pipeline, "workspace_note", lambda *a: "fixture checkout")
        self.started = []
        self.closed = []
        self.delivery_attempts = 0
        self.uncertain = False
        self.child_calls = self.patch(gh, "issue_create", Mock(side_effect=AssertionError("created a child")))
        self.children = {}
        self.patch(gh, "api_list", lambda api: self.children.get(api.split("/")[-2], []))
        self.spec = "975" if kind == "github" else "APP-975"
        self.parent = "974" if kind == "github" else "APP-974"
        self.standalone = "976" if kind == "github" else "APP-976"
        self.pr = "https://github.com/org/product/pull/1"
        self.rows = {}
        self.put(self.spec, ["spec", "agent"], "publisher")
        self.put(self.parent, ["spec"], "publisher")
        self.put(self.standalone, ["agent"], "org" if kind == "github" else "publisher")
        if kind == "github":
            self.patch(gh, "issue", self.github_issue)
            self.patch(gh, "issues", self.github_issues)
            self.patch(gh, "issue_labels", lambda repo, number, add, remove: self.labels(str(number), add, remove))
            self.patch(gh, "issue_comment", lambda repo, number, body, **kw: self.post(str(number), body, **kw))
            self.patch(gh, "issue_close", lambda repo, number: self.close(str(number)))
        else:
            self.client = Mock(spec=jira.Client)
            self.client.base = "https://jira.example.com"
            self.client.browse_url.side_effect = self.url
            self.client.comment_url.side_effect = lambda tid, cid: f"{self.url(tid)}?focusedCommentId={cid}"
            self.client.me.return_value = {"accountId": "publisher"}
            self.client.changelog.return_value = []
            self.client.issue.side_effect = self.jira_issue
            self.client.search.side_effect = self.jira_search
            self.client.comments.side_effect = lambda tid: self.rows[tid]["comments"]
            self.client.edit_labels.side_effect = self.labels
            self.client.comment.side_effect = self.post
            self.client.move.side_effect = self.move
            self.client.create_task.side_effect = AssertionError("created a child")
            self.enterContext(patch.object(tracker.JiraTracker, "client", new_callable=PropertyMock,
                                           return_value=self.client))

    def patch(self, module, name, value):
        self.enterContext(patch.object(module, name, value))
        return value

    def put(self, tid, labels, owner):
        if self.kind == "jira":
            labels = [*labels, "repo-product"]
        self.rows[tid] = {"labels": set(labels), "status": "Open", "owner": owner, "comments": []}

    def url(self, tid):
        return (gh.issue_url(self.repo.github, tid) if self.kind == "github"
                else f"https://jira.example.com/browse/{tid}")

    def github_issue(self, repo, number):
        tid = str(number)
        row = self.rows[tid]
        return {"number": number, "title": f"Ticket {tid}", "body": "Add an operator greeting.", "url": self.url(tid),
                "author": {"login": row["owner"]}, "state": "OPEN" if row["status"] != "Done" else "CLOSED",
                "labels": [{"name": label} for label in sorted(row["labels"])], "comments": row["comments"]}

    def github_issues(self, repo, label="", state="open", **kw):
        out = [self.github_issue(repo, int(tid)) for tid, row in self.rows.items()
               if (not label or label in row["labels"]) and (row["status"] != "Done") == (state == "open")]
        return out

    def jira_issue(self, tid, fields):
        row = self.rows[tid]
        return {"id": tid.split("-")[-1], "key": tid, "fields": {
            "summary": f"Ticket {tid}", "description": "Add an operator greeting.", "labels": sorted(row["labels"]),
            "status": {"name": row["status"], "statusCategory": {"key": "done" if row["status"] == "Done" else "new"}},
            "creator": {"accountId": row["owner"]}, "assignee": {"accountId": "publisher"}, "issuetype": {"subtask": False},
            "created": "2026-01-01T00:00:00Z", "statuscategorychangedate": "2026-11-25T00:00:00Z",
        }}

    def jira_search(self, jql, fields, *, limit, include=None):
        if jql.startswith("parent ="):
            return self.children.get(jql.split('"')[1], [])
        labels = set(re.findall(r'labels = "([^"]+)"', jql))
        done = "statusCategory = Done" in jql
        excluded = set(re.findall(r'"(agent-[^"]+)"', jql.split("labels not in ")[-1].split(")")[0])) \
            if "labels not in " in jql else set()
        out = [self.jira_issue(tid, fields) for tid, row in self.rows.items()
               if labels <= row["labels"] and not excluded & row["labels"] and (row["status"] == "Done") == done
               and ("creator = currentUser()" not in jql or row["owner"] == "publisher")]
        return [row for row in out if not include or include(row)][:limit]

    def labels(self, tid, add, remove):
        self.rows[tid]["labels"].difference_update(remove)
        self.rows[tid]["labels"].update(add)

    def comment(self, tid, body, *, author="publisher", created=None):
        comments = self.rows[tid]["comments"]
        cid = len(comments) + 1
        at = util.iso() if created is None else created
        url = self.url(tid) + (f"#issuecomment-{cid}" if self.kind == "github" else f"?focusedCommentId={cid}")
        comments.append({"body": body, "author": {"login": author, "accountId": author}, "id": cid,
                         "createdAt": at, "created": at, "url": url})
        return url

    def post(self, tid, body, *, return_url=False, single_attempt=False):
        url = self.comment(tid, body)
        if "## Delivery evidence" in body:
            self.delivery_attempts += 1
            self.assertTrue(single_attempt)
            if self.uncertain and self.delivery_attempts == 1:
                raise (gh.GhUnsure if self.kind == "github" else jira.JiraUnsure)("lost fixture acknowledgement")
        return url if return_url else None

    def move(self, tid, target, *, category=""):
        if target == "Done":
            if category == "open":
                return False
            self.close(tid)
        else:
            self.rows[tid]["status"] = target
        return True

    def close(self, tid):
        self.assertEqual(tid, self.spec)
        self.assertEqual(specs.find(self.repo.name, tid)["state"], "evidenced")
        self.assertTrue(any("## Delivery evidence" in c["body"] for c in self.rows[tid]["comments"]))
        self.closed.append(tid)
        self.rows[tid]["status"] = "Done"

    def claim(self):
        return specs.find(self.repo.name, self.spec)

    def poll_start(self, spec_driver=None):
        def spawn(repo, tid, *, spec=False):
            self.started.append((tid, spec))
            if spec:
                self.assertIsNotNone(spec_driver)
                command = pipeline.run_command(repo, tid, spec=True)
                self.assertIn("orchestrate.py", command[3])
                self.run_polled_ticket(repo, tid, Mock(side_effect=AssertionError("ordinary builder got the spec")),
                                       pipeline.Script("orchestrate", spec_driver))
            else:
                self.assertEqual(tid, self.standalone)
                self.assertEqual(pipeline.dispatch_path(self.repo, tid), "classic")

                def classic(cfg, run, slot):
                    self.assertFalse(run.spec_root)
                    run.enter("pr") if self.kind == "github" else run.enter("running")
                    return "pr", "ordinary standalone path", ""

                self.assertEqual(self.run_polled_ticket(repo, tid, classic), 0)
                if self.kind == "jira":
                    self.labels(tid, ["agent-pr"], ["agent-running"])
        self.assertEqual(self.poll_with(self.cfg, spawn), 0)

    def fixtures_for_controller(self):
        def prepare(flow, tree):
            run, ws = flow.ctx.run, flow.ctx.ws
            run.enter("running")
            (ws / ".git").mkdir(parents=True)
            checkout = workspace.Checkout("", self.repo.github, self.repo.path, ws, "main")
            return factory.Outcome("pass", data=flows.prepared_fields(
                run.tracker.get(run.tid), "spec-975", [checkout], config.load_layer(self.repo.layer_dir), [], "", None))

        def open_pr(flow, **kw):
            self.assertFalse(self.closed)
            job = flow.ctx
            self.assertNotIn("Closes", job.run.tracker.pr_link(job.ticket, whole=True))
            job.checkouts[0].lease = self.head
            return factory.Outcome("pass", data={"prs": {self.repo.github: self.pr}, "opened": [self.repo.github],
                "pushed": [], "changed": [self.repo.github], "draft_why": "", "report_body": "", "summary": "candidate",
                "committed_lines": 1})

        def publish(flow, merge):
            self.assertTrue(callable(merge))
            flow.ctx.run.enter("pr")
            return flows.end_as("pass", "pr", summary="candidate ready")

        self.enterContext(patch.dict(factory.STEPS, {"prepare": prepare, "pr": open_pr, "publish": publish}))
        self.patch(flows.Job, "reclaim", lambda *a, **kw: "")
        self.patch(workspace, "kept_work", lambda *a, **kw: "")
        self.patch(workspace, "kept_onto", lambda *a: {})
        self.patch(pipeline, "claim_workspace", Mock())
        self.patch(pipeline, "release_workspace", Mock())
        self.patch(factory, "fingerprint", lambda *a: self.tree)
        self.patch(code_review, "bases", lambda *a: {"": "main-base"})
        self.patch(orchestration.Controller, "validate_publication_diff", lambda *a: None)
        self.patch(orchestration.Controller, "publication_ci", lambda *a: None)
        self.patch(spec_delivery.util, "git", lambda argv, cwd, **kw: self.tree if argv[-1].endswith("^{tree}") else "")

    def controller(self, flow):
        controller = orchestration.Controller(flow)

        def choose(turn, request, retried):
            if not flow.continues:
                op, args = "owner.ask", {"request": {
                    "questions": [{"kind": "text", "header": "Message", "question": self.question}],
                    "why": "The spec does not say what to show.", "tried": "Read the spec and checkout context.",
                }}
            elif "definition" not in controller.data:
                self.assertIn(self.decision, controller.job.goal)
                op, args = "spec.define", {"criterion": self.criterion, "procedure": "Run the checkout command."}
            else:
                self.assertIn(self.criterion, controller.job.goal)
                self.assertEqual(controller.required_gates(), ("checks", "review", "verify"))
                job = controller.job
                review = {"method": code_review.METHOD, "verdict": "approve", "summary": "fixture review",
                          "trees": {"": self.tree}, "bases": {"": "main-base"}}
                job.review = pipeline.Review.stored(review)
                controller.executing = "fixture-gates"
                controller.stamp("checks", {"full": True})
                controller.stamp("review", review)
                job.verified = Verified("pass", checks=[{"what": "Unrelated behavior", "passed": True,
                                                         "how": "ran another command", "saw": "a result"}])
                with self.assertRaisesRegex(ValueError, "exact spec criterion"):
                    controller.publish()
                job.verified.checks = [{"what": self.criterion, "passed": True,
                                        "how": "ran the checkout command", "saw": "Hello"}]
                controller.stamp("verify", {"verdict": "pass"})
                op, args = "publication.request", {}
            return {"ok": True, "structured": {"version": controller.protocol, "decision_id": turn,
                    "state_version": ledger.snapshot(controller.lease)["version"], "rationale": "fixture decision",
                    "calls": [{"op": op, "args": args}]}}

        with patch.object(controller, "controller_turn", side_effect=choose):
            return controller.drive()

    def bootstrap(self, kind):
        self.configure(kind)
        self.fixtures_for_controller()
        self.uncertain = True
        self.labels(self.parent, ["agent"], [])
        self.children[self.parent] = [{"key": "APP-971"}] if kind == "jira" else [{"number": 971}]
        with patch.object(pipeline, "run_refusal", return_value=(True, "")):
            forbidden = Mock(side_effect=AssertionError("ordinary feature drive ran"))
            self.assertEqual(pipeline.run_issue(self.repo.name, self.spec, force=True,
                                               script=pipeline.Script("feature", forbidden)), 2)
            forbidden.assert_not_called()
        self.assertFalse(runs.ticket_runs(self.repo.name, self.spec))
        self.poll_start(lambda flow: ("interrupted", "interrupted before controller registration", ""))
        root = self.claim()["root"]
        self.assertFalse(ledger.find(root))
        self.assertIn((self.standalone, False), self.started)
        self.poll_start(self.controller)
        asking = self.claim()["run"]
        [ask] = asks.asks_of(asking, "open")
        self.assertEqual(ask["questions"][0]["question"], self.question)
        self.assertEqual(ledger.find(asking)["root"], root)
        self.labels(self.spec, [], ["spec"])
        with patch.object(pipeline, "run_refusal", return_value=(True, "")):
            self.assertEqual(pipeline.run_issue(self.repo.name, self.spec, force=True,
                                               script=pipeline.Script("feature", forbidden)), 2)
        forbidden.assert_not_called()
        self.labels(self.spec, ["spec"], [])
        later = util.iso(util.parse_iso(ask["asked"]) + timedelta(seconds=5))
        self.comment(self.spec, "Stale decision", created=ask["asked"])
        self.comment(self.spec, "Wrong publisher", author="outsider", created=later)
        self.poll_start()
        self.assertEqual(asks.ask_get(ask["id"])["status"], "open")
        self.assertEqual(self.claim()["run"], asking)
        self.comment(self.spec, self.decision, created=later)
        self.poll_start(self.controller)
        receipt = self.claim()
        self.assertEqual(receipt["state"], "published")
        self.assertEqual((ledger.find(root)["current_run"], ledger.find(root)["generation"]), (receipt["run"], 2))
        self.assertEqual(receipt["data"]["definition"]["criterion"], self.criterion)
        self.assertEqual(asks.ask_get(ask["id"])["answers"][0]["text"], self.decision)
        self.assertIsNone(spec_retro.delivery_root(self.repo, self.spec, receipt["identity"]))
        self.assertEqual(self.rows[self.spec]["status"], "Open")
        self.assertEqual(len([s for s in self.started if s[1]]), 3)
        self.child_calls.assert_not_called()
        if kind == "jira":
            self.client.create_task.assert_not_called()
            self.client.move.assert_any_call(self.spec, "Done", category="open")

        pr = {"state": "OPEN", "headRefOid": self.head, "baseRefName": "main"}
        branch_ref = ["e" * 40]
        self.patch(gh, "pr_view", lambda *a: pr)
        self.patch(spec_delivery, "revision", lambda *a: (branch_ref[0], self.tree))
        self.patch(gh, "branch_contains", lambda *a: False)
        self.poll_start()
        self.assertEqual(self.claim()["state"], "published")
        pr.update(state="MERGED", mergeCommit={"oid": self.merged}, mergedAt=util.iso())
        self.poll_start()
        self.assertEqual(self.rows[self.spec]["status"], "Open")
        branch_ref[0] = self.merged
        self.poll_start()
        self.assertEqual(self.claim()["state"], "published")
        self.assertEqual(self.rows[self.spec]["status"], "Open")
        self.poll_start()
        self.assertEqual(self.claim()["state"], "complete")
        self.assertEqual(self.closed, [self.spec])
        self.assertEqual(self.delivery_attempts, 1)
        self.assertEqual(self.rows[self.parent]["status"], "Open")
        self.assertFalse(specs.find(self.repo.name, self.parent))
        self.assertEqual(len(runs.ticket_runs(self.repo.name, self.spec)), 3)

        def closed_pages(*a, **kw):
            issue = self.github_issue(self.repo.github, int(self.spec))
            issue.update(closedAt=util.iso(), parent=None)
            return [{"data": {"repository": {"issues": {"nodes": [issue]}}}}]

        loop = loops.load(loops.BUNDLED_LOOPS_DIR / "mine", self.repo.name, "", self.cfg)
        index = 0 if kind == "github" else 1
        with patch.object(gh, "gh_json", side_effect=closed_pages):
            events, queued = sources.poll(loop, index, self.cfg)
            self.assertEqual([e["data"]["ref"] for e in events], [self.spec])
            self.assertEqual(len(queued), 1)
            self.assertEqual(sources.poll(loop, index, self.cfg), ([], []))

    def test_github_clarification_restart_release_and_retro_replay(self):
        self.bootstrap("github")

    def test_jira_clarification_restart_release_and_retro_replay(self):
        self.bootstrap("jira")

    def test_github_todo_requires_the_spec_publisher_and_existing_approval(self):
        self.configure("github")
        self.put("973", ["spec", "agent"], "org")
        tk = tracker.for_repo(self.cfg, self.repo)
        self.assertEqual({t.id for t in tk.todo()}, {self.spec, self.standalone})
        self.assertTrue(tk.get("973").blocker)
        self.assertIn("no `agent` label", tk.get(self.parent).blocker)

    def test_named_runtime_needs_authorized_delivery_point_evidence_even_on_matching_tree(self):
        self.configure("github")
        tk = tracker.for_repo(self.cfg, self.repo)
        spec = tk.get(self.spec)
        run = specs.bind(spec.spec_identity, self.repo.name, self.spec, "", lambda: record.start(
            "pipeline", repo=self.repo.name, issue=self.spec))
        record.settle(run, "pr", pr_url=self.pr)
        data = {"source": spec_delivery.source_version(spec), "landing": [], "targets": [],
                "definition": {"criterion": self.criterion, "procedure": "Run the checkout command.",
                               "environment": "Production"}, "behavior": {"how": "ran in the checkout", "saw": "Hello"}}
        self.assertTrue(specs.advance(spec.spec_identity, run, "building", "published", data))
        self.patch(spec_delivery, "reached", lambda _: [{"pr": self.pr, "repo": self.repo.github, "branch": "main",
                                                         "commit": self.merged, "exact_tree": True}])
        spec_delivery.sync(self.cfg)
        request = self.claim()["data"]["observation_request"]
        self.assertEqual(self.rows[self.spec]["status"], "Open")
        body = f"Spec evidence {request['token']}\nHow: Ran the command in Production.\nSaw: Hello."
        later = util.iso(util.parse_iso(request["since"]) + timedelta(seconds=5))
        self.comment(self.spec, body, created=request["since"])
        self.comment(self.spec, body, author="outsider", created=later)
        spec_delivery.sync(self.cfg)
        self.assertFalse(self.closed)
        good = self.comment(self.spec, body, created=later)
        spec_delivery.sync(self.cfg)
        self.assertEqual(self.closed, [self.spec])
        self.assertEqual(self.claim()["data"]["delivery"]["source"], good)

    def test_jira_refuses_ambiguous_route_and_rejects_closing_intermediate_transition(self):
        self.configure("jira")
        other = replace(self.repo, name="other", jira_label="repo-other")
        self.cfg.repos.append(other)
        self.rows[self.spec]["labels"].add("repo-other")
        tk = tracker.for_repo(self.cfg, self.repo)
        self.assertIn("exactly one", tracker.spec_blocker(self.cfg, tk, tk.get(self.spec)))
        self.put("APP-973", ["spec", "agent"], "outsider")
        self.client.changelog.return_value = [{"created": "2026-01-02T00:00:00Z", "author": {"accountId": "publisher"},
                                             "items": [{"field": "labels", "fromString": "spec repo-product",
                                                        "toString": "agent spec repo-product"}]}]
        self.assertIn("not created", tk.get("APP-973").blocker)
        client = jira.Client("https://jira.example.com", "fixture@example.com", "fixture-token")
        with (patch.object(client, "statuses", return_value={"Done"}),
              patch.object(client, "get", return_value={"transitions": [{"id": "1", "name": "Close",
                  "to": {"name": "Done", "statusCategory": {"key": "done"}}}]}),
              patch.object(client, "request") as send):
            self.assertFalse(client.move(self.spec, "Close", category="open"))
            send.assert_not_called()
            self.assertTrue(client.move(self.spec, "Close"))
            send.assert_called_once_with("POST", f"/rest/api/2/issue/{self.spec}/transitions", body={"transition": {"id": "1"}})
