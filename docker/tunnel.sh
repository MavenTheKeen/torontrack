#!/bin/sh
# Forward port 5432 of this container to the collector server's PostgreSQL, reconnecting
# whenever the connection drops. The key and known_hosts are mounted read-only; ssh needs
# a private copy of the key with strict permissions.
set -u
install -m 600 /keys/id /tmp/id
install -m 600 /keys/known_hosts /tmp/known_hosts
while :; do
  ssh -N -i /tmp/id -p "$COLLECTOR_SSH_PORT" \
    -o UserKnownHostsFile=/tmp/known_hosts -o StrictHostKeyChecking=yes \
    -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -L 0.0.0.0:5432:127.0.0.1:5432 "$COLLECTOR_SSH"
  echo "tunnel closed; reconnecting in 10 s"
  sleep 10
done
