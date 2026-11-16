from __future__ import annotations

import dataclasses
import json
import sys
from collections import Counter

from .. import config, paths
from .common import Parser, print_lines, print_table, refused, steering


def hold_end(text: str) -> str:
    import re
    from datetime import UTC, timedelta

    from .. import util

    if found := re.fullmatch(r"(\d+(?:\.\d+)?)([mhd])", text.strip().lower()):
        minutes = float(found[1]) * {"m": 1, "h": 60, "d": 1440}[found[2]]
        return util.iso(util.now() + timedelta(minutes=minutes))
    end = util.parse_iso(text.strip())
    if end.tzinfo is None:
        end = end.astimezone()
    if end <= util.now():
        raise ValueError(f"{text} is already past")
    return util.iso(end.astimezone(UTC))


def cmd_hold(args) -> int:
    from datetime import timedelta

    from .. import accounts, claude, codex, environment, routing, state, util

    try:
        if not args.subscription:
            if args.until or args.release:
                raise ValueError("name the subscription: claude or codex")
            found = state.holds()
            for h in found:
                print(f"{h['scope']}\n  {routing.hold_words(h)}")
            if not found:
                print("no holds")
            return 0
        module, limit = {"claude": (claude, claude.EVERY_MODEL), "codex": (codex, codex.LIMIT_SCOPE)}[args.subscription]
        scope = accounts.scope(module.authentication(environment.passed(config.load())), limit)
        if not scope:
            raise ValueError(f"no {args.subscription} login found to hold")
        if args.release:
            if args.until:
                raise ValueError("--release takes no time")
            state.clear_holds([scope], util.iso(util.now() + timedelta(seconds=1)))
            print(f"released {args.subscription}; its routes are back in every chain")
            return 0
        if not args.until:
            raise ValueError("give a time like 6h, 90m, 2d or an ISO time, or --release")
        until = hold_end(args.until)
        state.hold(scope, until, f"held with arete hold {args.subscription}")
        print(f"held {args.subscription} until {routing.local_time(until)}; its steps take the next route in their tier")
        return 0
    except (ValueError, OSError) as e:
        return refused(e)


def cmd_costs(args) -> int:
    from pathlib import Path

    from .. import control, record, state, util

    correct = getattr(args, "correct", None)
    backfill = getattr(args, "backfill", False)
    why, mode, before = (getattr(args, k, None) for k in ("why", "auth_mode", "before"))
    try:
        if (correct or backfill) and (args.by or args.json or args.days is not None):
            raise ValueError("cost writes cannot be combined with report flags")
        if (why is not None or mode is not None) and not correct:
            raise ValueError("--why and --auth-mode require --correct")
        if before is not None and not backfill:
            raise ValueError("--before requires --backfill")
        if correct and backfill:
            raise ValueError("choose --correct or --backfill")
        if correct:
            if not why or not why.strip():
                raise ValueError("--correct requires --why")
            record.correct(correct[0], float(correct[1]), why, mode or "subscription")
            print(f"corrected {correct[0]} by ${float(correct[1]):g}")
            return 0
        if backfill:
            values = json.loads(Path(before).read_text()) if before else None
            if before and not isinstance(values, dict):
                raise ValueError("before must map run IDs to finite dollar amounts")
            count = state.backfill_legacy_cost(values)
            print(f"backfilled {count} steps")
            return 0
    except (ValueError, OSError) as e:
        return refused(e)

    if args.by or args.json:
        return print_step_costs(args.by or ["step"], args.days, args.json)
    try:
        c = control.costs(args.days)
    except control.Refused as e:
        return refused(e)
    print(f"{util.paid_note(c['total_usd']).capitalize()} in the last {c['days']:g} days; subscription calls count nothing")
    for group in ("model", "runner", "loop"):
        print(f"by {group}:")
        for row in c[f"by_{group}"][:12]:
            steps = "step " if row["steps"] == 1 else "steps"
            print(f"  {row['name']:<36} ${row['cost_usd']:>8.2f}  {row['steps']:>5} {steps}  "
                  f"{row['tokens_in']:>10,} in  {row['tokens_out']:>9,} out")
    print("by route:")
    for row in c["by_route"][:12]:
        spend = (f"subscription reported ${row['subscription_usd']:.2f}" if row["billing"] == "subscription"
                 else f"{row['billing']} ${row['cost_usd']:.2f}" if row["billing"] in ("local", "replay")
                 else f"paid ${row['paid_usd']:.2f}")
        print(f"  {row['name']:<52} {spend:<32} {row['calls']:>5} calls  {row['tokens_in']:>10,} in  "
              f"{row['cache_read']:>10,} cache rd  {row['cache_write']:>9,} cache wr  {row['tokens_out']:>9,} out")
    return 0


def cmd_ab(args) -> int:
    from .. import control

    try:
        report = control.ab_report(args.name or "", args.days)
    except (control.Refused, ValueError, TypeError) as e:
        return refused(e)
    if args.json:
        for test in report:
            print(json.dumps(test))
        return 0
    if not report:
        days = control.AB_DAYS if args.days is None else args.days
        print(f"No ticket ran {'the A/B ' + args.name if args.name else 'an A/B'} in the last {days:g} days.")
        return 0

    def money(value: float | None) -> str:
        return "-" if value is None else f"${value:.2f}"

    for test in report:
        print(f"A/B {test['ab']} on step {test['step']}, last {test['days']:g} days")
        rows = [("side", "tickets", "merged", "no PR", "repairs", "review rounds", "api $/merged PR",
                 "subscription $/merged PR")]
        for side, s in test["sides"].items():
            rows.append((side, str(s["tickets"]), str(s["merged"]), str(s["no_pr"]), str(s["repairs"]),
                         str(s["review_rounds"]), money(s["paid_per_merged"]), money(s["subscription_per_merged"])))
        print_table(rows, 200)
        lo, hi = test["interval"]
        print(f"merge rate arm - baseline: {test['merge_rate_difference'] * 100:+.1f} points "
              f"(95% Wilson interval {lo * 100:+.1f} to {hi * 100:+.1f})"
              + ("; too few tickets (under 20 a side)" if test["too_few"] else ""))
    return 0


def print_step_costs(groupings: list[str], days: float | None, as_json: bool) -> int:
    from .. import control

    try:
        found = [(by, control.step_costs(by, days)) for by in groupings]
    except control.Refused as e:
        return refused(e)
    c = found[-1][1]
    waits = {"wait_minutes": c["wait_minutes"], "days": c["days"], "since": c["since"], "paid_usd": c["paid_usd"],
             "subscription_usd": c["subscription_usd"]}
    if as_json:
        print_lines([*(json.dumps(g) for _, each in found for g in each["groups"]), json.dumps(waits)])
        return 0
    for by, each in found:
        print(f"Every step by {by} in the last {each['days']:g} days. Cost is what each call reported; "
              "subscription calls count here too. Paid totals exclude them. `in` counts the input no cache served.")
        routed = by == "route"
        rows = [(by, *(("billing",) if routed else ()), "runs", "calls", *(("paid", "subscription") if routed else ("cost",)),
                 "in", "cache rd", "cache wr", "out", "min", "api min")]
        rows += [(g["name"], *((g["billing"],) if routed else ()), str(g["runs"]), str(g["calls"]),
                  *((f"${g['paid_usd']:.2f}", f"${g['subscription_usd']:.2f}") if routed else (f"${g['cost_usd']:.2f}",)),
                  f"{g['tokens_in'] - g['cache_read'] - g['cache_write']:,}", f"{g['cache_read']:,}",
                  f"{g['cache_write']:,}", f"{g['tokens_out']:,}", f"{g['minutes']:.1f}", f"{g['api_minutes']:.1f}")
                 for g in each["groups"]]
        print_table(rows, 200)
        if routed:
            local = sum(g["calls"] for g in each["groups"] if g["billing"] == "local")
            print(f"paid ${each['paid_usd']:.2f}  subscription reported ${each['subscription_usd']:.2f}  "
                  f"local calls {local}")
    print("waits (minutes): " + "  ".join(f"{cause} {m:.1f}" for cause, m in c["wait_minutes"].items()))
    return 0


def cmd_steps(args) -> int:
    from .. import research_audit, research_search, routing, steps

    where = ""
    try:
        cfg = config.load()
        repo = cfg.repo(args.repo) if args.repo else None
        layer_dir = repo.layer_dir if repo else None
        where = f"{paths.short_path(layer_dir / 'config.toml')}: " if layer_dir else ""
        layer = config.load_layer(layer_dir) if layer_dir else None
        steps.check(cfg, layer)
        sheet = [steps.resolve(cfg, layer, role, args.kind) for role in config.STEP_ROLES]
    except (ValueError, TypeError) as e:
        print(f"{where}{e}", file=sys.stderr)
        return 1
    found = None if cfg.replay.all else routing.Discovery(cfg, repo, layer=layer)
    for s in sheet:
        notes = list(s.notes)
        if s.step in steps.SEARCHES and (why := research_search.unavailable(s)):
            notes.append(why)
        if s.step in steps.FETCHES and (why := research_audit.fetch_note(s)):
            notes.append(why)
        if s.runner == "pi" and not cfg.replay.all:
            selected = routing.pi_route(s.model, cfg, repo)
            if selected.login_refused:
                notes.append(f"pi {selected.provider} login refused: {selected.login_refused}")
            if selected.error:
                notes.append(selected.error)
            elif selected.provider == "anthropic" and selected.key_available:
                notes.append("pi anthropic: paid api_key route (Anthropic Console key)")
        remaining = routing.loop_budget_left(cfg) if s.step in ("loop", "classify") else None
        routes = routing.described(found.routes(s, remaining)) if found else []
        selection = (routing.described_selection(routing.select(found, s, remaining), s, 0)
                     if found and s.requires.declared else None)
        print(json.dumps({**dataclasses.asdict(s), "notes": notes, "routes": routes, "selection": selection,
                          "ab": steps.ab_view(cfg, layer, s.step, args.kind),
                          "payment_modes": list(config.allowed_payment_modes(cfg, layer))}))
    return 0


def prices_compared(file: str | None, importing: bool) -> int:
    from .. import prices, util

    try:
        old = prices.load()
        if file is not None:
            new = prices.read_file(file)
        elif old is None:
            print("no price snapshot imported", file=sys.stderr)
            return 1
        else:
            new = old
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    found = prices.diff(old if file is not None else None, new, util.now())
    print_lines([*(json.dumps(f, ensure_ascii=False) for f in found),
                 json.dumps({"source": new["source"], "observed": new["observed"], "version": new["version"],
                             "routes": len(new["routes"]), "against": old["version"] if file is not None and old else None,
                             "findings": dict(Counter(f["finding"] for f in found)), "imported": importing})])
    if importing:
        prices.store(new)
    return 0


def prices_diff(args) -> int:
    return prices_compared(args.file, False)


def prices_import(args) -> int:
    return prices_compared(args.file, True)


def prices_recompute(args) -> int:
    from .. import prices

    try:
        print_lines([json.dumps(prices.recompute(args.run_id, args.step), ensure_ascii=False)])
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def register(sub) -> None:
    p = steering(sub.add_parser("costs", help="what you paid for model calls, by model, runner and loop; with --by, "
                                              "every step by step, task kind, model or route, and the wait minutes by cause"))
    p.add_argument("--days", type=float, default=None)
    p.add_argument("--by", nargs="+", choices=("step", "kind", "model", "route"),
                   help="one grouping or several, in order; route is runner/provider/model/billing")
    p.add_argument("--json", action="store_true", help="one JSON line per group, then one with the wait minutes")
    writes = p.add_mutually_exclusive_group()
    writes.add_argument("--correct", nargs=2, metavar=("RUN", "USD"), help="record a signed repricing correction")
    writes.add_argument("--backfill", action="store_true", help="fill legacy run cost differences into steps")
    p.add_argument("--why", help="reason for --correct")
    p.add_argument("--auth-mode", choices=("subscription", "api_key", "local"), help="correction billing (default subscription)")
    p.add_argument("--before", metavar="FILE", help="JSON run-ID to pre-edit dollar amount map for --backfill")
    p.set_defaults(fn=cmd_costs)

    p = steering(sub.add_parser("hold", help="move steps off a subscription before it hits its limit, or bring it back; "
                                             "with no arguments, list the holds in force"))
    p.add_argument("subscription", nargs="?", choices=("claude", "codex"))
    p.add_argument("until", nargs="?", help="how long: 90m, 6h, 2d, or an ISO time")
    p.add_argument("--release", action="store_true", help="clear the hold now, including one a real limit set")
    p.set_defaults(fn=cmd_hold)

    p = steering(sub.add_parser("ab", help="compare the arm and baseline of each per-step A/B route on live tickets: "
                                           "merged PRs, repairs, review rounds and cost per merged PR"))
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--days", type=float, default=None)
    p.add_argument("--json", action="store_true", help="one JSON line per A/B")
    p.set_defaults(fn=cmd_ab)

    p = steering(sub.add_parser("steps", help="the model, runner, effort, timeout, tries, tools, mcp servers, "
                                              "instructions and session each step resolves to, one JSON line per step"))
    p.add_argument("--repo", default="", help="read this repo's layer too")
    p.add_argument("--kind", default="", choices=config.KINDS, help="resolve for this kind of ticket")
    p.set_defaults(fn=cmd_steps)

    p = sub.add_parser("prices", help="import, diff or recompute from an owner-supplied price and lifecycle "
                                      "snapshot")
    psub = p.add_subparsers(dest="action", required=True, parser_class=Parser)
    a = psub.add_parser("import", help="store an owner-supplied snapshot after diffing it against the stored one")
    a.add_argument("file", metavar="FILE", help="the snapshot file")
    a.set_defaults(fn=prices_import)
    a = psub.add_parser("diff", help="diff a snapshot file, or the stored snapshot, against the stored one")
    a.add_argument("file", metavar="FILE", nargs="?", default=None, help="the snapshot file")
    a.set_defaults(fn=prices_diff)
    a = psub.add_parser("recompute", help="reprice one step of a run from the stored snapshot")
    a.add_argument("run_id", metavar="RUN_ID")
    a.add_argument("step", metavar="STEP")
    a.set_defaults(fn=prices_recompute)
