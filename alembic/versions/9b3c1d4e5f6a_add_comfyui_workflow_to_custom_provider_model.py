"""add comfyui_workflow to custom_provider_model

Revision ID: 9b3c1d4e5f6a
Revises: a1c7e94f0d23
Create Date: 2026-08-28 10:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b3c1d4e5f6a"
down_revision: str | Sequence[str] | None = "a1c7e94f0d23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("custom_provider_model", schema=None) as batch_op:
        batch_op.add_column(sa.Column("comfyui_workflow", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("custom_provider_model", schema=None) as batch_op:
        batch_op.drop_column("comfyui_workflow")
