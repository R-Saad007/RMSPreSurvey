#!/bin/sh
# Deploys one image tag on the VM, once it runs on containers. GitLab's manual
# "deploy" job copies this and compose.yaml to /opt/rms and runs it:
#
#   RMS_IMAGE=registry.gitlab.com/<group>/<project> sh /opt/rms/deploy.sh <tag>
#
# Order matters: back up the database, stop the bot, start the portal (it
# migrates the database), then the bot, then the smoke check.
set -eu

TAG="$1"
cd /opt/rms
export RMS_TAG="$TAG"

stamp=$(date +%Y%m%d-%H%M%S)
mkdir -p /opt/rms-backups
if [ -f rms-data/rms_bot.sqlite3 ]; then
    python3 - "rms-data/rms_bot.sqlite3" "/opt/rms-backups/rms_bot-$stamp-pre-$TAG.sqlite3" <<'EOF'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
EOF
    chmod 600 "/opt/rms-backups/rms_bot-$stamp-pre-$TAG.sqlite3"
fi

docker compose pull
docker compose stop bot || true
docker compose up -d --wait portal
docker compose up -d bot
docker compose exec -T portal python -m tools.smoke_portal
echo "deployed $TAG"
