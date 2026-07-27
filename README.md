# pg-build-env

Build environments for compiling and testing PostgreSQL across its 2017-2026
history. One image per Debian era, tagged per PG major it serves.

## Tags

| tag  | runtime tag   | base image        |
|------|---------------|--------------------|
| pg9  | pg9-runtime   | debian/eol:stretch |
| pg10 | pg10-runtime  | debian/eol:stretch |
| pg11 | pg11-runtime  | debian/eol:stretch |
| pg12 | pg12-runtime  | debian/eol:buster  |
| pg13 | pg13-runtime  | debian/eol:buster  |
| pg14 | pg14-runtime  | debian:bullseye    |
| pg15 | pg15-runtime  | debian:bullseye    |
| pg16 | pg16-runtime  | debian:bookworm    |
| pg17 | pg17-runtime  | debian:bookworm    |
| pg18 | pg18-runtime  | debian:trixie      |
| pg19 | pg19-runtime  | debian:trixie      |
| pg20 | pg20-runtime  | debian:trixie      |

Source of truth for this mapping is `eras.yml`. The plain tag is for
building and testing a patch; the `-runtime` tag is a much smaller image
carrying just what's needed to run a compiled postgres (the official
`docker-entrypoint.sh` contract, a `postgres` user at uid 999, `gosu`), for
publishing an already-built patch.

## What's in the image

- Full PostgreSQL build dependency set (readline, zlib, openssl, icu, libxml2,
  ldap, pam, krb5, selinux, uuid, perl).
- autoconf 2.69, built from source regardless of what the base distro ships -
  postgres's `configure.ac` refuses newer autoconf outright.
- Perl TAP test modules (`IPC::Run`, `Test::Simple`) for `check-world`.
- `en_US.UTF-8` locale generated.
- A non-root `ci` user and an `as-ci` helper. `initdb` and `postgres` refuse
  to run as root, so any build/test step that runs the server goes through
  `as-ci`.
- A ccache pre-warmed by compiling the family's reference commits during the
  image build itself - `ccache -s` on a fresh container reports a populated
  cache, not an empty one.

## Using an image

```
docker run --rm -it ghcr.io/hackorum-dev/pg-build-env:pg20
./configure --enable-debug --enable-cassert --enable-tap-tests
as-ci make -j$(nproc)
as-ci make check-world
```

`as-ci` runs its argument as the `ci` user - needed for anything that starts
a postgres instance, since `initdb`/`postgres`/`pg_ctl` refuse to run as
root.

## Patch images (`postgres-patch`)

`patch-ci.yml` publishes one image per patch thread,
`ghcr.io/hackorum-dev/postgres-patch:t<topic>`, built from that thread's
own compiled postgres on top of the matching `-runtime` image above. Point
a reviewer at one and they get a working server without building
anything.

Verified end to end (`.github/workflows/image-contract.yml`, against a
sample of published images spanning PG10 through PG20):

- `docker run -p 5432:5432 <image>`, no environment variables at all, then
  connect from the host with `psql` - the image sets
  `POSTGRES_HOST_AUTH_METHOD=trust` by default, so this just works.
- `POSTGRES_PASSWORD` / `POSTGRES_USER` / `POSTGRES_DB` all set to
  non-default values - the official postgres image's env-var contract.
- `.sql` files under `/docker-entrypoint-initdb.d/` run once, on first
  boot.
- Data written to a named volume survives `docker stop && docker rm`, and
  a fresh container started on the same volume picks it back up.
- A contrib extension (`pg_trgm`) installs with `CREATE EXTENSION` and
  works in a query.

All five held identically from a 2017-era build (PG10) through the
current dev major (PG20) - no version-specific carve-outs needed. See
`docs/era-notes.md` for what the check found on the way to green (two
real bugs, both fixed) and exactly what "verified" covers.

## Layout

- `eras.yml` - PG major -> build image family. Single source of truth.
- `images/` - era build and runtime images, pushed to GHCR.
- `.github/workflows/build-era-images.yml` - builds them. A push to `main`
  only starts a build if the tip commit's subject line carries a
  `[build <family>]` marker (`[build all]` for every enabled family); a push
  with no marker runs the workflow but builds nothing. Can also be run
  manually for one family, or every enabled family if none is given.
- `.github/workflows/patch-ci.yml` - the reusable workflow that patch
  branches in [hackorum-dev/postgres](https://github.com/hackorum-dev/postgres)
  call into, and that publishes the `postgres-patch` images above.
- `.github/workflows/image-contract.yml` - verifies the `postgres-patch`
  contract against a fixed sample of already-published images. Same
  marker-on-commit-subject trigger as `build-era-images.yml`, using
  `[contract]`.
- `docs/era-notes.md` - implementation history and per-family gotchas.
