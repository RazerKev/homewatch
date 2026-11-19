#!/usr/bin/env bash
# Nightly HomeWatch database backup to S3. Runs on the AWS server from
# homewatch-backup.timer; settings come from /opt/homewatch/.env:
#   BACKUP_BUCKET   the S3 bucket, kept in .env so its name stays out of git
#   NTFY_URL        optional: where to push an alert if the backup fails
# The server's IAM role may only add files under nightly/ and can't delete
# anything, so a break-in can't erase old backups. An S3 lifecycle rule
# removes backups after 30 days.
set -Eeuo pipefail

on_error() {
    echo "backup FAILED at line $1" >&2
    if [[ -n "${NTFY_URL:-}" ]]; then
        auth=()
        if [[ -n "${NTFY_TOKEN:-}" ]]; then
            auth=(-H "Authorization: Bearer ${NTFY_TOKEN}")
        fi
        curl -fsS -m 10 "${auth[@]}" -H "Title: HomeWatch backup failed" -H "Priority: high" \
            -d "Tonight's database backup on the AWS server failed. Check: journalctl -u homewatch-backup" \
            "$NTFY_URL" > /dev/null || true
    fi
}
trap 'on_error $LINENO' ERR

if [[ -z "${BACKUP_BUCKET:-}" ]]; then
    echo "BACKUP_BUCKET is not set in /opt/homewatch/.env" >&2
    false
fi
cd /opt/homewatch

dump=$(mktemp /tmp/homewatch-backup.XXXXXX)
trap 'rm -f "$dump"' EXIT

# -T: no terminal, so the binary dump passes through untouched.
docker compose exec -T db pg_dump -U homewatch -d homewatch -Fc > "$dump"

# Sanity check before uploading: pg_restore can read the file's table of contents.
docker compose exec -T db pg_restore --list < "$dump" > /dev/null

key="nightly/homewatch-$(date -u +%Y-%m-%dT%H%MZ).dump"
aws s3 cp "$dump" "s3://${BACKUP_BUCKET}/${key}" --region us-east-2 --only-show-errors
echo "backup uploaded: ${key} ($(stat -c %s "$dump") bytes)"