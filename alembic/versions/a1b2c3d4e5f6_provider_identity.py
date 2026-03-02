"""provider identity

Revision ID: a1b2c3d4e5f6
Revises: 92d56d48d6c3
Create Date: 2026-03-01 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "92d56d48d6c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new columns as nullable first to allow data migration
    op.add_column("user", sa.Column("provider", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column("user", sa.Column("provider_subject", sqlmodel.sql.sqltypes.AutoString(), nullable=True))

    # Migrate existing rows: treat username as provider_subject under "dex"
    op.execute("UPDATE \"user\" SET provider = 'dex', provider_subject = username WHERE provider IS NULL")

    # Now enforce NOT NULL
    op.alter_column("user", "provider", nullable=False)
    op.alter_column("user", "provider_subject", nullable=False)

    # Add unique constraint on (provider, provider_subject)
    op.create_unique_constraint("uq_user_provider_subject", "user", ["provider", "provider_subject"])

    # Drop old username index and column
    op.drop_index("ix_user_username", table_name="user")
    op.drop_column("user", "username")


def downgrade() -> None:
    op.add_column("user", sa.Column("username", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.execute("UPDATE \"user\" SET username = provider_subject WHERE username IS NULL")
    op.alter_column("user", "username", nullable=False)
    op.create_index("ix_user_username", "user", ["username"], unique=True)

    op.drop_constraint("uq_user_provider_subject", "user", type_="unique")
    op.drop_column("user", "provider_subject")
    op.drop_column("user", "provider")
