"""generalize shared model cooldown backpressure metadata"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7a3951e2f48"
down_revision: str | Sequence[str] | None = "1a7e3c9d5b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_shared_model_cooldown_count_nonnegative",
        "shared_model_cooldowns",
        type_="check",
    )
    op.alter_column(
        "shared_model_cooldowns",
        "consecutive_rate_limits",
        new_column_name="consecutive_backpressure_responses",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        "shared_model_cooldowns",
        "last_rate_limited_at",
        new_column_name="last_backpressure_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_shared_model_backpressure_count_nonnegative",
        "shared_model_cooldowns",
        "consecutive_backpressure_responses >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_shared_model_backpressure_count_nonnegative",
        "shared_model_cooldowns",
        type_="check",
    )
    op.alter_column(
        "shared_model_cooldowns",
        "last_backpressure_at",
        new_column_name="last_rate_limited_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
    )
    op.alter_column(
        "shared_model_cooldowns",
        "consecutive_backpressure_responses",
        new_column_name="consecutive_rate_limits",
        existing_type=sa.Integer(),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_shared_model_cooldown_count_nonnegative",
        "shared_model_cooldowns",
        "consecutive_rate_limits >= 0",
    )
