#!/usr/bin/env bash
set -euo pipefail

echo "=== [1/8] Updating packages and installing dependencies ==="
dnf update -y
dnf install -y docker python3.11 python3.11-pip git sqlite tar awscli openssl

echo "=== [2/8] Enabling and starting Docker; checking cgroup v2 ==="
systemctl enable --now docker
CG_TYPE=$(stat -fc %T /sys/fs/cgroup)
echo "Cgroup filesystem type: $CG_TYPE"
if [ "$CG_TYPE" != "cgroup2fs" ]; then
    echo "WARNING: Expected cgroup2fs, got $CG_TYPE"
fi

echo "=== [3/8] Creating alloc system user and directories ==="
if ! id -u alloc >/dev/null 2>&1; then
    useradd -r -s /sbin/nologin -d /var/lib/alloc alloc
fi

mkdir -p /opt/alloc/releases /etc/alloc /var/lib/alloc /usr/local/bin
chown -R alloc:alloc /opt/alloc /etc/alloc /var/lib/alloc

echo "=== [4/8] Generating /etc/alloc/env ==="
if [ ! -f /etc/alloc/env ]; then
    SECRET_KEY=$(openssl rand -hex 32)
    cat > /etc/alloc/env <<EOF
SECRET_KEY=${SECRET_KEY}
DOCKER_HOST=tcp://127.0.0.1:2375
DB_PATH=/var/lib/alloc/alloc.db
MAX_TENANTS=10
MAX_DURATION_S=600
MAX_REPEATS=10
MAX_CONTAINER_CPU=4.0
MIN_CONTAINER_CPU=0.05
MAX_CONTAINER_MEM_MB=16384.0
MIN_CONTAINER_MEM_MB=32.0
EOF
    chmod 600 /etc/alloc/env
    chown alloc:alloc /etc/alloc/env
fi

echo "=== [5/8] Starting restricted docker-socket-proxy container ==="
if docker ps -a --format '{{.Names}}' | grep -q '^dockerproxy$'; then
    docker rm -f dockerproxy
fi

docker run -d \
    --name dockerproxy \
    --restart always \
    -e CONTAINERS=1 \
    -e POST=1 \
    -e DELETE=1 \
    -e NETWORKS=1 \
    -e IMAGES=0 \
    -e EXEC=0 \
    -e VOLUMES=0 \
    -e INFO=1 \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    -p 127.0.0.1:2375:2375 \
    tecnativa/docker-socket-proxy

echo "=== [6/8] Installing /usr/local/bin/alloc-install script ==="
cat > /usr/local/bin/alloc-install <<'INSTALL_EOF'
#!/usr/bin/env bash
set -euo pipefail

# Read BUCKET from instance metadata or resources tag if available, or fall back to param
BUCKET="${BUCKET:-}"
if [ -z "$BUCKET" ]; then
    # Try reading from resources.env if placed, or derive from AWS caller identity
    if [ -f /etc/alloc/bucket_name ]; then
        BUCKET=$(cat /etc/alloc/bucket_name)
    else
        ACCT=$(aws sts get-caller-identity --query Account --output text)
        REGION=$(curl -s http://169.254.169.254/latest/meta-data/placement/region || echo "ap-south-1")
        BUCKET="alloc-platform-${ACCT}-${REGION}"
    fi
fi

TS=$(date +%Y%m%d_%H%M%S)
REL_DIR="/opt/alloc/releases/${TS}"
mkdir -p "$REL_DIR"

echo "Downloading release from s3://${BUCKET}/releases/latest.tgz..."
aws s3 cp "s3://${BUCKET}/releases/latest.tgz" /tmp/latest.tgz
tar -xzf /tmp/latest.tgz -C "$REL_DIR"
rm -f /tmp/latest.tgz

echo "Building fixed tenant worker image locally as root..."
docker build -t alloc-worker:latest "$REL_DIR/src/alloc_platform/workload/worker"

echo "Creating application virtual environment..."
python3.11 -m venv "$REL_DIR/.venv"
"$REL_DIR/.venv/bin/pip" install --upgrade pip
"$REL_DIR/.venv/bin/pip" install "$REL_DIR"

chown -R alloc:alloc "$REL_DIR"

echo "Atomically repointing /opt/alloc/current..."
ln -sfn "$REL_DIR" /opt/alloc/current

echo "Restarting alloc-api service..."
systemctl restart alloc-api.service

echo "Pruning older releases (retaining last 3)..."
cd /opt/alloc/releases
ls -dt */ | tail -n +4 | xargs -r rm -rf

echo "Deployment complete."
INSTALL_EOF
chmod +x /usr/local/bin/alloc-install

echo "=== [7/8] Installing systemd unit alloc-api.service ==="
cat > /etc/systemd/system/alloc-api.service <<EOF
[Unit]
Description=Adaptive Multi-Tenant Cloud Resource Allocation Platform API
After=network.target docker.service

[Service]
Type=simple
User=alloc
Group=alloc
EnvironmentFile=/etc/alloc/env
WorkingDirectory=/opt/alloc/current
ExecStart=/opt/alloc/current/.venv/bin/uvicorn alloc_platform.api.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5s
ProtectSystem=full
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable alloc-api.service

echo "=== [8/8] Installing nightly database backup systemd timer ==="
cat > /usr/local/bin/alloc-backup <<'BACKUP_EOF'
#!/usr/bin/env bash
set -euo pipefail
if [ -f /var/lib/alloc/alloc.db ]; then
    TS=$(date +%Y%m%d_%H%M%S)
    BACKUP_TMP="/tmp/alloc_backup_${TS}.db"
    sqlite3 /var/lib/alloc/alloc.db ".backup '${BACKUP_TMP}'"
    ACCT=$(aws sts get-caller-identity --query Account --output text)
    REGION=$(curl -s http://169.254.169.254/latest/meta-data/placement/region || echo "ap-south-1")
    BUCKET="alloc-platform-${ACCT}-${REGION}"
    aws s3 cp "${BACKUP_TMP}" "s3://${BUCKET}/backups/alloc_${TS}.db"
    rm -f "${BACKUP_TMP}"
fi
BACKUP_EOF
chmod +x /usr/local/bin/alloc-backup

cat > /etc/systemd/system/alloc-backup.service <<EOF
[Unit]
Description=Nightly backup of Alloc Platform SQLite database to S3

[Service]
Type=oneshot
ExecStart=/usr/local/bin/alloc-backup
EOF

cat > /etc/systemd/system/alloc-backup.timer <<EOF
[Unit]
Description=Daily SQLite backup timer

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now alloc-backup.timer

echo "=== Alloc Platform User-Data bootstrap finished successfully ==="
