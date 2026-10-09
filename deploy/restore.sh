#!/usr/bin/env bash
# Restore an agentchan database from a backup.sh dump, ON the VPS.
#   deploy/restore.sh <file.dump> [project]     (project defaults to mb-agentchan)
# Stops the app, renames the current database to agentchan_before_<timestamp> (kept, not dropped),
# restores the dump into a fresh agentchan database and starts the app again.
# Try it against staging first: deploy/restore.sh <file.dump> mb-agentchan-staging
set -euo pipefail
DUMP=${1:?usage: restore.sh <file.dump> [project]}
PROJECT=${2:-mb-agentchan}
DB_USER=${DB_USER:-agentchan} DB_NAME=${DB_NAME:-agentchan}
[ -s "$DUMP" ] || { echo "no such dump: $DUMP" >&2; exit 1; }
cid() { docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=$1"; }
DB=$(cid db) APP=$(cid app)
[ -n "$DB" ] && [ -n "$APP" ] || { echo "containers for project $PROJECT not found" >&2; exit 1; }
docker exec -i "$DB" pg_restore -l < "$DUMP" > /dev/null  # readable?

OLD="${DB_NAME}_before_$(date -u +%Y%m%d%H%M%S)"
echo "stopping app; current database will be kept as $OLD"
docker stop "$APP" > /dev/null
psql() { docker exec -i "$DB" psql -v ON_ERROR_STOP=1 -q -U "$DB_USER" -d postgres "$@"; }
psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid()" > /dev/null
psql -c "ALTER DATABASE \"$DB_NAME\" RENAME TO \"$OLD\""
psql -c "CREATE DATABASE \"$DB_NAME\" OWNER \"$DB_USER\""
docker exec -i "$DB" pg_restore -U "$DB_USER" -d "$DB_NAME" --no-owner --exit-on-error < "$DUMP"
docker start "$APP" > /dev/null
echo "restored $DUMP into $PROJECT; posts: $(docker exec "$DB" psql -tA -U "$DB_USER" -d "$DB_NAME" -c 'SELECT count(*) FROM posts')"
echo "when satisfied: docker exec $DB dropdb -U $DB_USER $OLD"
