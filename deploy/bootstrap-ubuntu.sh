#!/bin/sh
set -eu

usage() {
  echo "Usage: sudo sh bootstrap-ubuntu.sh DEPLOY_USER SSH_PUBLIC_KEY" >&2
  echo "Run on a fresh Ubuntu VM after mounting the persistent disk at /srv/sql-trainer." >&2
  exit 2
}

[ "$(id -u)" -eq 0 ] || { echo "Run this script with sudo/root." >&2; exit 2; }
[ "$#" -eq 2 ] || usage
[ -r /etc/os-release ] || { echo "Cannot identify the operating system." >&2; exit 2; }
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 24.04 ] || {
  echo "This bootstrap script currently supports Ubuntu 24.04 only." >&2
  exit 2
}

deploy_user=$1
deploy_key=$2
case "$deploy_user" in
  ''|*[!a-zA-Z0-9_-]*) echo "Invalid Linux username." >&2; exit 2 ;;
esac
case "$deploy_key" in
  ssh-ed25519\ *|ecdsa-sha2-nistp256\ *|ssh-rsa\ *) ;;
  *) echo "Expected an OpenSSH public key (ed25519 recommended)." >&2; exit 2 ;;
esac

mountpoint -q /srv/sql-trainer || {
  echo "Persistent disk is not mounted at /srv/sql-trainer; refusing to create data on the boot disk." >&2
  exit 1
}
disk_target=$(findmnt -n -o TARGET --target /srv/sql-trainer)
disk_uuid=$(findmnt -n -o UUID --target /srv/sql-trainer)
disk_fstype=$(findmnt -n -o FSTYPE --target /srv/sql-trainer)
[ "$disk_target" = /srv/sql-trainer ] && [ -n "$disk_uuid" ] || {
  echo "Expected a UUID-backed filesystem mounted exactly at /srv/sql-trainer." >&2
  exit 1
}
case "$disk_fstype" in
  ext4) fsck_pass=2 ;;
  xfs) fsck_pass=0 ;;
  *) echo "Unsupported data disk filesystem: $disk_fstype (expected ext4 or xfs)." >&2; exit 1 ;;
esac

fstab_status=0
awk -v source="UUID=$disk_uuid" -v fstype="$disk_fstype" '
  $1 !~ /^#/ && $2 == "/srv/sql-trainer" {
    found=1
    if ($1 == source && $3 == fstype && $4 !~ /(^|,)noauto(,|$)/) valid=1
  }
  END { if (!found) exit 2; if (valid) exit 0; exit 1 }
' /etc/fstab || fstab_status=$?
if [ "$fstab_status" -eq 2 ]; then
  cp -an /etc/fstab /etc/fstab.before-sql-trainer
  printf '\nUUID=%s /srv/sql-trainer %s defaults,nofail,x-systemd.device-timeout=30s 0 %s\n' \
    "$disk_uuid" "$disk_fstype" "$fsck_pass" >> /etc/fstab
elif [ "$fstab_status" -ne 0 ]; then
  echo "/etc/fstab already mounts another device at /srv/sql-trainer; inspect it before continuing." >&2
  exit 1
fi

docker_dropin=/etc/systemd/system/docker.service.d/10-sql-trainer-storage.conf
install -d -m 0755 /etc/systemd/system/docker.service.d
if [ ! -e "$docker_dropin" ]; then
  cat > "$docker_dropin" <<'EOF'
[Unit]
RequiresMountsFor=/srv/sql-trainer

[Service]
ExecStartPre=/usr/bin/mountpoint -q /srv/sql-trainer
EOF
elif ! grep -Fq 'ExecStartPre=/usr/bin/mountpoint -q /srv/sql-trainer' "$docker_dropin" || \
     ! grep -Fq 'RequiresMountsFor=/srv/sql-trainer' "$docker_dropin"; then
  echo "Existing Docker storage drop-in differs from the expected safety guard: $docker_dropin" >&2
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates docker.io docker-compose-v2 git make python3
systemctl daemon-reload
systemctl enable docker
systemctl restart docker
docker compose version >/dev/null

if ! id "$deploy_user" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$deploy_user"
fi
usermod -aG docker "$deploy_user"
install -d -o "$deploy_user" -g "$deploy_user" -m 0700 "/home/$deploy_user/.ssh"
touch "/home/$deploy_user/.ssh/authorized_keys"
grep -Fqx "$deploy_key" "/home/$deploy_user/.ssh/authorized_keys" || \
  printf '%s\n' "$deploy_key" >> "/home/$deploy_user/.ssh/authorized_keys"
chown "$deploy_user:$deploy_user" "/home/$deploy_user/.ssh/authorized_keys"
chmod 0600 "/home/$deploy_user/.ssh/authorized_keys"

install -d -o "$deploy_user" -g docker -m 0750 /opt/sql-trainer
install -d -o 10001 -g 10001 -m 0700 /srv/sql-trainer/data /srv/sql-trainer/backups
install -d -o root -g root -m 0700 /srv/sql-trainer/caddy-data /srv/sql-trainer/caddy-config

env_file=/opt/sql-trainer/.env
if [ ! -e "$env_file" ]; then
  [ -t 0 ] || { echo "Run once from an interactive terminal to enter the learning DSN." >&2; exit 2; }
  printf 'Learning database DSN (postgresql://..., password URL-encoded): ' >&2
  trap 'stty echo 2>/dev/null || true' EXIT HUP INT TERM
  stty -echo
  IFS= read -r learning_dsn
  stty echo
  trap - EXIT HUP INT TERM
  printf '\n' >&2
  case "$learning_dsn" in
    postgresql://*|postgres://*) ;;
    *) echo "DSN must begin with postgresql:// or postgres://" >&2; exit 2 ;;
  esac
  case "$learning_dsn" in
    *[![:print:]]*) echo "DSN contains unsupported control characters." >&2; exit 2 ;;
  esac
  admin_token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
  umask 077
  {
    printf 'CABINET_DB_PATH=/data/cabinet.sqlite3\n'
    printf 'CABINET_MANIFEST=/data/manifest.json\n'
    printf 'CABINET_LEARNING_DSN=%s\n' "$learning_dsn"
    printf 'CABINET_ADMIN_TOKEN=%s\n' "$admin_token"
    printf 'CABINET_ADMIN_NAME=Преподаватель\n'
  } > "$env_file"
  unset learning_dsn admin_token
  chown "$deploy_user:docker" "$env_file"
  chmod 0600 "$env_file"
else
  echo "Keeping existing /opt/sql-trainer/.env unchanged."
  chown "$deploy_user:docker" "$env_file"
  chmod 0600 "$env_file"
fi

echo "Host prepared. The deploy user must log in again for Docker group membership to apply."
echo "Persistent disk fstab entry and Docker mount guard are configured; Docker was restarted so the guard is active."
echo "Clone the Git repository into /opt/sql-trainer/app, then run make prod-up from that directory."
echo "For later updates, run make update from /opt/sql-trainer/app."
