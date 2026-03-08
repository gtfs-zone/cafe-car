"""add gtfs static tables

Revision ID: c5e6f7a8b9c0
Revises: b3c4d5e6f7a8
Create Date: 2026-03-08 02:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "c5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feed_load_status",
        sa.Column("feed_id", sa.Integer(), nullable=False),
        sa.Column("last_loaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("error_message", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("stop_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("route_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trip_count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["feed_id"], ["feed.id"]),
        sa.PrimaryKeyConstraint("feed_id"),
    )

    op.create_table(
        "gtfs_stop",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("feed_id", sa.Integer(), nullable=False),
        sa.Column("stop_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("stop_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("stop_lat", sa.Float(), nullable=False),
        sa.Column("stop_lon", sa.Float(), nullable=False),
        sa.Column("stop_code", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("stop_desc", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(["feed_id"], ["feed.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("feed_id", "stop_id"),
    )

    op.create_table(
        "gtfs_route",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("feed_id", sa.Integer(), nullable=False),
        sa.Column("route_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("agency_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column(
            "route_short_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column(
            "route_long_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column("route_type", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["feed_id"], ["feed.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("feed_id", "route_id"),
    )

    op.create_table(
        "gtfs_trip",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("feed_id", sa.Integer(), nullable=False),
        sa.Column("trip_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("route_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("service_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("trip_headsign", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("direction_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["feed_id"], ["feed.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("feed_id", "trip_id"),
    )

    op.create_table(
        "gtfs_stop_time",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("feed_id", sa.Integer(), nullable=False),
        sa.Column("trip_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("stop_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "arrival_time", sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column(
            "departure_time", sqlmodel.sql.sqltypes.AutoString(), nullable=False
        ),
        sa.Column("stop_sequence", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["feed_id"], ["feed.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_gtfs_stop_time_feed_trip",
        "gtfs_stop_time",
        ["feed_id", "trip_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_gtfs_stop_time_feed_trip", table_name="gtfs_stop_time")
    op.drop_table("gtfs_stop_time")
    op.drop_table("gtfs_trip")
    op.drop_table("gtfs_route")
    op.drop_table("gtfs_stop")
    op.drop_table("feed_load_status")
