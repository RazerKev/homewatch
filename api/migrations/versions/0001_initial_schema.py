"""Initial schema: homes, heartbeats, events, alerts.

Revision ID: 0001
Revises:
Create Date: 2026-09-27

homes       one row per monitored home (opaque id + label, hashed API key, watchdog status)
heartbeats  every proof-of-life a home's agent sends
events      sensor state changes; (home_id, event_id) is unique so re-sends are harmless
alerts      the watchdog's transactional outbox and audit trail of what was sent when
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "homes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=40), nullable=False),
        sa.Column("api_key_hash", sa.String(length=64), nullable=False),
        sa.Column("api_key_hint", sa.String(length=16), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("offline_after_s", sa.Integer(), server_default=sa.text("300"), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_healthy_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("problem_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("label ~ '^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$'", name=op.f("ck_homes_label")),
        sa.CheckConstraint("status IN ('pending', 'ok', 'degraded', 'offline')", name=op.f("ck_homes_status")),
        sa.CheckConstraint("offline_after_s BETWEEN 5 AND 86400", name=op.f("ck_homes_offline_after_s")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_homes")),
        sa.UniqueConstraint("api_key_hash", name=op.f("uq_homes_api_key_hash")),
        sa.UniqueConstraint("label", name=op.f("uq_homes_label")),
    )

    op.create_table(
        "heartbeats",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("home_id", sa.Uuid(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent_version", sa.String(length=32), nullable=False),
        sa.Column("ha_ok", sa.Boolean(), nullable=False),
        sa.Column("outbox_depth", sa.Integer(), nullable=False),
        sa.Column("dead_letters", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["home_id"], ["homes.id"], name=op.f("fk_heartbeats_home_id_homes"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_heartbeats")),
    )
    op.create_index("ix_heartbeats_home_id_received_at", "heartbeats", ["home_id", "received_at"])

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("home_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Double(), nullable=True),
        sa.Column("device_class", sa.String(length=64), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["home_id"], ["homes.id"], name=op.f("fk_events_home_id_homes"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint("home_id", "event_id", name="uq_events_home_id_event_id"),
    )
    op.create_index(
        "ix_events_home_id_entity_id_occurred_at", "events", ["home_id", "entity_id", "occurred_at"]
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("home_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=80), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.String(length=200), nullable=True),
        sa.CheckConstraint("kind IN ('offline', 'degraded', 'recovered')", name=op.f("ck_alerts_kind")),
        sa.ForeignKeyConstraint(["home_id"], ["homes.id"], name=op.f("fk_alerts_home_id_homes"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alerts")),
    )
    # Partial index: the watchdog only ever looks for alerts that haven't been delivered.
    op.create_index(
        "ix_alerts_undelivered", "alerts", ["id"], postgresql_where=sa.text("delivered_at IS NULL")
    )


def downgrade() -> None:
    op.drop_table("alerts")
    op.drop_table("events")
    op.drop_table("heartbeats")
    op.drop_table("homes")
