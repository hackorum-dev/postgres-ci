#!/usr/bin/env python3
"""Build result.json for a patch CI run.

Takes the build/test outcome (already decided by the workflow - ok, failed,
or timed out, each with a wall-clock time), the check-world log to mine for
which tests failed, and ccache's own -s output, and writes one JSON file
tying it together. Parsing the log is the only non-trivial part here: a
check-world run mixes two different test runners in one log.

  - pg_regress (core suite, isolation, each contrib module) prints one line
    per test, in one of three formats depending on the era. PG <= 15 run
    serially: "test NAME ... ok". PG <= 15 inside a parallel group, which
    is most of the core suite: the same thing indented five spaces, with no
    "test" prefix. PG >= 16: TAP, "ok 1  - NAME  353 ms" or
    "not ok 3  + NAME  41 ms". All three are matched. A header line
    "# +++ regress check in DIR +++" names the suite directory, but only
    from PG 15 on - older majors print no such marker, so their tests all
    end up under one flat prefix. Every test line, plus every TAP file
    seen, is also recorded as an executed test.
  - prove-driven TAP suites print progress per .pl file, then on failure a
    "Test Summary Report" block with one line per failing file:
    "FILE.pl (Wstat: N Tests: N Failed: N)". If the run gets killed by the
    outer timeout before prove reaches that summary, the only trace left is
    raw "not ok" lines inside the partial output - handled as a fallback.

check-world runs with -Otarget, which buffers each make target's output as
one contiguous block, so a single left-to-right scan tracking "current
suite directory" and "current TAP file" never gets confused by unrelated
targets interleaving mid-block.
"""
import argparse
import json
import re
import sys

MAX_FAILED = 200
MAX_EXECUTED = 2000
MAX_NAME = 200
VALID_STATUSES = {
    "success", "build_failed", "build_timeout",
    "tests_failed", "tests_timeout", "infra_error",
}

SUITE_HEADER_RE = re.compile(
    r"#\s*\+\+\+ (?:regress|isolation|tap) check in (\S+) \+\+\+"
)
# three pg_regress eras, all still in use across our era images. "failed
# (ignored)" is a third status pre-16 (an --ignore listed test whose output
# differed): it ran, and it does not fail the suite. PG 16 dropped --ignore
# along with the old line format.
REGRESS_SERIAL_RE = re.compile(
    r"^test\s+(\S+)\s+\.\.\.\s+(ok|FAILED|failed \(ignored\))"
)
REGRESS_PARALLEL_RE = re.compile(
    r"^ {5}(\S+)\s+\.\.\.\s+(ok|FAILED|failed \(ignored\))"
)
REGRESS_TAP_RE = re.compile(r"^(not )?ok\s+\d+\s+[-+]\s+(\S+)\s+\d+ ms\s*$")
TAP_FILE_RE = re.compile(r"\bt/(\S+?)\.pl\b")
PROVE_SUMMARY_RE = re.compile(
    r"^(\S+)\.pl\s+\(Wstat:\s*(\d+)(?:\s*\(exited\s+\d+\))?\s+Tests:\s*\d+\s+Failed:\s*(\d+)\)"
)
NOT_OK_RE = re.compile(r"^(?:#\s*)?not ok\b")


def _add(items, seen, name, cap):
    name = name[:MAX_NAME]
    if name in seen:
        return
    seen.add(name)
    if len(items) < cap:
        items.append(name)


def _prefix(suite_dir, default):
    return suite_dir.rsplit("/", 1)[-1] if suite_dir else default


def parse_check_world(text):
    """Return (executed, failed) test identifier lists, each capped and deduped."""
    failed = []
    seen = set()
    executed = []
    executed_seen = set()

    suite_dir = None
    tap_file = None
    for raw in text.splitlines():
        line = raw.strip()

        m = SUITE_HEADER_RE.search(line)
        if m:
            suite_dir = m.group(1)
            tap_file = None
            continue

        # PG <= 15. The five-space indent is the only thing marking a test
        # inside a parallel group, so that one has to see the raw line.
        m = REGRESS_SERIAL_RE.match(line) or REGRESS_PARALLEL_RE.match(raw)
        if m:
            name = f"{_prefix(suite_dir, 'regress')}/{m.group(1)}"
            _add(executed, executed_seen, name, MAX_EXECUTED)
            if m.group(2) == "FAILED":
                _add(failed, seen, name, MAX_FAILED)
            continue

        # PG >= 16, where pg_regress emits TAP ('-' serial, '+' parallel).
        # Has to come before the bare "not ok" fallback below, which would
        # otherwise charge these to whatever TAP file was last seen. The
        # trailing runtime is what tells these apart from prove's own
        # progress lines and from a plain "not ok 3 - description" subtest,
        # so keep it anchored.
        m = REGRESS_TAP_RE.match(line)
        if m:
            name = f"{_prefix(suite_dir, 'regress')}/{m.group(2)}"
            _add(executed, executed_seen, name, MAX_EXECUTED)
            if m.group(1):
                _add(failed, seen, name, MAX_FAILED)
            continue

        m = PROVE_SUMMARY_RE.match(line)
        if m:
            prefix = _prefix(suite_dir, "tap")
            stem = m.group(1)
            if stem.startswith("t/"):
                stem = stem[2:]
            _add(executed, executed_seen, f"{prefix}/{stem}", MAX_EXECUTED)
            # a nonzero Wstat means the file bailed out or crashed before
            # prove could count subtests - "Failed: 0" in that case does not
            # mean nothing went wrong, it means nothing got the chance to
            wstat_nonzero = m.group(2) != "0"
            if wstat_nonzero or int(m.group(3)) > 0:
                _add(failed, seen, f"{prefix}/{stem}", MAX_FAILED)
            continue

        # relies on the build running make with -s: an unquiet make echoes
        # the prove recipe, glob-expanded to every t/*.pl in the suite, and
        # each of those names would land here as an executed test.
        m = TAP_FILE_RE.search(line)
        if m:
            tap_file = m.group(1)
            prefix = _prefix(suite_dir, "tap")
            _add(executed, executed_seen, f"{prefix}/{tap_file}", MAX_EXECUTED)

        if NOT_OK_RE.match(line) and tap_file:
            prefix = _prefix(suite_dir, "tap")
            # only a fallback for logs truncated before the summary block -
            # the Test Summary Report branch above already caught this file
            # if the log ran to completion, and _add is a no-op on repeats.
            _add(failed, seen, f"{prefix}/{tap_file}", MAX_FAILED)

    return executed, failed


def parse_ccache(text):
    """Return (hit, miss), defaulting to 0 on anything unrecognized.

    ccache's -s output format changed across the major versions spanning
    our eras (old: flat "cache hit (direct)" counters, new: a nested
    "Cacheable calls" tree). Both are checked; neither being present just
    yields zeros instead of an error.
    """
    if not text:
        return 0, 0

    direct = re.search(r"^cache hit \(direct\)\s+(\d+)", text, re.M)
    preproc = re.search(r"^cache hit \(preprocessed\)\s+(\d+)", text, re.M)
    old_miss = re.search(r"^cache miss\s+(\d+)", text, re.M)
    if direct or preproc or old_miss:
        hit = (int(direct.group(1)) if direct else 0) + (
            int(preproc.group(1)) if preproc else 0
        )
        miss = int(old_miss.group(1)) if old_miss else 0
        return hit, miss

    # new ccache: top-level "Hits:"/"Misses:" appear before the indented
    # Direct/Preprocessed breakdown and again under "Local storage:" -
    # take the first match of each, they agree when there's no remote cache.
    hit_m = re.search(r"^\s*Hits:\s+(\d+)\s*/", text, re.M)
    miss_m = re.search(r"^\s*Misses:\s+(\d+)\s*/", text, re.M)
    hit = int(hit_m.group(1)) if hit_m else 0
    miss = int(miss_m.group(1)) if miss_m else 0
    return hit, miss


def read_file(path):
    if not path:
        return ""
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def build_status(build_ok, build_timeout, tests_ok, tests_timeout):
    if not build_ok:
        return "build_timeout" if build_timeout else "build_failed"
    if not tests_ok:
        return "tests_timeout" if tests_timeout else "tests_failed"
    return "success"


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--branch", required=True)
    p.add_argument("--head-sha", required=True)
    p.add_argument("--base-sha", required=True)
    p.add_argument("--pg-major", type=int, required=True)
    p.add_argument("--run-id", type=int, default=0)
    p.add_argument("--run-attempt", type=int, default=1)
    p.add_argument("--build-ok", required=True, choices=["true", "false"])
    p.add_argument("--build-seconds", type=int, default=0)
    p.add_argument("--build-timeout", default="false", choices=["true", "false"])
    p.add_argument("--tests-ok", default="false", choices=["true", "false"])
    p.add_argument("--tests-seconds", type=int, default=0)
    p.add_argument("--tests-timeout", default="false", choices=["true", "false"])
    p.add_argument("--check-world-log", default=None)
    p.add_argument("--extra-failed", default=None)
    p.add_argument("--ccache-log", default=None)
    p.add_argument("--out", required=True)
    return p.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    build_ok = args.build_ok == "true"
    build_timeout = args.build_timeout == "true"
    tests_ok = args.tests_ok == "true"
    tests_timeout = args.tests_timeout == "true"

    status = build_status(build_ok, build_timeout, tests_ok, tests_timeout)
    if status not in VALID_STATUSES:
        raise SystemExit(f"internal error: computed invalid status {status!r}")

    executed, failed = [], []
    if build_ok:
        executed, failed = parse_check_world(read_file(args.check_world_log))
        # some old PG majors' prove never names the file that bailed out
        # (no per-file summary line if it dies before any subtest ran) -
        # the workflow finds these by walking the test log directory
        # instead, and passes the names here as a plain space-separated
        # list. A test that bailed out still ran, so these count as
        # executed too.
        seen = set(failed)
        executed_seen = set(executed)
        for name in read_file(args.extra_failed).split():
            _add(failed, seen, name, MAX_FAILED)
            _add(executed, executed_seen, name, MAX_EXECUTED)

    hit, miss = parse_ccache(read_file(args.ccache_log))

    result = {
        "schema": 1,
        "branch": args.branch,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "head_sha": args.head_sha,
        "base_sha": args.base_sha,
        "pg_major": args.pg_major,
        "status": status,
        "build": {"ok": build_ok, "seconds": args.build_seconds},
        "tests": {
            "ok": tests_ok if build_ok else False,
            "seconds": args.tests_seconds if build_ok else 0,
            "timed_out": tests_timeout,
            "failed": failed,
            "executed": executed,
        },
        "ccache": {"hit": hit, "miss": miss},
    }

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
