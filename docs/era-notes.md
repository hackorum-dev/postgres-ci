# Era notes

Implementation history for each build-env image. Not package docs - see the
top-level README for that. This is where per-family gotchas and measurements
get recorded so the next family doesn't hit the same thing blind. Entries
here are measurements with the run/log they came from; anything inferred
rather than observed is marked as such.

## Headline finding: era images are necessary, and the reason was perl, not glibc

The original worry was that a 2016-2017 PostgreSQL tree would fail to compile
or link against a 2026 glibc/openssl/gcc. That did not happen anywhere.
What actually broke stretch (9.6) was `check-world`'s pg_rewind TAP suite:

```
Can't locate RewindTest.pm in @INC (@INC contains: .../src/test/perl /etc/perl ...)
BEGIN failed--compilation aborted at t/001_basic.pl line 6.
```

9.6's `t/001_basic.pl` does a bare `use RewindTest;` and relies on `.` being
in `@INC`. Perl 5.26 removed `.` from `@INC` by default (CVE-2016-1238);
Debian's stretch perl (5.24.1) already carries that removal. Upstream later
fixed this properly by switching to `use FindBin; use lib $FindBin::RealBin;`,
which does not depend on `@INC` at all - so this is not a live problem for
newer trees, only old ones. The fix is `PERL_USE_UNSAFE_INC=1`, which restores
the pre-5.26 behavior. Once that env var actually reached the test process
(see the sudo/ccache section below), 9.6 built and passed `check-world`
cleanly on `debian/eol:stretch` with no other changes. First hard evidence
that era-matched images are pulling their weight, and not for the reason
anyone guessed going in.

## autoconf 2.69

- postgres's `configure.ac` has `m4_if(m4_defn([m4_PACKAGE_VERSION]), [2.69],
  [], [m4_fatal(...)])` at both the 9.6 and current-dev ends - verified by
  reading the source, so this is a property of postgres, not a guess.
- Building 2.69 from source takes ~3.5s and `autoconf --version` reports
  2.69 - measured on trixie and stretch.
- That only proves the tarball compiles and reports its version. It says
  nothing about whether `autoconf` actually regenerates a working `configure`
  from `configure.ac` - the operation `patch-ci.yml` will need whenever a
  patch touches `configure.ac`. That path is now exercised directly in the
  image build's warm-up loop (`as-ci autoconf -f` before `./configure`).
  Confirmed in run 30200702902, stretch leg log:
  ```
  #13 5.720 + as-ci autoconf -f
  #13 6.192 + as-ci ./configure --enable-debug --enable-cassert --enable-tap-tests
  ```
  ~0.5s per reference commit, ran before all three configures on all three
  families. Durations for that run - bullseye 12.9 min, stretch 17.0 min,
  trixie 24.5 min - are within noise of the prior parallel-check-world run,
  so regeneration changed nothing measurable.
- Distro-shipped versions differ: trixie 2.72-3.1, bookworm 2.71-3, bullseye
  already ships 2.69-14. So building from source is redundant on bullseye
  specifically - harmless, kept anyway for one code path across eras rather
  than a per-family conditional.

## trixie (pg18, pg19, pg20)

- Run 30196327409 (build/trixie), green first try, no iteration needed.
  09:22:02 to 10:07:58, 45 min, serial check-world (`-Otarget`, no `-j`).
- `ccache -s`: Cacheable calls 4922/6160 (79.90%).
- All three tags pushed: pg18, pg19, pg20.
- After parallelising check-world (`-j$(nproc) PROVE_FLAGS=--timer`, see
  below), re-verified green in run 30199806638: 11:16:10 to 11:42:35,
  26.4 min for three majors (~8.8 min/major, down from ~15).

## bullseye (pg14, pg15)

- Run 30197785276 (build/bullseye), green first try, no iteration needed.
  10:10:21 to 10:37:39, 27 min, serial check-world.
- Tags pg14 and pg15 pushed.
- `ccache -s` after the parallel-check-world run (30199806638): cache hit
  (direct) 0, cache hit (preprocessed) 54, cache miss 2845, hit rate 1.86%.
- Re-verified green with parallel check-world in run 30199806638: 11:16:10 to
  11:27:47, 11.6 min for two majors (~5.8 min/major, down from ~13.5).

## `-j` on check-world

Upstream's own CI (`.github/workflows/pg-ci.yml` in the postgres repo) runs
`make -s check-world PROVE_FLAGS=--timer -Otarget -j4`; ours ran serial. Once
the wrapper below was fixed, we switched to `-j"$(nproc)" PROVE_FLAGS=--timer`
in the warm-up loop. Effect, comparing the first (serial) build of each
family to the parallel one:

| family | serial | parallel | per-major serial -> parallel |
|---|---|---|---|
| trixie (3 majors) | 45 min | 26.4 min | ~15 min -> ~8.8 min |
| bullseye (2 majors) | 27 min | 11.6 min | ~13.5 min -> ~5.8 min |

`--timer` adds per-test timings to the log - the data needed to set
per-branch CI timeouts from measurement instead of guesswork.

## stretch (pg9, pg10, pg11)

Three iterations to green; each failure was a real finding, not noise.

1. **Run 30198628272**, first attempt: died 6s into the first `./configure`
   with `/usr/local/bin/as-ci: 2: exec: setpriv: not found`.
   `debian/eol:stretch`'s minimal rootfs doesn't ship `setpriv` (part of
   `util-linux`). Rather than adding a package, `as-ci` was rewritten around
   `sudo` (already installed everywhere) instead of `setpriv` - one code path
   across all eras, since `sudo` exists on every era we build.
2. **`sudo`'s `env_reset`/`secure_path` strip `CCACHE_DIR` and `PATH`** by
   default, which would silently disable ccache rather than fail loudly. Two
   follow-on ccache-assertion bugs while getting the check right:
   - First attempt asserted `command -v ccache | grep -q /usr/lib/ccache` -
     wrong target. `/usr/lib/ccache` holds compiler symlinks (`gcc`, `cc`,
     ...), not a `ccache` binary, so this failed even with a correct
     environment (run 30198775676, all three legs failed on this assertion
     alone in ~1 min).
   - Fixed to check the compiler instead: `command -v gcc | grep -q
     /usr/lib/ccache`, combined with asserting `CCACHE_DIR` and the running
     user. This is what actually caught the real regression next.
3. **Run 30198871249**: trixie and bullseye green, stretch failed - this
   time on the pg_rewind/`@INC` issue described above (all builds up to this
   point had run with `PERL_USE_UNSAFE_INC` unset). Fixed by setting
   `PERL_USE_UNSAFE_INC=1` in the image `ENV` and adding it to the `as-ci`
   env passthrough (same sudo-stripping problem as `CCACHE_DIR`), plus adding
   it to the ccache-env assertion so a future wrapper edit that drops it
   fails the image build instead of silently reverting stretch.
4. **Run 30199806638**: all three families green, including stretch.
   11:16:11 to 11:34:38, 18.5 min for three majors (~6.2 min/major) - cheaper
   per major than trixie's ~8.8, because a 2016 tree and its test suite are
   smaller. Old eras are cheaper to run, not dearer.
   `ccache -s`: cache hit (direct) 1, cache hit (preprocessed) 33, cache miss
   3677, hit rate 0.92%.
   Tags pg9, pg10, pg11 pushed.
5. **Run 30200702902**: re-verified green with `autoconf -f` added to the
   warm-up loop (see the autoconf section above) - stretch 17.0 min for
   three majors, no regression.

## buster (pg12, pg13) and bookworm (pg16, pg17)

The two families that shipped as `enabled: false` stubs - `majors` listed but
no reference commits and no `runtime_packages`. Together they cover 1254 of
the 4830 current patch branches carrying a base commit (26%), every one of
which sat in won't-retry with `no era image for pgNN` until these landed.

### `debian:buster` could never have worked - buster is archive-only

The stub had `base_image: debian:buster`. Checked both mirrors directly:

```
https://deb.debian.org/debian/dists/buster/Release      404
https://archive.debian.org/debian/dists/buster/Release  200
```

Buster left the live mirrors, so `apt-get update` on the official
`debian:buster` image 404s on every index before installing anything. Changed
to `debian/eol:buster` (that tag does exist - checked Docker Hub's tag list
for `debian/eol`), which points its sources at archive.debian.org, same as
stretch already did. `eol: true` was already set on the stub, so
`build-env.Dockerfile`'s `Acquire::Check-Valid-Until "false"` path handles
the long-expired Release file with no further change.

bookworm needs none of this - it still resolves on deb.debian.org (200), so
`debian:bookworm` / `eol: false` was already right.

### Reference commits: the corpus's own median base, not a window midpoint

The existing eight sit near the midpoint of each major's devel window
(`Stamp HEAD as Ndevel` to the next one). These four are instead the
branch-weighted median base commit of the corpus for that major - the point
half the branches sit before and half after:

| major | commit | date | branches |
|---|---|---|---|
| 12 | 68a13f28bebc9eb70cc6988bfa2daaf4500f519f | 2019-01-02 | 328 |
| 13 | 7559d8ebfa11d98728e816f6b655582ce41150f3 | 2020-01-01 | 344 |
| 16 | c8e1ba736b2b9e8c98d37a5b77c4ed31baf94147 | 2023-01-02 | 299 |
| 17 | 29275b1d177096597675b5c6e7e7c9db2df8f4df | 2024-01-03 | 283 |

These land 3-12 days from where the window-midpoint rule would have put them,
so the two rules agree to within noise here - the corpus is spread fairly
evenly across each devel cycle. Using the median anyway costs nothing and
guarantees the warm-up compiles a tree real patchsets actually sit on, rather
than one no branch uses.

Each was verified before use: on `master`, `AC_INIT` reporting the expected
`Ndevel`, and resolving on `hackorum-dev/postgres` so the Dockerfile's
`git fetch --depth 1 origin <sha>` can find it. Note 12 and 13 carry
`configure.in`, not `configure.ac` - the rename lands in 14. `EraDetector`
reads both, and `autoconf -f` in the warm-up loop takes either.

### runtime_packages: bookworm's ldap is a third distinct name

Same method as the first three families - every name checked against that
release's real `Packages.gz` (archive.debian.org for buster,
deb.debian.org for bookworm), plus each `-dev` package's `Depends:` field, so
the runtime list matches what `build-env.Dockerfile` actually links against
instead of matching a version number by eye.

| library  | buster        | bookworm       |
|----------|---------------|----------------|
| openssl  | libssl1.1     | libssl3        |
| readline | libreadline7  | libreadline8   |
| icu      | libicu63      | libicu72       |
| ldap     | libldap-2.4-2 | libldap-2.5-0  |
| perl     | libperl5.28   | libperl5.36    |

The earlier note guessed ahead for these two families and got half of it
right. It predicted bookworm would not need trixie's `t64` suffix - correct,
`libssl3` and `libreadline8` are the real names. It said nothing about ldap,
which is the one that would have been guessed wrong: bookworm ships OpenLDAP
2.5 as `libldap-2.5-0`, a third name distinct from both stretch/bullseye's
`libldap-2.4-2` and trixie's `libldap2`. It does not resolve directly either -
`libldap2-dev` depends on `libldap-dev`, which depends on `libldap-2.5-0`, so
it takes two hops to reach. Four names for one library across five families;
there is no rule here, only the archive.

Neither family needs `extra_packages` - every package
`build-env.Dockerfile` installs, plus `gosu` for the runtime image, exists in
both releases.

### Built while still disabled, on purpose

`enabled: true` and the app's `supported?` were held back until the tags were
confirmed pullable. `PushGuard` computes the major from `base_sha` live rather
than from `patch_branches.pg_major` (which is entirely NULL - that backfill
still hasn't run), so the flip makes all 1254 branches push-eligible
immediately. Flipping before the images exist would have failed every one of
them on `docker pull` and stamped `infra_error` across a quarter of the
corpus.

`build-era-images.yml`'s matrix job allows exactly this: the `enabled` check
only applies when no family was named, so a `[build <family>]` marker or a
`workflow_dispatch` naming one family builds a disabled family, while
`[build all]` still skips it. Build first, flip second.

## All eight tags, final green run (30199806638, reconfirmed in 30200702902)

pg9, pg10, pg11 (stretch), pg14, pg15 (bullseye), pg18, pg19, pg20 (trixie).

## Trigger mechanism: commit-message marker instead of build/* refs

Replaced the four `build/<family>` refs (force-pushed copies of `main`, so
they could never actually drift - just four names for one line of history)
with a marker on the pushed commit's subject line: `[build <family>]`, or
`[build all]` for every enabled family. No marker means no build; the
`matrix` job still runs (cheap) but emits `families=[]`, which the existing
`if: needs.matrix.outputs.families != '[]'` guard turns into a skipped
`build` job.

The marker regex only looks at the first line (subject) of the commit
message, not the whole thing. A squashed merge's commit message body is
built out of every individual commit message it absorbs; if the regex
scanned the whole message, a stray marker in an old WIP commit
(`wip: try again [build stretch]`) would resurrect itself in that body and
fire a build nobody asked for at merge time. Scoping to the subject line
closes that off entirely, and costs nothing - the marker belongs on the
subject line of the commit that lands anyway.

Both paths verified from actual run output, not just reasoning:

- **No marker skips the build**: run 30201725270 (subject `ci: trigger
  builds from commit marker, not build/* refs`, no marker anywhere in it).
  `matrix`: success. `build`: **skipped**. Confirms the no-marker-no-build
  contract holds for an ordinary push to `main`.
- **A single-family marker builds only that family**: run 30201765841
  (subject `rebuild stretch [build stretch]`, pushed as an empty commit).
  `matrix` emitted exactly one family entry (stretch); the `build` job's
  only matrix leg was `stretch, debian/eol:stretch, ...`, and it completed
  success (pg9/pg10/pg11 rebuilt).
- **Only the tip commit's subject counts, even across a multi-commit
  push**: the push that landed the runtime-image work (Part 2) contained
  two commits - the implementation commit (no marker) followed by an empty
  `[build all]` commit. `github.event.head_commit` is only ever the last
  commit of a push, so an earlier commit's marker (or lack of one) in the
  same push is irrelevant; only the tip's subject line is read. This is
  deterministic but not obvious, so noting it here: **the marker must be on
  the last commit of whatever you push**, not on some earlier commit in the
  same push.

Old refs deleted after both paths were confirmed:
`build/all`, `build/trixie`, `build/bullseye`, `build/stretch`.

## Runtime images (`pg<major>-runtime`) and the vendored entrypoint

### Entrypoint pin: docker-library/postgres@bc22a9fc356444f4a4c51dd681750b0ee0046959

Found by walking the commit history of `docker-entrypoint.sh` in
`docker-library/postgres` looking for where old-release support ends.
The commit `36abfddd6f7235770d00f8546b199936b0ca77aa` ("Remove 9.6 (EOL)")
has a single parent, `bc22a9fc...`, so that parent is the last commit where
the shared entrypoint template still had 9.6 in scope. Diffing the two
confirms the removal touched exactly one thing in the shared
`docker-entrypoint.sh`, inside `pg_setup_hba_conf`:

```
-	# postgres 9 only reports "on" and not "md5"
-	if [ "$auth" = 'on' ]; then
-		auth='md5'
-	fi
```

This is genuine version-conditional logic, not boilerplate: `postgres -C
password_encryption` reports the bare string `on` on 9.x instead of `md5`,
so without this shim the script picks the wrong default auth method on
9.x. It is not version-agnostic - the concern in the task was correct, not
overstated. Nothing 10-specific needed removing at this point (the
`xlog`->`wal` rename PG10 needed was handled by an earlier, 2017 commit
that is still present), so `bc22a9fc...` handles both 9.x and 10 correctly.
Confirmed current upstream has dropped old-release handling entirely, not
just this one shim: `docker-library/postgres`'s HEAD tree today only has
`14/ 15/ 16/ 17/ 18/ 19/` version directories - 9.6 through 13 are gone
completely. So vendoring an old commit was necessary, not optional.

`images/docker-entrypoint.sh` carries the script unmodified below a header
comment recording the pin; `diff` against the fetched blob confirms the
body is byte-identical. `bash -n` on it is part of the runtime image build
(see below).

Deviation from upstream: `POSTGRES_HOST_AUTH_METHOD=trust` is set as an
`ENV` in `runtime.Dockerfile`, not in the script - the vendored script is
untouched and still prints its own loud warning ("WARNING:
POSTGRES_HOST_AUTH_METHOD has been set to trust...") unmodified when trust
is in effect. Setting `POSTGRES_PASSWORD` restores upstream's default
(refuse to start without one).

### runtime_packages: zero iteration rounds needed

Each family's list (openssl, readline, ldap, icu, libxml2, libxslt1.1, pam,
krb5, selinux, uuid, zlib1g, perl) was checked against that release's real
package index before pushing - `archive.debian.org` for stretch (EOL),
`deb.debian.org` for bullseye and trixie - rather than guessed. Names that
actually differ between the three enabled families:

| library  | stretch        | bullseye       | trixie          |
|----------|----------------|----------------|-----------------|
| openssl  | libssl1.1      | libssl1.1      | libssl3t64      |
| readline | libreadline7   | libreadline8   | libreadline8t64 |
| ldap     | libldap-2.4-2  | libldap-2.4-2  | libldap2        |
| icu      | libicu57       | libicu67       | libicu76        |
| perl     | libperl5.24    | libperl5.32    | libperl5.40     |

(krb5, selinux, uuid, zlib1g, libxml2, libxslt1.1 kept the same package
name across all three.) Two things worth flagging for when buster/bookworm
get enabled:
- stretch's `libssl-dev` (already installed by `build-env.Dockerfile`)
  depends on `libssl1.1`, not `libssl1.0.2` - checked the actual `Depends:`
  field rather than assuming stretch means OpenSSL 1.0.2.
- trixie's Debian 13 64-bit-time_t transition appended a `t64` suffix to
  `libssl3`/`libreadline8`'s package names (`libssl3t64`,
  `libreadline8t64`) - the bare names do not exist in trixie's archive.
  bookworm predates this transition, so its names are unlikely to need it,
  but that is inferred from the transition's Debian 13 timing, not checked
  against bookworm's actual archive.

All three lists built clean on the first push (run 30202412904) - no
`Unable to locate package` or similar in any of the three build logs, so
no iteration round was actually needed this time. The task noted iteration
would likely be needed; it wasn't, this round.

### What is checked at image-build time vs what is still unverified

The image build (`runtime.Dockerfile`'s last `RUN`, all three legs of run
30202412904) asserts, and confirmed passing in every leg's log:
`command -v gosu` resolves, `gosu nobody true` runs, `id -u postgres` is
999, uid 999 resolves back to the name `postgres` via `getent`, `bash -n`
parses the vendored entrypoint with no syntax error, the entrypoint file
is executable, and `en_US.utf8` shows up in `locale -a`.

None of that proves a server actually starts. This image has no compiled
postgres in it - `RUNTIME_PACKAGES` only supplies the shared libraries a
future compiled binary will need, there is no `postgres` binary here yet.
`ENTRYPOINT`/`CMD ["postgres"]` will fail immediately if run bare today.
A working server is unverified and stays that way until the patch-CI
publish job (later work) composes an image with a real install tree on
top of this one. Not tested here; not claiming otherwise.

### Image sizes (compressed, as pulled - GHCR manifest, anonymous token)

| tag           | size    | tag  | size    |
|---------------|---------|------|---------|
| pg9-runtime   | 82.6 MB | pg9  | 351.6 MB |
| pg10-runtime  | 82.6 MB | -    | -        |
| pg11-runtime  | 82.6 MB | -    | -        |
| pg14-runtime  | 93.1 MB | pg14 | 366.8 MB |
| pg15-runtime  | 93.1 MB | -    | -        |
| pg18-runtime  | 92.9 MB | pg18 | 496.7 MB |
| pg19-runtime  | 92.9 MB | -    | -        |
| pg20-runtime  | 92.9 MB | -    | -        |

Runtime images are 4-5x smaller than the build images (no compiler
toolchain, no `-dev` packages, no ccache), which matches their purpose -
something someone pulls just to run a patch, not to build one.

All sixteen tags (`pg9`..`pg20` build images, `pg9-runtime`..`pg20-runtime`
runtime images) published from run 30202412904 and pull anonymously.

## as-ci only forwards a fixed env list - a shell prefix does not cross it

`as-ci` is `exec sudo -u ci -H CCACHE_DIR=... CCACHE_MAXSIZE=... PERL_USE_UNSAFE_INC=... PATH=... -- "$@"`.
It forwards exactly those four names and nothing else - `sudo`'s `env_reset`
strips everything not explicitly passed. This matters for patch-ci.yml's
check-world step: `PGCTLTIMEOUT=120` has to widen pg_ctl's start/stop wait
for TAP tests under `-j` contention, but setting it as a shell prefix in
front of `as-ci` (`PGCTLTIMEOUT=120 as-ci make ...`) does nothing - it only
lands in as-ci's own environment, not the sudo'd one, and fails silently
instead of erroring.

Fix: put it on the `make` command line instead
(`as-ci make check-world ... PGCTLTIMEOUT=120`). `make` is the process that
actually runs as `ci` (as-ci wraps the whole make invocation), and GNU make
exports command-line-assigned variables to every recipe's environment by
default - so this one does cross, no wrapper change needed.

The existing `PG_TEST_EXTRA= as-ci make ...` prefix in this image's own
warmup loop looks like the same mistake, but isn't one in practice: an
empty `PG_TEST_EXTRA` and a wholly unset one are the same to check-world
(no opt-in extra tests either way), so the prefix not crossing the sudo
boundary changes nothing observable. Anyone adding a *non-empty* env var
this same way will get a silent no-op, not an error - remember this needs
to be a `make` var, not a shell prefix.

## Patch CI: build-and-test / publish / report

The reusable workflow patch branches call into. Verified end to end with
three throwaway probe branches (`probe_pg10`, `probe_pg15`, `probe_pg20`,
one per era), each carrying a real reference commit and calling the
workflow with made-up topic/message ids. Deleted once green; their
`refs/hackorum-ci/*` result refs were left in place on purpose, as
fixtures for testing the database-side ingestion.

### `container:` jobs don't work on old eras - same glibc problem as before

First attempt ran `build-and-test` with the era image as the job's own
`container:`. Died instantly on stretch (run 30204301765):

```
/__e/node24/bin/node: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.27' not found
```

GitHub injects its own Node into a job container to run bundled actions
(checkout, upload-artifact, ...); stretch's glibc (2.24) is too old for
it, same root cause as the pg_rewind/perl issue above but hitting the
platform this time, not the payload. Forcing `ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION`
to pin Node 20 was tried first and didn't fully stick - some actions still
got forced onto Node 24 regardless, per the run's own warnings. Fixed
properly by dropping `container:` altogether: the job runs plain on
`ubuntu-latest`, and the era image is only ever entered via `docker exec`.
No action ever executes inside the era image; it's used for compiling and
testing postgres only.

### ccache only hit once the bind mount path matched the image's own build path

Early runs showed a near-zero ccache hit rate despite the image shipping
a pre-warmed cache. Cause: ccache's default `hash_dir` setting bakes the
*compilation directory* into the cache key, and the checkout was mounted
under `$GITHUB_WORKSPACE`'s own path - a different string every run,
different from `/tmp/pg`, where the image's own warm-up build compiled
the same reference commit. Mounting the checkout at `/tmp/pg` explicitly
(matching the warm-up build's path) instead of at `$GITHUB_WORKSPACE`
fixed it - confirmed by `ccache -s` hit counts in the "All eight tags"
runs above (1.86%, 0.92%) jumping to real hit rates once this landed
(pg10: 1268 hit / 3677 miss in the final green run - the misses are all
non-reference commits and contrib modules the warm-up never touched, not
a caching failure).

### `$RUNNER_TEMP`, not the checkout, for anything the host itself writes

`chown -R ci:ci /tmp/pg` (needed so the `ci` user inside the container can
write into the bind mount) applies host-side too, since it's the same
mount. Any step where the *runner's own user* tries to create a new file
under `$GITHUB_WORKSPACE` afterward fails outright. Hit this twice:

- `ccache -s > ccache-stats.txt` - failed silently, because `|| true` on
  the same line swallowed the redirect failure along with a real
  `docker exec` failure. Looked green, wrote nothing.
- `collect_results.py --out result.json` - failed loudly (run 30205035056):
  `PermissionError: [Errno 13] Permission denied: 'result.json'`, which is
  what actually surfaced the bug - the ccache case would have stayed
  invisible on its own.

Fixed by writing both to `${{ runner.temp }}` instead - a directory the
runner's own user owns regardless of what happened to the checkout.

### 0700 tmp_check dirs mean only root-inside-the-container can read test output

`pg_regress`/TAP test data dirs come out mode 0700 owned by `ci`. The host
runner user can't list them, let alone read a `regression.diffs` or a
`regress_log_*` out of them - has to happen via `docker exec` (default
user root, since the container itself was started with `--user root`),
while the container is still alive, handing the host a single file
instead of trying to walk the tree from outside. Two places need this:
collecting `regression.diffs` for the artifact, and printing a failing
test's own log to the run log (next section).

### Making test failures readable from the run log alone

Originally a test failure meant: no `regression-diffs` artifact (pg_regress
only writes `regression.diffs` on an actual diff mismatch, not a TAP
bailout), so the only trace of *why* was inside `tmp_check/log/regress_log_*`
- which nothing surfaced, and which nobody should have to download an
artifact zip to read at scale. Added a step that runs inside the container
(same root-access reason as above), tails (300 lines, bounded) every
`regress_log_*` that contains `not ok` or `Bail out`, plus any per-node
`.log` file next to it, plus any `regression.diffs` - straight into the
run log.

That step turned out to be load-bearing for something else too:
`collect_results.py`'s failed-test list came back empty on some real
failures, because prove's own progress output never names the file that
bailed if it dies before finishing a single subtest - no per-file
"Test Summary Report" line to parse, nothing. Confirmed by diffing two
real bailouts side by side: the pg20 case (bails mid-suite, after other
files already completed) gets a `t/017_shm.pl (Wstat: 65280 ... Tests: 0
Failed: 0)` summary line; the pg15 case (bails on the very first
`$node->start` in the suite) gets none - `parse_check_world()` on that
run's actual log returns `[]`. Since the log-printing step already walks
the log directory to find the failing file by matching `not ok`/`Bail out`
inside it, it also emits those names as a plain list
(`HACKORUM_FAILED_TESTS: recovery/017_shm`), which `collect_results.py`
now reads via `--extra-failed` and merges in - filesystem ground truth
instead of a guess at "whichever file didn't get a summary line."

### `recovery/017_shm` failing on every PG14+ branch - the real blocker

Signature: `node "gnat" is already running` or `pg_ctl start failed` /
`lock file "postmaster.pid" already exists`, `Is another postmaster (PID
N) running`, always in the recovery suite, always around a `kill9`/restart
pair. `017_shm.pl` exists from PG14 on, so left unfixed this would have
stamped `tests_failed` on roughly a third of the whole corpus regardless
of what the patch actually did - indistinguishable from a real regression.

**Root cause: the era container has no init process, so a killed
postmaster never gets reaped.** The container is started as
`docker run -d ... tail -f /dev/null` - `tail` is PID 1 inside it, and
`tail` never calls `wait()` on anything. `017_shm.pl` (like other
crash/recovery tests) does `$node->kill9` and immediately expects to
start a fresh postmaster in the same data directory. The killed process's
immediate parent (`pg_ctl`) already exited right after confirming the
original start, so the dead postmaster was already reparented to PID 1
before the kill; with no reaper, it stays a zombie forever. A zombie's PID
still answers "alive" to a liveness check, so when the new postmaster (or
`pg_ctl` retrying it) checks whether the PID recorded in `postmaster.pid`
is still running, it gets a false yes and refuses to start, over and over,
until the test gives up and bails.

This is a property of containers in general, not of postgres or of this
test - anything that kills a daemon and expects a clean restart will hit
it, on any image, the moment its entrypoint doesn't reap.

**Why the same reference commit's `check-world` passed during the image
build and not here**, which is what made this confusing for a while: the
image build ran `check-world` inside a `docker build` `RUN` step, and
BuildKit's executor for `RUN` does its own child reaping - it isn't a bare
`tail`/`sh` with no signal handling. Two invocations of the exact same
test, same binary, same `-j`, different only in which process tree is
PID 1 - one reaps, one doesn't. Not the postgres source, not parallelism -
just which init process happens to own PID 1 in that particular run.

Fixed by adding `--init` to the `docker run` that starts the era
container - gives it a real (tini) PID 1 that reaps its children. One
line, no change to the postgres tree or the test itself required, and
none was appropriate: the task was to fix the environment, not to work
around a test that is correctly checking real behavior.

Confirmed green on both affected probes after the fix (pg15 run
30206543592, pg20 run 30206543567) - both `status: success`, `failed: []`.
Side effect worth noting: fixing this also made the affected runs faster,
not just correct - pg15's test time dropped 472s -> 196s and pg20's
385s -> 310s, comparing the last broken run to the first fixed one. The
zombie wasn't free; the suite was burning real wall-clock retrying
`pg_ctl start` in a loop it could never win before finally bailing out.

**Dead end worth recording:** `/dev/shm` sizing (docker's default 64MB
tmpfs limit for a container's shared memory) was a plausible-looking
candidate early on, given the failure is inside a test that deliberately
manipulates SysV shared memory. It wasn't the cause - the actual error is
a stale-PID lock file check, unrelated to shm segment size or space, and
the fix above didn't touch `/dev/shm` at all. Recording this so nobody
re-chases it: if a *different* shm-related failure shows up later, this
is not where it came from.

### The GitHub run conclusion is not the verdict - the payload is

`build-and-test`'s steps are written to always exit 0 (`set +e` plus an
explicit `exit 0` after recording outcomes in step outputs), on purpose -
a failing patch is data, not a CI infrastructure problem, and the job
needs to reach `publish`/`report` regardless so a failing patch still gets
an image and a recorded status. Consequence: the run's own top-level
conclusion in the Actions UI reads `success` even when the tests failed
and the patch is bad. Both `probe_pg15` and `probe_pg20`'s broken runs
show `completed / success` in `gh run list` right up until the fix landed
- the only place the real verdict ever lived is `status` in the pushed
`result.json` payload (`tests_failed`, with the failing test named). The
database-side ingestion already reads the payload, not the run
conclusion, so this doesn't affect it - but anyone eyeballing the Actions
tab to judge a patch by green/red will draw the wrong conclusion at scale.
Treat the payload as the only authoritative source for pass/fail.

### Final measured timings, three eras, after the zombie-reaper fix

| era | run | build | tests | full cycle (build-and-test + publish + report) |
|---|---|---|---|---|
| pg10 | 30205419445 | 8s | 276s | 7m00s |
| pg15 | 30206543592 | 11s | 196s | 6m36s |
| pg20 | 30206543567 | 46s | 310s | 8m06s |

All three: `status: success`, `tests.failed: []`, image published, report
ref landed. Full-cycle wall clock (job creation to the `report` job
finishing) runs a bit ahead of build+test alone - the rest is checkout,
image pull, and the publish job's own image build/push/smoke-test, which
runs as a separate job after build-and-test frees its runner.

At ~6.5-8 minutes per branch end to end and 18 concurrent runner slots,
that's roughly 200 branches/hour for the real backfill - a full pass over
the ~4300-branch corpus is on the order of a day, not weeks.

## Image contract check (`image-contract.yml`)

The `publish` job's smoke test proves a pushed image boots and answers one
query - it says nothing about the actual contract these images claim to
honor (bare `docker run`, the official env vars, init hooks, volume
persistence, contrib extensions), and every one of its own checks goes
through `docker exec`, never the published host port. `image-contract.yml`
exercises that full contract against a fixed sample of already-published
images instead of every patch branch, triggered the same way as
`build-era-images.yml`: a `[contract]` marker on the pushed commit's
subject line.

### `continue-on-error: true` masks `conclusion`, not `outcome` - read the right field

Each of the five contract cases runs as its own step with
`continue-on-error: true`, so one case failing doesn't stop the rest from
running. The consequence, easy to get backwards: once that flag is set,
GitHub Actions forces the step's `conclusion` to `success` regardless of
what actually happened - `outcome` is the only field that still carries the
real result, and `outcome` is only visible from *inside* the same job run
(the `steps.<id>.outcome` expression), never through the REST API
afterward. Querying `GET /repos/.../actions/jobs/{id}` for a finished run
returns `conclusion` only, and every continue-on-error step in it will
report `success` even if it failed outright - there is no way to recover
the true per-step result from that endpoint once the run is over.

This bit for real during this workflow's own rollout: run 30210140086
had every one of the 45 case-attempts (9 images times 5 cases) genuinely
fail - confirmed straight from the run log, e.g. `bare did not become
ready on port 5432 within 60s` - while `gh api .../jobs/<id>` reported
`conclusion: success` for every one of those same steps, because
continue-on-error was doing exactly what it's supposed to do. The
workflow's own `Summarize contract results` step was never fooled, since
it reads `steps.caseN.outcome`, and correctly failed the job. Anyone
auditing this workflow from the API rather than the run log will see all
green cases and a red job and reasonably suspect the summary step is
broken - it isn't; the API just cannot show what continue-on-error hides.
If you need the real per-case result after the fact, read the run's own
log output (the `Summarize` step prints all five), not the jobs API.

### Readiness check: poll the host port, not `docker exec ... pg_isready`

First cut of the wait loop ran `docker exec <container> pg_isready` in a
retry loop, same as the existing smoke test. `docker exec pg_isready`
connects over the container's local socket, which comes up *before* the
real thing. The vendored entrypoint starts a throwaway, local-socket-only
postgres first (to run init scripts), stops it, then starts the real,
TCP-listening one. A `docker exec` check happily reports the throwaway
instance ready, and a host connection made right after catches the gap
between the two servers: `psql: error: ... server closed the connection
unexpectedly` (run 30210039720, every one of the 45 case-attempts, same
failure). Fixed by polling `pg_isready -h 127.0.0.1 -p <port>` from the
runner itself (`scripts/wait_pg_ready.sh`) - that only succeeds once the
real server is listening the same way an actual caller would reach it.

This section originally claimed the patch CI smoke test was safe from
this because it only ever talks to the container over `docker exec`. It
was not. Staying inside the container narrows the gap to the few
milliseconds between the `pg_isready` exec and the `psql` exec, but does
not close it: of 92 failed patch CI runs on 2026-07-27, 72 were healthy
images where the probe passed against the throwaway server and the query
landed after it stopped - `FATAL: the database system is shutting down`
or a socket that was already gone. Successful runs printed the same
"accepted connections after 2s" line, so every run was racing and ~96%
of them won. The smoke test now uses `wait_pg_ready.sh` and a published
port like the contract cases do. There is one readiness rule; nothing
gets to have its own.

### Real defect found: images could never accept a host connection at all

Once the readiness race above was fixed, every case on every image still
failed the same way: `did not become ready ... within 60s`, and
`docker logs` showed why -

```
2026-07-26 16:19:21.251 UTC [1] LOG:  listening on IPv4 address "127.0.0.1", port 5432
```

The real server was listening on loopback only, inside the container's
own network namespace. Docker's `-p 5432:5432` NAT forwards host traffic
to the container's actual network interface, not to its loopback - so a
process bound to `127.0.0.1` there is unreachable from `docker run -p`,
structurally, regardless of pg_hba.conf or `POSTGRES_HOST_AUTH_METHOD`.
Confirmed identical on the oldest (pg10) and newest (pg20) images in the
sample, so this was never a per-major difference - it's universal.

Root cause: the vendored entrypoint only clears `listen_addresses` for its
own throwaway init-scripts server (`docker_temp_server_start`); the real
server just execs `postgres` with whatever `postgresql.conf` `initdb`
generated, whose stock default is `listen_addresses = 'localhost'`.
Upstream's own Dockerfile (at the exact commit this entrypoint is vendored
from, `bc22a9fc...`) patches that default before `initdb` ever runs:

```
sed -ri "s!^#?(listen_addresses)\s*=\s*\S+.*!\1 = '*'!" /usr/share/postgresql/postgresql.conf.sample
```

Our pipeline builds postgres from source (`install-world-bin`) instead of
installing the Debian package, so there's no Dockerfile step doing the
equivalent - and the vendored entrypoint script itself, correctly, doesn't
either (it's not its job upstream; the base image is). Checked the actual
published layers over the OCI registry API (no docker needed - just
`ghcr.io`'s anonymous pull token, the manifest, and the one ~30MB layer
that is `COPY pgsql /usr/local/pgsql`) to find the real path for a
from-source install with `--prefix=/usr/local/pgsql`, since it isn't the
same as the Debian layout: `share/postgresql/postgresql.conf.sample`, not
`share/postgresql.conf.sample`. Verified on both `t37759` (pg10) and
`t44825` (pg20) - same path, same stock line
(`#listen_addresses = 'localhost'`), same sed result on both.

Fixed in `patch-ci.yml`'s `publish` job, right after `COPY pgsql
/usr/local/pgsql`:

```
RUN set -eux; sed -ri "s!^#?(listen_addresses)\s*=\s*\S+.*!\1 = '*'!" /usr/local/pgsql/share/postgresql/postgresql.conf.sample; grep -qxF "listen_addresses = '*'" /usr/local/pgsql/share/postgresql/postgresql.conf.sample
```

The trailing `grep` is load-bearing, not decoration: a future
`postgresql.conf.sample` layout change (a new PG major moving the file, an
upstream install-tree change) would make the `sed` a silent no-op, and
this exact bug would come back with nothing in the build log to say so.
The `grep` turns that into a build failure instead.

This is a real, pre-existing defect in every patch image ever published,
not a version difference to document and move past - "run this and
connect" is the entire point of the pipeline, and it never worked over the
one path (a bare `docker run -p ...`) a reviewer is actually told to use.
The existing smoke test never caught it because it only ever used `docker
exec`, same as the readiness-check bug above.

### Fix verified against real patches, not a one-off image edit

The nine trial images were already published before this fix landed, so
fixing `patch-ci.yml` alone doesn't touch them. Rather than patch and
re-push those nine images directly (different provenance from every other
image in the system, and it would only prove the *sed* works, not that the
real pipeline produces a correct image), the nine source branches in
`hackorum-dev/postgres` (`t37759_1 t37764_1 t38263_8 t40413_113 t42603_1
t44817_1 t44825_1 t50617_1 t52674_11`) were deleted and re-pushed at their
existing commits to force a fresh `push` event (their stub workflows call
`patch-ci.yml@main`, so they always run whatever is on `main` now,
without needing any change on the postgres side). All nine
`build-and-test` / `publish` / `report` runs came back green, and the
`publish` job's own `RUN ... grep -qxF ...` step - which fails the image
build outright if the sed didn't take - passed on all nine, spanning
pg10 through pg20.

### Final green run: all 9 images, all 5 cases, PG10 through PG20

Run 30211181479 (subject `re-trigger image contract check [contract]`),
matrix job first, then 9 contract legs. Every leg's `outcome` for every
case read directly from that run's own log (not the jobs API - see above):

| image | pg major | case1 bare-run | case2 env vars | case3 initdb.d | case4 persistence | case5 pg_trgm |
|---|---|---|---|---|---|---|
| t37759 | 10 | success | success | success | success | success |
| t37764 | 11 | success | success | success | success | success |
| t38263 | 11 | success | success | success | success | success |
| t40413 | 14 | success | success | success | success | success |
| t42603 | 14 | success | success | success | success | success |
| t44817 | 15 | success | success | success | success | success |
| t50617 | 18 | success | success | success | success | success |
| t44825 | 20 | success | success | success | success | success |
| t52674 | 19 | success | success | success | success | success |

No per-major differences turned up anywhere in the actual contract
behavior - every case that matters to a reviewer (bare run against the
trust default, the official env-var contract, init hooks, volume
persistence, a contrib extension) works identically from a 2017-era build
(pg10) through the current dev major (pg20). The two real bugs the test
did find (readiness-check race, loopback-only listen address) were both
pipeline/test bugs with a single, version-independent cause - not
something that varies by PostgreSQL major.
