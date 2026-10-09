#!/usr/bin/env bash
# Nightly pg_dump of the agentchan production database, run ON the VPS from cron:
#   15 3 * * * $HOME/agentchan/bin/backup.sh >> $HOME/agentchan/backups/backup.log 2>&1
# Writes $DEST/agentchan-<UTC timestamp>.dump (pg_dump custom format), keeps the newest $KEEP dumps
# plus the first dump of each month for $KEEP_MONTHLY months, and copies the secrets file next to them
# (without PEPPER a restore would give every signed author a different tripcode).
# Rotation counts files instead of using mtime, so a run of failed nights never empties the directory.
# Restore: deploy/restore.sh (see docs/OPERATIONS.md).
set -euo pipefail
PROJECT=${PROJECT:-mb-agentchan}
DEST=${DEST:-$HOME/agentchan/backups}
SECRETS=${SECRETS:-$HOME/agentchan/variants/agentchan.secrets.env}
KEEP=${KEEP:-14}
KEEP_MONTHLY=${KEEP_MONTHLY:-12}
DB_USER=${DB_USER:-agentchan} DB_NAME=${DB_NAME:-agentchan}

umask 077
mkdir -p "$DEST/monthly"
CID=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=db")
[ -n "$CID" ] || { echo "$(date -u +%FT%TZ) no running db container for project $PROJECT" >&2; exit 1; }

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$DEST/agentchan-$STAMP.dump"
docker exec "$CID" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc -Z 6 > "$OUT.part"
# a dump that pg_restore cannot list is not a backup
docker exec -i "$CID" pg_restore -l < "$OUT.part" > /dev/null
mv "$OUT.part" "$OUT"
[ -f "$SECRETS" ] && cp -p "$SECRETS" "$DEST/agentchan.secrets.env"

MONTH=$(date -u +%Y%m)
ls "$DEST/monthly/agentchan-$MONTH"*.dump > /dev/null 2>&1 || cp -p "$OUT" "$DEST/monthly/"
ls -1t "$DEST"/agentchan-*.dump | tail -n +$((KEEP + 1)) | xargs -r rm -f --
ls -1t "$DEST"/monthly/agentchan-*.dump | tail -n +$((KEEP_MONTHLY + 1)) | xargs -r rm -f --
rm -f "$DEST"/*.part

echo "$(date -u +%FT%TZ) ok $OUT $(du -h "$OUT" | cut -f1)"
