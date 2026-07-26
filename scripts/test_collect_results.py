#!/usr/bin/env python3
"""Tests for collect_results.py, stdlib only.

    python3 scripts/test_collect_results.py
    python3 -m unittest discover scripts

The pg_regress fixtures are byte-for-byte what the printf formats in
src/test/regress/pg_regress.c produce, per era:

    PG <= 15 serial    status(_("test %-28s ... "))  + status + " %8.0f ms"
    PG <= 15 parallel  status(_("     %-28s ... "))  + status + " %8.0f ms"
    PG >= 16           emit_tap_output(TEST_STATUS,
                         "%sok %-5i%*s %c %-*s %8.0f ms")

The name padding was %-24s before PG 11 and %-20s before 9.1, which the
regexes absorb; the five-space parallel indent is unchanged from 9.0 to 15.
"""
import json
import os
import tempfile
import unittest

import collect_results as cr


def serial(name, status, ms, width=28):
    return "test %-*s ... " % (width, name) + status + " %8.0f ms" % ms


def parallel(name, status, ms, width=28):
    return "     %-*s ... " % (width, name) + status + " %8.0f ms" % ms


def tap(ok, num, name, ms, in_group):
    return "%sok %-5i%*s %c %-*s %8.0f ms" % (
        "" if ok else "not ",
        num,
        4 if ok else 0,
        "",
        "+" if in_group else "-",
        36,
        name,
        ms,
    )


class RegressSerial(unittest.TestCase):
    def test_ok_and_failed(self):
        log = "\n".join([
            "# +++ regress check in src/test/regress +++",
            serial("boolean", "ok    ", 38),
            serial("numeric", "FAILED", 1247),
        ])
        executed, failed = cr.parse_check_world(log)
        self.assertEqual(executed, ["regress/boolean", "regress/numeric"])
        self.assertEqual(failed, ["regress/numeric"])

    def test_narrow_padding_and_no_runtime(self):
        # verbatim from a PG 10 run: %-24s padding, and the runtime column
        # only arrived later, so nothing may be anchored to it
        log = "\n".join([
            "test boolean                  ... ok",
            "test numeric                  ... FAILED",
        ])
        executed, failed = cr.parse_check_world(log)
        self.assertEqual(executed, ["regress/boolean", "regress/numeric"])
        self.assertEqual(failed, ["regress/numeric"])

    def test_child_failure_suffix_still_matches(self):
        # log_child_failure() appends to the status line before the runtime
        log = "\n".join([
            "# +++ regress check in src/test/regress +++",
            serial("crash", "FAILED (test process exited with exit code 2)", 55),
        ])
        executed, failed = cr.parse_check_world(log)
        self.assertEqual(executed, ["regress/crash"])
        self.assertEqual(failed, ["regress/crash"])


class RegressParallel(unittest.TestCase):
    def test_group_ok_and_failed(self):
        log = "\n".join([
            "# +++ regress check in src/test/regress +++",
            serial("test_setup", "ok    ", 353),
            "parallel group (3 tests):  boolean char name",
            parallel("boolean", "ok    ", 38),
            parallel("char", "FAILED", 41),
            "     name                     ... ok",  # PG 10, no runtime
        ])
        executed, failed = cr.parse_check_world(log)
        self.assertEqual(
            executed,
            [
                "regress/test_setup",
                "regress/boolean",
                "regress/char",
                "regress/name",
            ],
        )
        self.assertEqual(failed, ["regress/char"])

    def test_indent_is_the_discriminator(self):
        # anything but exactly five leading spaces is not a pg_regress line
        log = "\n".join([
            "# +++ regress check in src/test/regress +++",
            "    boolean                      ... ok           38 ms",
            "      char                       ... ok           41 ms",
            "boolean                          ... ok           38 ms",
        ])
        self.assertEqual(cr.parse_check_world(log), ([], []))


class RegressTap(unittest.TestCase):
    def test_ok_and_not_ok(self):
        log = "\n".join([
            "# +++ regress check in src/test/regress +++",
            tap(True, 1, "test_setup", 353, False),
            "# parallel group (2 tests):  boolean char",
            tap(False, 2, "char", 41, True),
            tap(True, 3, "boolean", 38, True),
            # long names push past the padding, leaving one space before the
            # runtime column
            tap(True, 4, "a_very_long_test_name_that_exceeds_the_width", 5, False),
        ])
        executed, failed = cr.parse_check_world(log)
        self.assertEqual(
            executed,
            [
                "regress/test_setup",
                "regress/char",
                "regress/boolean",
                "regress/a_very_long_test_name_that_exceeds_the_width",
            ],
        )
        self.assertEqual(failed, ["regress/char"])

    def test_plain_tap_subtests_are_not_regress_lines(self):
        # a .pl file's own subtests have no runtime column, and must not be
        # mistaken for pg_regress tests or the fallback below never fires
        log = "\n".join([
            "# +++ tap check in src/test/recovery +++",
            "[10:00:00] t/013_partition.pl .. ",
            "ok 1 - subscriber connected",
            "not ok 12 - subscriber received data",
        ])
        executed, failed = cr.parse_check_world(log)
        self.assertEqual(executed, ["recovery/013_partition"])
        self.assertEqual(failed, ["recovery/013_partition"])


class IgnoredFailures(unittest.TestCase):
    def test_ignored_is_executed_but_not_failed(self):
        log = "\n".join([
            "# +++ regress check in src/test/regress +++",
            serial("tablespace", "failed (ignored)", 12),
            "parallel group (1 tests):  int2",
            parallel("int2", "failed (ignored)", 9),
        ])
        executed, failed = cr.parse_check_world(log)
        self.assertEqual(executed, ["regress/tablespace", "regress/int2"])
        self.assertEqual(failed, [])


class SuitePrefix(unittest.TestCase):
    def test_isolation_and_missing_header(self):
        log = "\n".join([
            serial("orphan", "ok    ", 1),
            "# +++ isolation check in src/test/isolation +++",
            serial("read-committed", "ok    ", 20),
        ])
        executed, _ = cr.parse_check_world(log)
        self.assertEqual(executed, ["regress/orphan", "isolation/read-committed"])


class Prove(unittest.TestCase):
    def test_progress_lines_are_executed(self):
        log = "\n".join([
            "# +++ tap check in src/test/recovery +++",
            "[15:04:05] t/001_stream_rep.pl .......... ok    45123 ms",
            "[15:04:51] t/002_archiving.pl ........... ok     8123 ms",
        ])
        executed, failed = cr.parse_check_world(log)
        self.assertEqual(
            executed, ["recovery/001_stream_rep", "recovery/002_archiving"]
        )
        self.assertEqual(failed, [])

    def test_summary_report(self):
        log = "\n".join([
            "# +++ tap check in src/bin/pg_dump +++",
            "Test Summary Report",
            "-------------------",
            "t/001_basic.pl (Wstat: 0 Tests: 5 Failed: 1)",
            "  Failed test:  3",
            "t/002_pg_dump.pl (Wstat: 6400 (exited 25) Tests: 3 Failed: 0)",
            "t/003_pg_dump_pglz.pl (Wstat: 0 Tests: 3 Failed: 0)",
        ])
        executed, failed = cr.parse_check_world(log)
        self.assertEqual(
            executed,
            [
                "pg_dump/001_basic",
                "pg_dump/002_pg_dump",
                "pg_dump/003_pg_dump_pglz",
            ],
        )
        self.assertEqual(failed, ["pg_dump/001_basic", "pg_dump/002_pg_dump"])


class Caps(unittest.TestCase):
    def test_caps(self):
        lines = ["# +++ regress check in src/test/regress +++"]
        for i in range(2500):
            lines.append(serial("t%04d" % i, "FAILED", 1))
        executed, failed = cr.parse_check_world("\n".join(lines))
        self.assertEqual(len(executed), cr.MAX_EXECUTED)
        self.assertEqual(len(failed), cr.MAX_FAILED)
        self.assertEqual(executed[0], "regress/t0000")
        self.assertEqual(executed[-1], "regress/t1999")

    def test_name_truncation(self):
        log = "\n".join([
            "# +++ regress check in src/test/regress +++",
            serial("x" * 300, "FAILED", 1),
        ])
        executed, failed = cr.parse_check_world(log)
        self.assertEqual(cr.MAX_NAME, 64)
        self.assertEqual(len(executed[0]), cr.MAX_NAME)
        self.assertEqual(executed, failed)

    def test_empty_log(self):
        self.assertEqual(cr.parse_check_world(""), ([], []))


class Main(unittest.TestCase):
    def run_main(self, log, extra):
        with tempfile.TemporaryDirectory() as d:
            log_path = os.path.join(d, "check-world.log")
            extra_path = os.path.join(d, "extra.txt")
            out_path = os.path.join(d, "result.json")
            with open(log_path, "w") as f:
                f.write(log)
            with open(extra_path, "w") as f:
                f.write(extra)
            cr.main([
                "--branch", "b", "--head-sha", "a" * 40, "--base-sha", "b" * 40,
                "--pg-major", "14", "--build-ok", "true", "--tests-ok", "false",
                "--check-world-log", log_path, "--extra-failed", extra_path,
                "--out", out_path,
            ])
            with open(out_path) as f:
                return f.read()

    def parse_main(self, log, extra):
        return json.loads(self.run_main(log, extra))

    def test_extra_failed_lands_in_both_lists(self):
        log = "\n".join([
            "# +++ regress check in src/test/regress +++",
            serial("boolean", "ok    ", 38),
        ])
        result = self.parse_main(log, "recovery/001_stream_rep subscription/013_x\n")
        self.assertEqual(result["status"], "tests_failed")
        self.assertEqual(
            result["tests"]["failed"],
            ["recovery/001_stream_rep", "subscription/013_x"],
        )
        self.assertEqual(
            result["tests"]["executed"],
            ["regress/boolean", "recovery/001_stream_rep", "subscription/013_x"],
        )

    def test_extra_failed_dedupes_against_the_log(self):
        log = "\n".join([
            "# +++ tap check in src/test/recovery +++",
            "t/001_stream_rep.pl (Wstat: 0 Tests: 5 Failed: 1)",
        ])
        result = self.parse_main(log, "recovery/001_stream_rep\n")
        self.assertEqual(result["tests"]["failed"], ["recovery/001_stream_rep"])
        self.assertEqual(result["tests"]["executed"], ["recovery/001_stream_rep"])

    def test_worst_case_payload_fits_the_reader_budget(self):
        # the reader rejects an oversize payload outright instead of trimming
        # it, so the caps have to guarantee the fit. Every name here is
        # unique in its leading digits and long enough to hit MAX_NAME, which
        # is the largest result.json the caps allow.
        reader_max_bytes = 256 * 1024
        lines = ["# +++ regress check in src/test/regress +++"]
        for i in range(cr.MAX_EXECUTED + 500):
            lines.append(serial("%04d%s" % (i, "x" * cr.MAX_NAME), "FAILED", 1))
        text = self.run_main("\n".join(lines), "")
        result = json.loads(text)
        self.assertEqual(len(result["tests"]["executed"]), cr.MAX_EXECUTED)
        self.assertEqual(len(result["tests"]["failed"]), cr.MAX_FAILED)
        self.assertEqual({len(n) for n in result["tests"]["executed"]}, {cr.MAX_NAME})
        self.assertLess(len(text.encode()), reader_max_bytes)


if __name__ == "__main__":
    unittest.main()
