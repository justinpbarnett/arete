from __future__ import annotations

import functools
import json
import sys

from .. import config, paths
from .common import Parser, print_lines

BENCHMARK_NEVER = "It never pulls weights, provisions a device or schedules load."


def refusing(caught):
    def decorate(handler):
        @functools.wraps(handler)
        def guarded(args) -> int:
            try:
                return handler(args)
            except caught() as e:
                print(e, file=sys.stderr)
                return 1

        return guarded

    return decorate


def experiment_errors() -> tuple[type[BaseException], ...]:
    import tomllib

    from ..lab import experiment

    return experiment.Refused, tomllib.TOMLDecodeError, OSError


def verifier_errors() -> tuple[type[BaseException], ...]:
    import tomllib

    from ..lab import verifier_suites

    return verifier_suites.Refused, tomllib.TOMLDecodeError, OSError, RuntimeError


def retrieval_errors() -> tuple[type[BaseException], ...]:
    import tomllib

    from ..lab import retrieval

    return retrieval.Refused, tomllib.TOMLDecodeError, OSError, RuntimeError, config.UnknownRepo


@refusing(experiment_errors)
def experiment_run(args) -> int:
    from urllib.parse import quote

    from .. import client
    from ..lab import experiment

    target = paths.expand(args.manifest)
    if args.dry_run:
        if target.is_file():
            return experiment.run(target, args.out, True)
        exp = experiment.load_from_config(experiment.experiment_slug(args.manifest))
        path = paths.expand(args.out) if args.out else experiment.default_path(exp.name)
        return experiment.dry_run(exp, path)
    if target.is_file():
        slug = experiment.commit_manifest(target)
    else:
        slug = experiment.experiment_slug(args.manifest)
    body: dict[str, object] = {"mode": "run"}
    if args.out:
        body["args"] = {"out": args.out}
    try:
        answer = client.call("POST", f"/api/v1/experiments/{quote(slug, safe='')}/run", body)
    except client.Down:
        if target.is_file():
            return experiment.run(target, args.out, False)
        exp = experiment.load_from_config(slug)
        path = paths.expand(args.out) if args.out else experiment.default_path(exp.name)
        return experiment.exclusive(exp.repo.name, lambda: experiment.drive(exp, path))
    client.emit({k: v for k, v in answer.items() if k != "seq"})
    return int(answer.get("code") or 0)


@refusing(experiment_errors)
def experiment_optimize(args) -> int:
    from urllib.parse import quote

    from .. import client
    from ..lab import experiment, optimize

    target = paths.expand(args.plan)
    if args.dry_run:
        if target.is_file():
            return optimize.run(target, args.out, True)
        search = optimize.load_from_config(experiment.experiment_slug(args.plan))
        folder = optimize.folder_of(search, args.out)
        optimize.check_digest(search, folder / "trail.jsonl", experiment.events(folder / "trail.jsonl"))
        return optimize.dry_run(search, folder)
    if target.is_file():
        slug = experiment.commit_manifest(target)
    else:
        slug = experiment.experiment_slug(args.plan)
    body: dict[str, object] = {"mode": "optimize"}
    if args.out:
        body["args"] = {"out": args.out}
    try:
        answer = client.call("POST", f"/api/v1/experiments/{quote(slug, safe='')}/run", body)
    except client.Down:
        if target.is_file():
            return optimize.run(target, args.out, False)
        search = optimize.load_from_config(slug)
        folder = optimize.folder_of(search, args.out)
        optimize.check_digest(search, folder / "trail.jsonl", experiment.events(folder / "trail.jsonl"))
        return experiment.exclusive(search.repo, lambda: optimize.climb(search, folder))
    client.emit({k: v for k, v in answer.items() if k != "seq"})
    return int(answer.get("code") or 0)


@refusing(experiment_errors)
def experiment_summary(args) -> int:
    from ..lab import experiment

    return experiment.show(paths.expand(args.file), args.json)


@refusing(experiment_errors)
def experiment_sample(args) -> int:
    from ..lab import optimize

    print_lines(optimize.sample(paths.expand(args.folder)))
    return 0


@refusing(experiment_errors)
def experiment_promote(args) -> int:
    from ..lab import optimize

    return optimize.promote(paths.expand(args.folder), args.apply, args.rollback)


@refusing(verifier_errors)
def verifiers_sample(args) -> int:
    from ..lab import verifier_suites

    print_lines(verifier_suites.sample(paths.expand(args.folder)))
    return 0


@refusing(verifier_errors)
def verifiers_run(args) -> int:
    from ..lab import verifier_suites

    return verifier_suites.run(paths.expand(args.suite), dry=args.dry_run)


@refusing(verifier_errors)
def verifiers_report(args) -> int:
    from ..lab import verifier_suites

    return verifier_suites.show(paths.expand(args.file), as_json=args.json)


@refusing(retrieval_errors)
def retrieval_sample(args) -> int:
    from ..lab import retrieval

    print_lines(retrieval.sample(paths.expand(args.folder)))
    return 0


@refusing(retrieval_errors)
def retrieval_run(args) -> int:
    from ..lab import retrieval

    return retrieval.run(args.repo, args.suite, split=args.split, dry_run=args.dry_run, as_json=args.json,
                         out=args.out)


def writers_errors(handler):
    @functools.wraps(handler)
    def guarded(args) -> int:
        from .. import util
        from ..lab import writers

        try:
            return handler(args)
        except (writers.Refused, ValueError, TypeError, OSError, config.UnknownRepo) as e:
            print(f"arete writers: {util.first_line(str(e), 400)}", file=sys.stderr)
            return 2

    return guarded


@writers_errors
def writers_sample(args) -> int:
    from pathlib import Path

    from ..lab import writers

    print_lines(writers.sample(Path(args.folder)))
    return 0


@writers_errors
def writers_run(args) -> int:
    from ..lab import writers

    return writers.run(args.repo, args.dry_run)


@writers_errors
def writers_export(args) -> int:
    from pathlib import Path

    from .. import util
    from ..lab import writers

    found = writers.export(args.run)
    if args.out:
        util.write_json(Path(args.out), found)
        print(json.dumps({"run": args.run, "out": args.out,
                          "texts": sum(len(c["texts"]) for c in found["cases"])}))
    else:
        print(json.dumps(found, indent=2))
    return 0


@writers_errors
def writers_rate(args) -> int:
    from ..lab import writers

    print(json.dumps(writers.rate(args.run, args.file, args.rater)))
    return 0


@writers_errors
def writers_report(args) -> int:
    from ..lab import writers

    found = writers.summary(args.run)
    print(json.dumps(found) if args.json else "\n".join(writers.render(found)))
    return 0


def implementation_manifest(path) -> bool:
    import tomllib

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return "candidates" not in data and ("arms" in data or "tasks" in data)


def cmd_implementations(args) -> int:
    import tomllib

    from ..lab import experiment, implbench, workers

    if args.action == "run" and args.json:
        print("usage: arete benchmark run MANIFEST [--dry-run] [--out FILE] | report FILE [--json] | "
              "sample FOLDER [--workers]", file=sys.stderr)
        return 2
    try:
        if args.action == "sample":
            print("\n".join((workers.sample if args.workers else implbench.sample)(paths.expand(args.folder))))
            return 0
        if args.action == "report":
            file = paths.expand(args.file)
            return (workers.show if workers.is_workers_results(file) else implbench.show)(file, args.json)
        manifest = paths.expand(args.manifest)
        return (workers.run if workers.is_workers_manifest(manifest) else implbench.run)(manifest, args.out or "", args.dry_run)
    except (experiment.Refused, tomllib.TOMLDecodeError, OSError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1


def cmd_benchmark(args) -> int:
    from pathlib import Path

    from .. import devices, util
    from ..lab import benchmark

    if args.action in ("report", "sample") or (args.action == "run" and implementation_manifest(
            paths.expand(args.manifest))):
        return cmd_implementations(args)
    if args.action == "fixture":
        if not 1 <= args.seconds <= 3600:
            print("arete benchmark: --seconds must be from 1 to 3600", file=sys.stderr)
            return 2
        try:
            return benchmark.serve_fixture(args.port, args.seconds)
        except OSError as e:
            print(f"arete benchmark: cannot serve the fixture on port {args.port}: {util.first_line(str(e), 200)}",
                  file=sys.stderr)
            return 1
    if args.dry_run:
        print("arete benchmark: --dry-run applies to a manifest of arms and tasks", file=sys.stderr)
        return 2
    try:
        found = benchmark.run(benchmark.read_manifest(paths.expand(args.manifest)))
        if args.out:
            util.write_json(Path(args.out), found)
    except (benchmark.Refused, ValueError, TypeError, OSError, devices.DeviceError) as e:
        print(f"arete benchmark: {util.first_line(str(e), 400)}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(found))
    else:
        print_lines(benchmark.render(found))
    return 0


def register(sub) -> None:
    p = sub.add_parser("experiment", help="run paired atom or fixed/single-worker/orchestrated workflow trials, summarize results, "
                                          "or search for a better setting and promote the winner")
    esub = p.add_subparsers(dest="action", required=True, parser_class=Parser)
    a = esub.add_parser("run", help="run a manifest of paired trials")
    a.add_argument("manifest", metavar="MANIFEST", help="the manifest")
    a.add_argument("--dry-run", action="store_true", help="validate and print the plan; writes and calls nothing")
    a.add_argument("--out", default="", metavar="FILE", help="the results file, by default one named for the manifest")
    a.set_defaults(fn=experiment_run)
    a = esub.add_parser("summary", help="summarize a results file")
    a.add_argument("file", metavar="FILE", help="the results file")
    a.add_argument("--json", action="store_true", help="print one JSON line")
    a.set_defaults(fn=experiment_summary, local_read=True)
    a = esub.add_parser("optimize", help="search for a better setting")
    a.add_argument("plan", metavar="PLAN", help="the plan")
    a.add_argument("--dry-run", action="store_true", help="validate and print the plan; writes and calls nothing")
    a.add_argument("--out", default="", metavar="FOLDER", help="the trail folder")
    a.set_defaults(fn=experiment_optimize)
    a = esub.add_parser("promote", help="write the winner's keys to config.toml, or undo that")
    a.add_argument("folder", metavar="FOLDER", help="the trail folder")
    mode = a.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write the winner's keys to config.toml")
    mode.add_argument("--rollback", action="store_true", help="undo the last apply")
    a.set_defaults(fn=experiment_promote)
    a = esub.add_parser("sample", help="write a replay fixture into a new folder")
    a.add_argument("folder", metavar="FOLDER", help="a new folder for the replay fixture")
    a.set_defaults(fn=experiment_sample)

    p = sub.add_parser("benchmark", help="measure already configured local model endpoints under 8K and 32K context "
                                         "and concurrency, or compare implementation routes on ordinary and "
                                         "difficult tasks; " + BENCHMARK_NEVER,
                       description="Benchmark already configured local model endpoints once, on request. "
                                   + BENCHMARK_NEVER + " `run` takes a manifest of candidates (endpoints) or of arms "
                                   "and tasks (implementation routes); `report` and `sample` belong to the latter.")
    bsub = p.add_subparsers(dest="action", required=True, parser_class=Parser)
    b = bsub.add_parser("run", help="run a manifest once: endpoint candidates print a comparable report; arms and "
                                    "tasks run the implementation benchmark")
    b.add_argument("manifest", metavar="MANIFEST", help="a TOML manifest of candidates, or of arms and tasks")
    b.add_argument("--json", action="store_true", help="endpoint manifests: print the report as one JSON line")
    b.add_argument("--out", metavar="FILE", help="endpoint manifests: also write the report JSON to FILE; "
                                                  "implementation manifests: the results file")
    b.add_argument("--dry-run", action="store_true",
                   help="implementation manifests: validate and print the plan; writes and calls nothing")
    b = bsub.add_parser("report", help="print the report of an implementation benchmark results file")
    b.add_argument("file", metavar="FILE", help="the results file")
    b.add_argument("--json", action="store_true", help="print one JSON line")
    b = bsub.add_parser("sample", help="write a replay fixture for the implementation benchmark into a new folder")
    b.add_argument("folder", metavar="FOLDER", help="a new, empty folder")
    b.add_argument("--workers", action="store_true", help="write the bounded-worker fixture instead")
    b = bsub.add_parser("fixture", help="serve built-in fixture endpoints on loopback for a bounded time")
    b.add_argument("--port", type=int, required=True)
    b.add_argument("--seconds", type=float, default=60, help="how long to serve, 1 to 3600 (default 60)")
    p.set_defaults(fn=cmd_benchmark, local_read=lambda args: args.action == "report")

    p = sub.add_parser("verifiers", help="compare visual verifiers against known passes and regressions: "
                                         "`sample FOLDER`, `run SUITE`, `report FILE`")
    vsub = p.add_subparsers(dest="action", required=True, parser_class=Parser)
    a = vsub.add_parser("sample", help="write a fixture into a new folder")
    a.add_argument("folder", metavar="FOLDER", help="a new folder")
    a.set_defaults(fn=verifiers_sample)
    a = vsub.add_parser("run", help="run a suite")
    a.add_argument("suite", metavar="SUITE", help="the suite file")
    a.add_argument("--dry-run", action="store_true", help="list arms, tasks and trials; no calls, no writes")
    a.set_defaults(fn=verifiers_run)
    a = vsub.add_parser("report", help="summarize a results file")
    a.add_argument("file", metavar="FILE", help="the results file")
    a.add_argument("--json", action="store_true", help="the summary as one JSON line")
    a.set_defaults(fn=verifiers_report, local_read=True)

    p = sub.add_parser("writers", help="compare owner-configured writer routes with blinded team ratings and "
                                       "factual checks; proposals only")
    wsub = p.add_subparsers(dest="action", required=True, parser_class=Parser)
    a = wsub.add_parser("sample", help="write a fixture into a new folder")
    a.add_argument("folder", metavar="FOLDER", help="a new folder")
    a.set_defaults(fn=writers_sample)
    a = wsub.add_parser("run", help="run the configured writer routes on a repo's cases")
    a.add_argument("repo", metavar="REPO", help="a repo name from the config")
    a.add_argument("--dry-run", action="store_true", help="list the candidates and cases without a call")
    a.set_defaults(fn=writers_run)
    a = wsub.add_parser("export", help="write a run's blinded rating view")
    a.add_argument("run", metavar="RUN", help="a run id")
    a.add_argument("--out", metavar="FILE", help="write the rating view to FILE")
    a.set_defaults(fn=writers_export)
    a = wsub.add_parser("rate", help="record a rater's ratings for a run")
    a.add_argument("run", metavar="RUN", help="a run id")
    a.add_argument("file", metavar="FILE", help="the ratings file")
    a.add_argument("--rater", default="", metavar="NAME", help="the rater, instead of the file's rater")
    a.set_defaults(fn=writers_rate)
    a = wsub.add_parser("report", help="report a run")
    a.add_argument("run", metavar="RUN", help="a run id")
    a.add_argument("--json", action="store_true", help="one JSON line")
    a.set_defaults(fn=writers_report, local_read=True)

    p = sub.add_parser("retrieval", help="offline experiment: compare exact, hybrid, embedding and rerank retrieval on "
                                         "a layer's labeled queries; `retrieval sample FOLDER` writes a fixture")
    rsub = p.add_subparsers(dest="action", required=True, parser_class=Parser)
    a = rsub.add_parser("sample", help="write a fixture into a new folder")
    a.add_argument("folder", metavar="FOLDER", help="a new folder")
    a.set_defaults(fn=retrieval_sample)
    a = rsub.add_parser("run", help="run a suite on a repo's layer")
    a.add_argument("repo", metavar="REPO", help="a repo name from the config")
    a.add_argument("suite", metavar="SUITE", help="the suite")
    a.add_argument("--split", default="", help="keep only the queries of this split (train, validation, holdout)")
    a.add_argument("--dry-run", action="store_true", help="check the suite and corpus, call no adapter or model")
    a.add_argument("--json", action="store_true", help="print the results as one JSON line")
    a.add_argument("--out", default="", help="also write the results JSON to this file")
    a.set_defaults(fn=retrieval_run)
