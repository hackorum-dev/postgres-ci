ARG BASE_IMAGE=debian:trixie
FROM ${BASE_IMAGE}

ARG EOL=false
ENV DEBIAN_FRONTEND=noninteractive

# EOL Debian releases live on archive.debian.org and their Release files are
# long expired, so validity checking has to go.
RUN if [ "$EOL" = "true" ]; then \
      printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99no-check-valid; \
    fi

ARG EXTRA_PACKAGES=""
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ccache git ca-certificates curl xz-utils sudo \
      bison flex perl pkg-config gettext tzdata locales \
      python3 libreadline-dev zlib1g-dev libssl-dev libicu-dev \
      libxml2-dev libxslt1-dev libldap2-dev libpam0g-dev \
      libkrb5-dev libselinux1-dev uuid-dev libperl-dev \
      libipc-run-perl libtest-simple-perl \
      ${EXTRA_PACKAGES} \
    && rm -rf /var/lib/apt/lists/*

RUN sed -i 's/^# *\(en_US.UTF-8\)/\1/' /etc/locale.gen && locale-gen
ENV LANG=en_US.utf8

# postgres's configure.ac m4_fatals unless autoconf reports exactly 2.69, and
# distro versions vary, so build 2.69 from source everywhere - cheap, one
# code path, harmless on distros that already ship it.
RUN curl -sSL https://ftp.gnu.org/gnu/autoconf/autoconf-2.69.tar.xz -o /tmp/ac.tar.xz \
    && tar -xJf /tmp/ac.tar.xz -C /tmp \
    && cd /tmp/autoconf-2.69 \
    && ./configure --prefix=/usr/local >/dev/null \
    && make >/dev/null && make install >/dev/null \
    && cd / && rm -rf /tmp/autoconf-2.69 /tmp/ac.tar.xz \
    && autoconf --version | head -1

# initdb, postgres and pg_ctl all refuse to run as root, so every build and
# test step runs through as-ci.
RUN useradd --create-home --uid 1000 ci \
    && mkdir -p /ccache && chown ci:ci /ccache
RUN printf '#!/bin/sh\nexec sudo -u ci -H CCACHE_DIR="$CCACHE_DIR" CCACHE_MAXSIZE="$CCACHE_MAXSIZE" PERL_USE_UNSAFE_INC="$PERL_USE_UNSAFE_INC" PATH="$PATH" -- "$@"\n' \
      > /usr/local/bin/as-ci && chmod 755 /usr/local/bin/as-ci

# perl 5.26+ dropped "." from @INC (CVE-2016-1238). Old postgres trees (9.6's
# pg_rewind TAP tests) do a bare "use RewindTest;" and rely on it being there -
# modern trees use FindBin instead. PERL_USE_UNSAFE_INC restores the old
# behavior. Applies to every era, not just the affected ones: modern trees
# don't care, and one code path beats a conditional.
ENV CCACHE_DIR=/ccache \
    CCACHE_MAXSIZE=3G \
    PERL_USE_UNSAFE_INC=1 \
    PATH=/usr/lib/ccache:/usr/local/bin:/usr/bin:/bin

# sudo resets env and PATH by default (env_reset, secure_path), so as-ci
# passes CCACHE_DIR/CCACHE_MAXSIZE/PERL_USE_UNSAFE_INC/PATH through explicitly
# rather than relying on them surviving the wrapper.
RUN as-ci sh -c 'test "$CCACHE_DIR" = /ccache && test "$PERL_USE_UNSAFE_INC" = 1 && test "$(id -un)" = ci && command -v gcc | grep -q /usr/lib/ccache'

# Warm the cache by building every reference commit this family serves. Also
# doubles as the era's own smoke test: if a tree can't build here, this image
# build fails instead of failing later in every build that uses this image.
ARG REFERENCE_COMMITS=""
ARG POSTGRES_REPO=https://github.com/hackorum-dev/postgres.git
RUN set -eux; \
    for sha in $REFERENCE_COMMITS; do \
      rm -rf /tmp/pg; mkdir -p /tmp/pg; cd /tmp/pg; \
      git init -q .; \
      git remote add origin "$POSTGRES_REPO"; \
      git fetch -q --depth 1 origin "$sha"; \
      git checkout -q FETCH_HEAD; \
      chown -R ci:ci /tmp/pg; \
      as-ci autoconf -f; \
      as-ci ./configure --enable-debug --enable-cassert --enable-tap-tests \
        --prefix=/tmp/pgi >/dev/null; \
      as-ci make -j"$(nproc)" -s; \
      as-ci make -j"$(nproc)" -s -C contrib; \
      PG_TEST_EXTRA= as-ci make -s check-world -Otarget -j"$(nproc)" PROVE_FLAGS=--timer; \
      cd /; rm -rf /tmp/pg /tmp/pgi; \
    done; \
    ccache -s

USER ci
WORKDIR /home/ci
