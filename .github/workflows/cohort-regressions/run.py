"""Run trusted cases against a candidate. Run candidate code only on a disposable CI worker."""

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

POLICY = Path(__file__).resolve().parent
CASES = [
    (
        "repeated-runner-question",
        "975",
        "check-runner.py",
        "df764cd8f8b6993f5b4997d51e2ac05dc200e613",
        "54680cd5db326fdce98e5015b6d0040dc523c180",
    ),
    (
        "escaped-chat-draft",
        "978",
        "chat-draft.mjs",
        "54680cd5db326fdce98e5015b6d0040dc523c180",
        "1acbe542b2b6f1c54e8793326b53a94d279066cd",
    ),
]


@contextmanager
def historical(repo, sha):
    with tempfile.TemporaryDirectory(prefix="arete-case-") as temp:
        folder = Path(temp)
        archive = folder / "source.tar"
        subprocess.run(["git", "-C", repo, "archive", "-o", archive, sha, "arete"], check=True)
        with tarfile.open(archive) as tar:
            tar.extractall(folder, filter="data")
        yield folder


def replay(script, tree):
    program = [sys.executable, "-I", "-B"] if script.endswith(".py") else ["node"]
    with tempfile.TemporaryDirectory(prefix="arete-case-home-") as home:
        ran = subprocess.run(
            [*program, str(POLICY / script), str(tree.resolve())],
            cwd=home,
            timeout=30,
            capture_output=True,
            text=True,
            env={"HOME": home, "PATH": os.environ["PATH"]},
        )
        if ran.returncode:
            raise RuntimeError(f"{script} could not complete (exit {ran.returncode}): {ran.stderr[-400:]}")
    result = json.loads(ran.stdout)
    if type(result.get("pass")) is not bool:
        raise ValueError(f"{script} returned no observed verdict")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate", type=Path, required=True)
    ap.add_argument("--calibrate", action="store_true", help="also demonstrate the observed old failure and correction")
    args = ap.parse_args()
    results = []
    false_passes, repeated_work, regressions, errors = [], [], [], []
    for case, ticket, script, before, fixed in CASES:
        record = {"case": case, "source": f"https://github.com/justinpbarnett/arete/issues/{ticket}"}
        try:
            if args.calibrate:
                for name, sha, expect in (("before", before, False), ("corrected", fixed, True)):
                    with historical(args.candidate, sha) as tree:
                        measured = replay(script, tree)
                    record[name] = {"revision": sha, **measured}
                    if measured["pass"] != expect:
                        (regressions if expect else false_passes).append(case)
            record["candidate"] = replay(script, args.candidate)
            if not record["candidate"]["pass"]:
                regressions.append(case)
                if case == "repeated-runner-question":
                    repeated_work.append(case)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append({"case": case, "error": str(error)})
        results.append(record)
    revision = subprocess.check_output(["git", "-C", args.candidate, "rev-parse", "HEAD"], text=True).strip()
    print(
        json.dumps(
            dict(
                candidate=revision,
                calibrated=args.calibrate,
                cases=results,
                false_passes=false_passes,
                repeated_interventions=repeated_work,
                newly_broken=regressions,
                unavailable=errors,
            ),
            indent=2,
        )
    )
    return int(bool(false_passes or regressions or errors))


if __name__ == "__main__":
    sys.exit(main())
