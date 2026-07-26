ARG BASE_IMAGE=debian:trixie
FROM ${BASE_IMAGE}

ARG EOL=false
ENV DEBIAN_FRONTEND=noninteractive

# EOL Debian releases live on archive.debian.org and their Release files are
# long expired, so validity checking has to go.
RUN if [ "$EOL" = "true" ]; then \
      printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99no-check-valid; \
    fi

# shared library packages a compiled postgres needs at runtime. names vary
# per Debian release (openssl, ldap and readline all rename their runtime
# package across releases), so this list is supplied per family rather than
# hardcoded here.
ARG RUNTIME_PACKAGES=""
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates locales gosu \
      ${RUNTIME_PACKAGES} \
    && rm -rf /var/lib/apt/lists/*

RUN sed -i 's/^# *\(en_US.UTF-8\)/\1/' /etc/locale.gen && locale-gen
ENV LANG=en_US.utf8

# same uid/gid the official postgres image uses, so a volume built by one
# image keeps working if mounted into the other
RUN groupadd -r postgres --gid=999 \
    && useradd -r -g postgres --uid=999 --home-dir=/var/lib/postgresql --shell=/bin/bash postgres \
    && mkdir -p /var/lib/postgresql \
    && chown -R postgres:postgres /var/lib/postgresql

RUN mkdir -p /docker-entrypoint-initdb.d

ENV PGDATA=/var/lib/postgresql/data
# 777 here gets narrowed to 700 by initdb itself once it runs
RUN mkdir -p "$PGDATA" && chown -R postgres:postgres "$PGDATA" && chmod 777 "$PGDATA"
VOLUME /var/lib/postgresql/data

RUN mkdir -p /var/run/postgresql \
    && chown -R postgres:postgres /var/run/postgresql \
    && chmod 2777 /var/run/postgresql

COPY --chmod=755 images/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# upstream refuses to start without POSTGRES_PASSWORD; this image starts on
# a bare `docker run` instead. the entrypoint script itself is untouched -
# it still prints its own loud warning when trust is in effect. setting
# POSTGRES_PASSWORD restores upstream's default behaviour.
ENV POSTGRES_HOST_AUTH_METHOD=trust

# nothing here can start an actual server yet - no compiled postgres exists
# in this image, that gets added by whatever copies the build output in.
# what can be checked at build time gets checked here instead.
RUN set -eux; \
    command -v gosu; \
    gosu nobody true; \
    [ "$(id -u postgres)" = '999' ]; \
    [ "$(getent passwd 999 | cut -d: -f1)" = 'postgres' ]; \
    bash -n /usr/local/bin/docker-entrypoint.sh; \
    [ -x /usr/local/bin/docker-entrypoint.sh ]; \
    locale -a | grep -qi '^en_US.utf8$'

ENTRYPOINT ["docker-entrypoint.sh"]
EXPOSE 5432
CMD ["postgres"]
