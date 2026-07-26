#!/usr/bin/env bash
# wait for a container's postgres to accept TCP connections from the host,
# bounded so a broken image fails the job instead of hanging it forever.
#
# checks readiness through the published host port, not "docker exec ...
# pg_isready". the official entrypoint starts a throwaway, local-socket-only
# postgres first to run init scripts, then stops it and starts the real,
# TCP-listening one. a docker-exec check happily reports the throwaway
# instance ready over the local socket, and a host connection made right
# after that catches the gap between the two servers ("server closed the
# connection unexpectedly") - not a real failure, just the wrong instance.
# polling the host port instead only succeeds once the real server is
# reachable the same way a caller would reach it.
#
# usage: wait_pg_ready.sh <container-for-logs-on-failure> <host-port> [user] [timeout seconds]
set -uo pipefail

name="$1"
port="$2"
user="${3:-postgres}"
timeout="${4:-60}"

for i in $(seq 1 "$timeout"); do
  if pg_isready -h 127.0.0.1 -p "$port" -U "$user" >/dev/null 2>&1; then
    echo "$name ready on port $port after ${i}s"
    exit 0
  fi
  sleep 1
done

echo "$name did not become ready on port $port within ${timeout}s"
docker logs "$name"
exit 1
