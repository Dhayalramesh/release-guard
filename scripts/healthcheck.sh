#!/usr/bin/env bash
# Usage: scripts/healthcheck.sh <url> [attempts] [sleep_seconds]
# Polls a health endpoint; exits 0 when healthy, 1 if it never becomes healthy.
url="${1:?usage: healthcheck.sh <url> [attempts] [sleep]}"
attempts="${2:-10}"
pause="${3:-2}"
for i in $(seq 1 "$attempts"); do
  if curl -fsS "$url" > /dev/null; then
    echo "healthy after $i attempt(s)"
    exit 0
  fi
  echo "attempt $i/$attempts failed; retrying in ${pause}s"
  sleep "$pause"
done
echo "service never became healthy" >&2
exit 1
