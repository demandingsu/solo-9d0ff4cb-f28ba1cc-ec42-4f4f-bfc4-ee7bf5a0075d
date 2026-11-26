#!/bin/sh
set -e

DB_FILE="${DB_PATH:-/data/referee.db}"
DB_DIR="$(dirname "$DB_FILE")"
mkdir -p "$DB_DIR"

echo "[entrypoint] initializing SQLite at ${DB_PATH:-/data/referee.db}"
# 官方 python 镜像同时提供 python / python3；用 python3 也便于在普通 Linux 上试跑。
# bootstrap() 直接读 DB_PATH 环境变量。
python3 -c "from app.db import bootstrap; bootstrap()"

exec "$@"
