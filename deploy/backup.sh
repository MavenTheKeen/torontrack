#!/bin/sh
# Daily compressed dump of the transit database; keeps BACKUP_KEEP_DAYS days of dumps.
# Restore: pg_restore --clean --if-exists -d transit /backups/transit-<stamp>.dump
# zstd makes dumps about 45% smaller than the default gzip. Raw TTC snapshots (kept only
# 3 days, for replay) and the issues logged against them are left out, which halves the
# dump again; their tables are restored empty and refill from the next poll.
set -u
# Dumps hold every table, including the site login's password hash, so only root (the
# owner) may read them, and existing dumps from before this rule are tightened too.
umask 077
chmod 700 /backups
chmod 600 /backups/transit-*.dump 2>/dev/null
PGPASSWORD=$(cat /run/secrets/postgres_password)
export PGPASSWORD
while true; do
  stamp=$(date -u +%Y%m%dT%H%MZ)
  partial="/backups/transit-$stamp.dump.partial"
  if pg_dump --format=custom --compress=zstd:9       --exclude-table-data=raw.ttc_realtime_snapshots       --exclude-table-data=ops.ttc_record_issues --file="$partial"; then
    mv "$partial" "/backups/transit-$stamp.dump"
    echo "backup written: transit-$stamp.dump"
  else
    rm -f "$partial"
    echo "backup failed" >&2
  fi
  find /backups -name 'transit-*.dump' -mtime +"${BACKUP_KEEP_DAYS:-7}" -delete
  sleep 86400
done
