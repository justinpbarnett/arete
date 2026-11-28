from __future__ import annotations

import json
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import nullcontext
from pathlib import Path

from . import (
    config,
    contract,
    events,
    factory_control,
    intake_rules,
    lines,
    principals,
    prompts,
    record,
    runner,
    steps,
    util,
)
from .store import blobs as blob_store
from .store import db as foundation
from .store import events as event_store
from .store import factory as factory_store
from .store import intake as intake_store
from .store import runs as run_store
from .store import tasks as task_store

_EXTRACT_POOL_LOCK = threading.Lock()
_EXTRACT_POOL: factory_control.WorkPool | None = None


def submit_extract(fn: Callable, *args, **kwargs) -> Future:
    global _EXTRACT_POOL
    with _EXTRACT_POOL_LOCK:
        if _EXTRACT_POOL is None:
            _EXTRACT_POOL = factory_control.WorkPool(4, thread_name_prefix="intake-extract")
        pool = _EXTRACT_POOL
    return pool.submit(fn, *args, **kwargs)


def list_items(
    *,
    source: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 100,
    connect=foundation.reader,
) -> list[dict]:
    rows = intake_store.list_items(source=source, status=status, kind=kind, limit=limit, connect=connect)
    return [intake_store.public_item(r) for r in rows]


def get_item(item_id: str, *, connect=foundation.reader) -> dict:
    row = intake_store.get_item(item_id, connect=connect)
    if row is None:
        raise contract.Problem("not_found", f"No intake item {item_id}")
    candidates = intake_store.candidates_for(row["num"], connect=connect)
    return {"item": intake_store.public_item(row), "candidates": candidates}


def history(item_id: str, *, connect=foundation.reader) -> list[dict]:
    row = intake_store.get_item(item_id, connect=connect)
    if row is None:
        raise contract.Problem("not_found", f"No intake item {item_id}")
    subject = f"intake:{row['id']}"
    with connect() as c:
        epoch = str(c.execute("SELECT value FROM schema_meta WHERE key = 'restore_epoch'").fetchone()[0])
        rows = c.execute(
            "SELECT * FROM events_log WHERE subject = ? AND class = 'durable' ORDER BY seq ASC",
            (subject,),
        ).fetchall()
        return [event_store.envelope(c, dict(r), epoch) for r in rows]


def add_item(values: dict, actor: str) -> dict:
    raw_text_in = values.get("text")
    blob_sha_in = values.get("blob")
    has_text = isinstance(raw_text_in, str) and len(raw_text_in) > 0
    has_blob = isinstance(blob_sha_in, str) and len(blob_sha_in.strip()) > 0
    if has_text == has_blob:
        raise contract.Problem("invalid_input", "Provide exactly one of text or blob")

    source = str(values.get("source") or "intake").strip() or "intake"
    kind = str(values.get("kind") or ("transcript" if has_blob else "paste")).strip() or "paste"
    author = str(values.get("author") or actor or "").strip()
    origin_url = str(values.get("origin_url") or "").strip()
    shape = str(values.get("shape") or ("blob" if has_blob else "paste")).strip()

    if has_blob:
        sha = blob_sha_in.strip()
        try:
            blob_store.validate_sha256(sha)
        except ValueError as err:
            raise contract.Problem("invalid_input", str(err)) from None
        found = blob_store.get(sha)
        if found is None:
            raise contract.Problem("not_found", f"No blob {sha}")
        _, blob_bytes = found
        raw_text_col = None
        raw_blob_col = sha
        raw_sha = sha
        raw_len = len(blob_bytes)
        default_title = util.first_line(blob_bytes.decode("utf-8", "replace"), 160) or f"Intake {kind}"
    else:
        raw_bytes = raw_text_in.encode("utf-8")
        raw_text_col, raw_blob_col, raw_sha = intake_store.prepare_raw(raw_bytes)
        raw_len = len(raw_bytes)
        default_title = util.first_line(raw_text_in, 160) or f"Intake {kind}"

    title = str(values.get("title") or "").strip() or default_title
    now = util.iso()

    with foundation.tx() as c:
        factory_store.admit(c)
        if raw_blob_col is not None and not has_blob:
            intake_store.record_raw_blob(c, raw_blob_col, raw_len)
        item_num, created = intake_store.insert_item(
            c,
            source=source,
            external_key=raw_sha,
            external_rev="",
            kind=kind,
            title=title,
            author=author,
            origin_url=origin_url,
            trust="untrusted",
            quarantined=1,
            raw=raw_text_col,
            raw_blob=raw_blob_col,
            raw_sha256=raw_sha,
            status="new",
            shape=shape,
            received=now,
        )
        if item_num is None:
            raise contract.Problem("internal_error", "Could not store intake item")
        row = intake_store.get_item_conn(c, item_num)
        assert row is not None
        if created:
            with principals.acting(actor):
                events.emit(
                    "intake.created",
                    f"intake:{row['id']}",
                    cls="durable",
                    ver=row["version"],
                    data={
                        "item": row["id"],
                        "source": row["source"],
                        "external_key": row["external_key"],
                        "kind": row["kind"],
                        "trust": row["trust"],
                        "status": row["status"],
                    },
                    connection=c,
                )
    return intake_store.public_item(row)


def _extract_candidates_from_result(res) -> list[dict]:
    structured = res.structured
    if structured is None and res.text:
        try:
            parsed = json.loads(res.text)
            if isinstance(parsed, dict):
                structured = parsed
        except ValueError:
            structured = None
    if not isinstance(structured, dict) or not isinstance(structured.get("candidates"), list):
        raise contract.Problem("upstream_failed", "intake_extract did not return structured candidates")
    return list(structured["candidates"])


def extract_item(item_id: str, actor: str, *, cfg: config.Config | None = None) -> dict:
    item = intake_store.get_item(item_id)
    if item is None:
        raise contract.Problem("not_found", f"No intake item {item_id}")
    if item["status"] in ("dismissed", "refused"):
        raise contract.Problem("invalid_state", f"Intake item {item['id']} is {item['status']}")

    # Never restate an item that has already been extracted or linked.
    existing_candidates = intake_store.candidates_for(item["num"])
    if item["status"] in ("extracted", "linked") and existing_candidates:
        step_id = next((c["extracted_by"] for c in existing_candidates if c.get("extracted_by") is not None), None)
        return {
            "item": intake_store.public_item(item),
            "candidates": existing_candidates,
            "step_id": step_id,
        }

    cfg = cfg or config.load_or_problem()[0]

    # Choose and pin rule/version BEFORE extraction; never silently substitute a newer live rule.
    pinned_rule: intake_rules.IntakeRule | None = None
    if item.get("rule") and item.get("rule_version"):
        try:
            pinned_rule = intake_rules.load_pinned_rule(str(item["rule"]), str(item["rule_version"]))
        except ValueError as err:
            raise contract.Problem("config_invalid", str(err)) from None
    else:
        matched = intake_rules.match_rule(item)
        if matched is not None:
            with foundation.tx() as c:
                cur_row = intake_store.get_item_conn(c, item["num"])
                if cur_row is None:
                    raise contract.Problem("not_found", f"No intake item {item_id}")
                if cur_row.get("rule") and cur_row.get("rule_version"):
                    pinned_rule = intake_rules.load_pinned_rule(
                        str(cur_row["rule"]), str(cur_row["rule_version"])
                    )
                else:
                    c.execute(
                        "UPDATE intake_items SET rule = ?, rule_version = ? WHERE id = ?",
                        (matched.name, matched.content_version, item["num"]),
                    )
                    pinned_rule = matched
                item = intake_store.get_item_conn(c, item["num"]) or item

    try:
        resolved_prompt, output_schema = intake_rules.resolve_prompt_and_schema(pinned_rule)
    except ValueError as err:
        raise contract.Problem("config_invalid", str(err)) from None

    raw_text = intake_store.raw_text(item)
    allowed_kinds = (
        list(pinned_rule.candidate_kinds)
        if pinned_rule and pinned_rule.candidate_kinds
        else sorted(intake_rules.CANDIDATE_KINDS)
    )
    allowed_actions = (
        list(pinned_rule.candidate_actions)
        if pinned_rule and pinned_rule.candidate_actions
        else sorted(intake_rules.CANDIDATE_ACTIONS)
    )
    rule_guidance = f"\nRule guidance ({pinned_rule.name}):\n{pinned_rule.guidance}\n" if pinned_rule and pinned_rule.guidance else ""
    rendered = prompts.render(
        resolved_prompt,
        source=item["source"],
        kind=item["kind"],
        title=item["title"],
        author=item["author"] or "unknown",
        origin_url=item["origin_url"] or "",
        raw=raw_text,
        rule_guidance=rule_guidance,
        candidate_kinds=", ".join(allowed_kinds),
        candidate_actions=", ".join(allowed_actions),
        defaults=json.dumps(pinned_rule.defaults if pinned_rule else {}, sort_keys=True),
    )

    step_cfg = steps.resolve(cfg, None, "intake_extract")
    model = step_cfg.model or cfg.model("intake_extract")
    run_with = step_cfg.runner
    run_id = record.new_id("intake")
    trace_name = f"intake-{item['num']}-extract"
    record.start("intake_extract", run_id=run_id, status="running", summary=f"Extract {item['id']}")
    step_id: int | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="arete-intake-extract-") as tmp_dir:
            res = runner.run(
                rendered,
                Path(tmp_dir),
                model=model,
                runner=run_with,
                trace=(run_id, trace_name),
                cfg=cfg,
                tools=[],
                allowed_tools=[],
                disallowed_tools=[],
                mcp_config="",
                instructions=[],
                json_schema=output_schema,
                ask_owner=False,
                persist=False,
                fallback=False,
                wait_out_limits=False,
            )
    except Exception as err:
        record.settle(run_id, "failed", summary=f"intake_extract failed: {err}")
        raise contract.Problem("upstream_failed", f"intake_extract failed: {err}") from None

    recorded_steps = run_store.steps(run_id)
    if recorded_steps:
        step_id = int(recorded_steps[-1]["id"])
    if not res.ok:
        record.settle(run_id, "failed", summary=res.error or "intake_extract failed")
        raise contract.Problem("upstream_failed", res.error or "intake_extract failed")

    raw_candidates = _extract_candidates_from_result(res)
    normalized = intake_rules.apply_rule_to_candidates(raw_candidates, raw_text, pinned_rule)
    record.settle(run_id, "done", summary=f"Extracted {len(normalized)} candidates for {item['id']}")

    with foundation.tx() as c:
        factory_store.admit(c)
        cur_row = intake_store.get_item_conn(c, item["num"])
        if cur_row is None:
            raise contract.Problem("not_found", f"No intake item {item_id}")
        cur_cands = intake_store.candidates_for_conn(c, item["num"])
        if cur_row["status"] in ("extracted", "linked") and cur_cands:
            return {
                "item": intake_store.public_item(cur_row),
                "candidates": cur_cands,
                "step_id": cur_cands[0]["extracted_by"],
            }
        for n, cand in enumerate(normalized, 1):
            c.execute(
                "INSERT INTO intake_candidates ("
                "item, n, kind, action, title, quote_start, quote_end, fields, "
                "match_task, match_confidence, state, task_id, extracted_by, version"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', NULL, ?, 1)",
                (
                    item["num"],
                    n,
                    cand["kind"],
                    cand["action"],
                    cand["title"],
                    cand["quote_start"],
                    cand["quote_end"],
                    json.dumps(cand["fields"], sort_keys=True, default=str),
                    cand["match_task"],
                    cand["match_confidence"],
                    step_id,
                ),
            )
        c.execute(
            "UPDATE intake_items SET status = 'extracted', version = version + 1 WHERE id = ?",
            (item["num"],),
        )
        updated_item = intake_store.get_item_conn(c, item["num"])
        assert updated_item is not None
        saved_candidates = intake_store.candidates_for_conn(c, item["num"])
        with principals.acting(actor):
            events.emit(
                "intake.extracted",
                f"intake:{updated_item['id']}",
                cls="durable",
                ver=updated_item["version"],
                data={
                    "item": updated_item["id"],
                    "rule": updated_item.get("rule"),
                    "rule_version": updated_item.get("rule_version"),
                    "step_id": step_id,
                    "count": len(saved_candidates),
                },
                connection=c,
            )
    return {
        "item": intake_store.public_item(updated_item),
        "candidates": saved_candidates,
        "step_id": step_id,
    }


def _ref_spec_for_item(item: dict) -> tuple[str, str, str, str]:
    """Return (system, kind, key, url) for an intake item."""
    kind = str(item.get("kind") or "")
    raw_text = intake_store.raw_text(item)
    payload: dict = {}
    if raw_text.lstrip().startswith("{"):
        try:
            parsed = json.loads(raw_text)
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            payload = {}

    if kind == "github.issue":
        key = str(payload.get("key") or "")
        if "#" in key and "/" in key:
            return "github", "issue", key, str(item.get("origin_url") or "")
    if kind == "jira.issue":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        jkey = str(data.get("key") or payload.get("key") or "").split("@", 1)[0]
        if jkey:
            return "jira", "ticket", jkey, str(item.get("origin_url") or "")
    if kind.startswith("github.") and isinstance(payload.get("repository"), dict):
        repo_full = str(payload["repository"].get("full_name") or "").strip()
        issue = payload.get("issue")
        pr = payload.get("pull_request")
        if repo_full and isinstance(issue, dict) and issue.get("number"):
            return "github", "issue", f"{repo_full}#{issue['number']}", str(issue.get("html_url") or item.get("origin_url") or "")
        if repo_full and isinstance(pr, dict) and pr.get("number"):
            return "github", "pr", f"{repo_full}#{pr['number']}", str(pr.get("html_url") or item.get("origin_url") or "")

    ref_kind = "transcript" if kind == "transcript" else "paste"
    return "intake", ref_kind, str(item["id"]), str(item.get("origin_url") or "")


def _extract_message_id(item: dict) -> str | None:
    raw_text = intake_store.raw_text(item)
    if raw_text.lstrip().startswith("{"):
        try:
            parsed = json.loads(raw_text)
        except ValueError:
            return None
        if isinstance(parsed, dict):
            for key in ("message_id", "ts", "delivery_id"):
                val = parsed.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            data = parsed.get("data")
            if isinstance(data, dict):
                for key in ("message_id", "ts"):
                    val = data.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
    return None


def _default_repo_for_create(cfg: config.Config, preferred: str | None) -> str:
    if preferred and preferred.strip():
        return preferred.strip()
    if len(cfg.repos) == 1:
        return cfg.repos[0].name
    return cfg.repos[0].name if cfg.repos else ""


def _check_rule_permissions(rule: intake_rules.IntakeRule | None, *, action: str, repo: str, project: str | None, line: str | None) -> None:
    if rule is None or not rule.task_permissions:
        return
    perms = rule.task_permissions
    if action == "create" and perms.get("allow_create") is False:
        raise contract.Problem("forbidden", f"Rule {rule.name!r} does not allow task creation")
    if action == "link" and perms.get("allow_link") is False:
        raise contract.Problem("forbidden", f"Rule {rule.name!r} does not allow task linking")
    if action == "create":
        if perms.get("repos") and repo and repo not in perms["repos"]:
            raise contract.Problem("forbidden", f"Rule {rule.name!r} does not permit repo {repo!r}")
        if perms.get("projects") and project and project not in perms["projects"]:
            raise contract.Problem("forbidden", f"Rule {rule.name!r} does not permit project {project!r}")
        if perms.get("lines") and line and line not in perms["lines"]:
            raise contract.Problem("forbidden", f"Rule {rule.name!r} does not permit line {line!r}")


def accept_item(item_id: str, values: dict, actor: str) -> dict:
    base_version = values.get("base_version")
    if type(base_version) is not int or base_version < 1:
        raise contract.Problem("invalid_input", "base_version must be an integer >= 1")
    decisions = values.get("candidates")
    if not isinstance(decisions, list) or not decisions:
        raise contract.Problem("invalid_input", "candidates must be a non-empty list")

    cfg = config.load_or_problem()[0]
    top_role = str(values.get("role") or "").strip() or None
    top_repo = str(values.get("repo") or "").strip() or None
    top_project = values.get("project")
    top_line = values.get("line")

    with foundation.tx() as c:
        factory_store.admit(c)
        item = intake_store.get_item_conn(c, item_id)
        if item is None:
            raise contract.Problem("not_found", f"No intake item {item_id}")
        if item["version"] != base_version:
            raise contract.Problem(
                "version_conflict",
                f"Intake item {item['id']} changed.",
                current=intake_store.public_item(item),
            )
        if item["status"] in ("dismissed", "refused"):
            raise contract.Problem("invalid_state", f"Intake item {item['id']} is {item['status']}")

        pinned_rule: intake_rules.IntakeRule | None = None
        if item.get("rule") and item.get("rule_version"):
            try:
                pinned_rule = intake_rules.load_pinned_rule(str(item["rule"]), str(item["rule_version"]))
            except ValueError:
                pinned_rule = None

        raw_text = intake_store.raw_text(item)
        message_id = _extract_message_id(item)
        ref_system, ref_kind, ref_key, ref_url = _ref_spec_for_item(item)
        ref_dict, _ = task_store.TaskStore.ensure_ref(
            c,
            ref_system,
            ref_key,
            kind=ref_kind,
            title=item["title"],
            url=ref_url,
            intake_item=item["id"],
            actor=actor,
        )

        all_cands = {cand["n"]: cand for cand in intake_store.candidates_for_conn(c, item["num"])}
        result_tasks: list[dict] = []
        result_links: list[dict] = []
        changed_any = False

        for entry in decisions:
            if not isinstance(entry, dict) or type(entry.get("n")) is not int:
                raise contract.Problem("invalid_input", "Each candidate decision requires integer n")
            n = int(entry["n"])
            cand = all_cands.get(n)
            if cand is None:
                raise contract.Problem("not_found", f"No candidate {n} on {item['id']}")

            # Repeat accept of an already-linked candidate returns the existing link without duplicating.
            if cand["state"] == "linked" and cand.get("task_id"):
                existing_task = task_store.task_get(cand["task_id"], connect=lambda: nullcontext(c))
                if existing_task is not None:
                    result_tasks.append(existing_task)
                    existing_num = existing_task.get("num")
                    if existing_num is not None:
                        link_row = c.execute(
                            "SELECT * FROM task_refs WHERE task_id = ? AND ref_id = ?",
                            (existing_num, ref_dict["num"]),
                        ).fetchone()
                        if link_row is not None:
                            result_links.append(task_store._row_to_task_ref(link_row, ref_dict))
                continue

            if cand["state"] == "dismissed":
                raise contract.Problem("invalid_state", f"Candidate {n} on {item['id']} is dismissed")

            action = str(entry.get("action") or cand["action"] or "").strip()
            if action not in ("create", "link"):
                raise contract.Problem("invalid_input", f"Candidate {n} action must be 'create' or 'link'")

            cand_fields = dict(cand.get("fields") or {})
            if isinstance(entry.get("fields"), dict):
                cand_fields.update(entry["fields"])

            role_override = str(entry.get("role") or "").strip() or top_role
            role = role_override or ("origin" if action == "create" else "related")
            if role not in task_store.TASK_REF_ROLES:
                raise contract.Problem("invalid_input", f"Unsupported role {role!r}")

            if action == "create":
                repo = _default_repo_for_create(
                    cfg,
                    str(entry.get("repo") or top_repo or cand_fields.get("repo") or ""),
                )
                project = entry.get("project") if "project" in entry else (top_project if top_project is not None else cand_fields.get("project"))
                line = entry.get("line") if "line" in entry else (top_line if top_line is not None else cand_fields.get("line"))
                _check_rule_permissions(pinned_rule, action="create", repo=repo, project=project, line=line)

                title = str(entry.get("title") or cand["title"] or "").strip()
                quote_snippet = ""
                if cand["quote_start"] is not None and cand["quote_end"] is not None:
                    quote_snippet = raw_text[cand["quote_start"]:cand["quote_end"]]
                body = str(entry.get("body") or cand_fields.get("body") or cand_fields.get("summary") or quote_snippet or title).strip()
                raw_kind = str(entry.get("kind") or cand["kind"] or "feat").strip()
                task_kind = raw_kind if raw_kind in config.KINDS else "feat"
                task_values: dict[str, object] = {
                    "repo": repo,
                    "title": title,
                    "body": body,
                    "kind": task_kind,
                    "state": "ready",
                }
                if project is not None:
                    task_values["project"] = project
                if line is not None:
                    task_values["line"] = line
                if cand_fields.get("priority") is not None:
                    task_values["priority"] = cand_fields["priority"]
                if cand_fields.get("owner") is not None:
                    task_values["owner"] = cand_fields["owner"]
                try:
                    task_row = task_store.TaskStore.create(c, task_values, actor)
                except lines.ValidationError as err:
                    raise contract.Problem("invalid_fields", str(err)) from None
                except ValueError as err:
                    raise contract.Problem("invalid_input", str(err)) from None
                target_task_id = task_row["id"]
            else:
                target_task_id = str(entry.get("task") or cand.get("match_task") or "").strip()
                if not target_task_id:
                    raise contract.Problem("invalid_input", f"Candidate {n} with action 'link' requires task or match_task")
                existing_task = task_store.task_get(target_task_id, connect=lambda: nullcontext(c))
                if existing_task is None:
                    raise contract.Problem("not_found", f"No task {target_task_id}")
                _check_rule_permissions(
                    pinned_rule,
                    action="link",
                    repo=str(existing_task.get("repo") or ""),
                    project=existing_task.get("project"),
                    line=existing_task.get("line"),
                )
                task_row = existing_task
                target_task_id = task_row["id"]

            anchor_entry: dict[str, object] = {
                "intake_item": item["id"],
                "n": n,
                "quote_start": cand["quote_start"],
                "quote_end": cand["quote_end"],
                "ts": item["received"],
            }
            if cand["quote_start"] is not None and cand["quote_end"] is not None:
                anchor_entry["quote"] = raw_text[cand["quote_start"]:cand["quote_end"]]
            if message_id:
                anchor_entry["message_id"] = message_id

            interpretation = str(entry.get("interpretation") or cand_fields.get("summary") or cand["title"] or "").strip()
            try:
                link_dict = task_store.TaskStore.link_ref(
                    c,
                    target_task_id,
                    ref_dict["num"],
                    {
                        "role": role,
                        "role_explicit": role_override is not None,
                        "interpretation": interpretation,
                        "claims": cand_fields,
                        "anchor": [anchor_entry],
                        "linked_by": actor,
                        "confidence": cand["match_confidence"],
                        "intake_item": item["id"],
                        "intake_n": n,
                    },
                    actor,
                    append_anchor=True,
                )
            except task_store.PrimaryRefConflict as err:
                raise contract.Problem("conflict", str(err)) from None
            except LookupError as err:
                raise contract.Problem("not_found", str(err)) from None
            except ValueError as err:
                raise contract.Problem("invalid_input", str(err)) from None

            c.execute(
                "UPDATE intake_candidates SET state = 'linked', task_id = ?, version = version + 1 "
                "WHERE item = ? AND n = ?",
                (target_task_id, item["num"], n),
            )
            changed_any = True
            refreshed_task = task_store.task_get(target_task_id, connect=lambda: nullcontext(c)) or task_row
            result_tasks.append(refreshed_task)
            result_links.append(link_dict)

        if changed_any or item["status"] != "linked":
            c.execute(
                "UPDATE intake_items SET status = 'linked', version = version + 1 WHERE id = ?",
                (item["num"],),
            )
            updated_item = intake_store.get_item_conn(c, item["num"])
            assert updated_item is not None
            with principals.acting(actor):
                events.emit(
                    "intake.changed",
                    f"intake:{updated_item['id']}",
                    cls="durable",
                    ver=updated_item["version"],
                    data={
                        "item": updated_item["id"],
                        "status": updated_item["status"],
                        "tasks": [t["id"] for t in result_tasks],
                    },
                    connection=c,
                )
        else:
            updated_item = item

        updated_candidates = intake_store.candidates_for_conn(c, item["num"])
        return {
            "item": intake_store.public_item(updated_item),
            "candidates": updated_candidates,
            "ref": {k: v for k, v in ref_dict.items() if k != "num"},
            "tasks": result_tasks,
            "links": result_links,
        }


def dismiss_item(item_id: str, values: dict, actor: str) -> dict:
    base_version = values.get("base_version")
    reason = str(values.get("reason") or "").strip()
    raw_cands = values.get("candidates")
    if raw_cands is not None and (not isinstance(raw_cands, list) or not all(type(x) is int for x in raw_cands)):
        raise contract.Problem("invalid_input", "candidates must be a list of candidate numbers")

    with foundation.tx() as c:
        factory_store.admit(c)
        item = intake_store.get_item_conn(c, item_id)
        if item is None:
            raise contract.Problem("not_found", f"No intake item {item_id}")
        if base_version is not None and item["version"] != base_version:
            raise contract.Problem(
                "version_conflict",
                f"Intake item {item['id']} changed.",
                current=intake_store.public_item(item),
            )

        existing_cands = {cand["n"]: cand for cand in intake_store.candidates_for_conn(c, item["num"])}
        dismissed_nums: list[int] = []
        if raw_cands:
            for n in raw_cands:
                if n not in existing_cands:
                    raise contract.Problem("not_found", f"No candidate {n} on {item['id']}")
            for n in raw_cands:
                c.execute(
                    "UPDATE intake_candidates SET state = 'dismissed', version = version + 1 "
                    "WHERE item = ? AND n = ? AND state != 'dismissed'",
                    (item["num"], n),
                )
                dismissed_nums.append(n)
            c.execute(
                "UPDATE intake_items SET version = version + 1 WHERE id = ?",
                (item["num"],),
            )
        else:
            c.execute(
                "UPDATE intake_candidates SET state = 'dismissed', version = version + 1 "
                "WHERE item = ? AND state != 'dismissed'",
                (item["num"],),
            )
            dismissed_nums = sorted(existing_cands)
            c.execute(
                "UPDATE intake_items SET status = 'dismissed', refuse_reason = COALESCE(NULLIF(?, ''), refuse_reason), "
                "version = version + 1 WHERE id = ?",
                (reason, item["num"]),
            )

        updated_item = intake_store.get_item_conn(c, item["num"])
        assert updated_item is not None
        updated_candidates = intake_store.candidates_for_conn(c, item["num"])
        with principals.acting(actor):
            events.emit(
                "intake.changed",
                f"intake:{updated_item['id']}",
                cls="durable",
                ver=updated_item["version"],
                data={
                    "item": updated_item["id"],
                    "status": updated_item["status"],
                    "dismissed": dismissed_nums,
                    "reason": reason,
                },
                connection=c,
            )
        return {
            "item": intake_store.public_item(updated_item),
            "candidates": updated_candidates,
        }
