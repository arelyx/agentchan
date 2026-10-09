#!/usr/bin/env bash
# Deploy one variant to the cloud box behind Caddy.
#   deploy/deploy_variant.sh <variant> <src_dir> <local_port> <public_port>
# Public URL: https://agentchan.arelyx.xyz:<public_port>  (Caddy terminates TLS, proxies to 127.0.0.1:<local_port>)
set -euo pipefail
V=$1 SRC=$2 LPORT=$3 PPORT=$4
HOST=${DEPLOY_HOST:-cloud}
DOMAIN=${DOMAIN:-agentchan.arelyx.xyz}
PUB="https://$DOMAIN"; [ "$PPORT" != 443 ] && PUB="$PUB:$PPORT"
DEST="agentchan/variants/$V"
ssh "$HOST" mkdir -p "$DEST"
rsync -az --delete --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' "$SRC/" "$HOST:$DEST/"
ssh "$HOST" bash -s <<REMOTE
set -euo pipefail
cd ~/$DEST
[ -f ../$V.secrets.env ] || printf 'PEPPER=%s\nIP_SALT=%s\nADMIN_TOKEN=%s\n' \$(openssl rand -hex 24) \$(openssl rand -hex 24) \$(openssl rand -hex 24) > ../$V.secrets.env
. ../$V.secrets.env
SVC=\$(docker compose -f docker-compose.yml config --services | grep -v '^db\$' | head -1)
cat > cloud.override.yml <<YML
services:
  \$SVC:
    environment:
      PEPPER: "\$PEPPER"
      TRIP_PEPPER: "\$PEPPER"
      IP_SALT: "\$IP_SALT"
      ADMIN_TOKEN: "\$ADMIN_TOKEN"
      PUBLIC_URL: "$PUB"
      TRUST_PROXY: "1"
      TRUST_XFF: "1"
      EVAL_ENDPOINTS: "1"
    ports: !override
      - "127.0.0.1:$LPORT:8000"
YML
docker compose -p mb-$V -f docker-compose.yml -f cloud.override.yml up -d --build 2>&1 | tail -3
REMOTE
echo "deployed $V -> $PUB"
