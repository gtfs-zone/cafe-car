"""normalise informed_entity

Revision ID: b3c4d5e6f7a8
Revises: a98f898dc627
Create Date: 2026-03-08 01:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, Sequence[str], None] = 'a98f898dc627'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Drop all existing service alerts — informed_entity replaces the flat fields.
    op.execute('DELETE FROM service_alert')

    op.drop_column('service_alert', 'agency_id')
    op.drop_column('service_alert', 'route_id')
    op.drop_column('service_alert', 'stop_id')
    op.drop_column('service_alert', 'trip_id')

    op.create_table('informed_entity',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('service_alert_id', sa.Integer(), nullable=False),
    sa.Column('agency_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('route_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('route_type', sa.Integer(), nullable=True),
    sa.Column('direction_id', sa.Integer(), nullable=True),
    sa.Column('stop_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('trip_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('trip_route_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('trip_direction_id', sa.Integer(), nullable=True),
    sa.Column('trip_start_time', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('trip_start_date', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.CheckConstraint(
        'agency_id IS NOT NULL OR route_id IS NOT NULL OR route_type IS NOT NULL '
        'OR direction_id IS NOT NULL OR stop_id IS NOT NULL OR trip_id IS NOT NULL',
        name='ck_informed_entity_has_specifier',
    ),
    sa.ForeignKeyConstraint(['service_alert_id'], ['service_alert.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('informed_entity')

    op.add_column('service_alert', sa.Column('agency_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('service_alert', sa.Column('route_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('service_alert', sa.Column('stop_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('service_alert', sa.Column('trip_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
