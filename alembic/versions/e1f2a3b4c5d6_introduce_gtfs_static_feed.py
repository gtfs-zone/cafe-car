"""introduce gtfs_static_feed

Revision ID: e1f2a3b4c5d6
Revises: c5e6f7a8b9c0
Create Date: 2026-03-08 03:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "c5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create gtfs_static_feed table
    op.create_table(
        "gtfs_static_feed",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("timezone", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column(
            "status",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("error_message", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("last_loaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # 2. Add gtfs_static_feed_id to feed
    op.add_column(
        "feed",
        sa.Column("gtfs_static_feed_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_feed_gtfs_static_feed_id",
        "feed",
        "gtfs_static_feed",
        ["gtfs_static_feed_id"],
        ["id"],
    )

    # 3. Drop index on old gtfs_stop_time
    op.drop_index("ix_gtfs_stop_time_feed_trip", table_name="gtfs_stop_time")

    # 4. Drop old GTFS tables (FK order) and feed_load_status — data is ephemeral
    op.drop_table("gtfs_stop_time")
    op.drop_table("gtfs_trip")
    op.drop_table("gtfs_route")
    op.drop_table("gtfs_stop")
    op.drop_table("feed_load_status")

    # 5. Recreate GTFS tables with gtfs_static_feed_id FK
    op.create_table(
        "gtfs_stop",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gtfs_static_feed_id", sa.Integer(), nullable=False),
        sa.Column("stop_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("stop_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("stop_lat", sa.Float(), nullable=False),
        sa.Column("stop_lon", sa.Float(), nullable=False),
        sa.Column("stop_code", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("stop_desc", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(["gtfs_static_feed_id"], ["gtfs_static_feed.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gtfs_static_feed_id", "stop_id"),
    )

    op.create_table(
        "gtfs_route",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gtfs_static_feed_id", sa.Integer(), nullable=False),
        sa.Column("route_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("agency_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("route_short_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("route_long_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("route_type", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["gtfs_static_feed_id"], ["gtfs_static_feed.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gtfs_static_feed_id", "route_id"),
    )

    op.create_table(
        "gtfs_trip",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gtfs_static_feed_id", sa.Integer(), nullable=False),
        sa.Column("trip_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("route_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("service_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("trip_headsign", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("direction_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["gtfs_static_feed_id"], ["gtfs_static_feed.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gtfs_static_feed_id", "trip_id"),
    )

    op.create_table(
        "gtfs_stop_time",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gtfs_static_feed_id", sa.Integer(), nullable=False),
        sa.Column("trip_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("stop_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("arrival_time", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("departure_time", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("stop_sequence", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["gtfs_static_feed_id"], ["gtfs_static_feed.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_gtfs_stop_time_gsf_trip",
        "gtfs_stop_time",
        ["gtfs_static_feed_id", "trip_id"],
    )


def downgrade() -> None:
    # Drop new tables
    op.drop_index("ix_gtfs_stop_time_gsf_trip", table_name="gtfs_stop_time")
    op.drop_table("gtfs_stop_time")
    op.drop_table("gtfs_trip")
    op.drop_table("gtfs_route")
    op.drop_table("gtfs_stop")

    # Remove gtfs_static_feed_id from feed
    op.drop_constraint("fk_feed_gtfs_static_feed_id", "feed", type_="foreignkey")
    op.drop_column("feed", "gtfs_static_feed_id")

    # Drop gtfs_static_feed
    op.drop_table("gtfs_static_feed")

    # Recreate old tables with feed_id FK
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
        sa.Column("route_short_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("route_long_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
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
        sa.Column("arrival_time", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("departure_time", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("stop_sequence", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["feed_id"], ["feed.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_gtfs_stop_time_feed_trip",
        "gtfs_stop_time",
        ["feed_id", "trip_id"],
    )
