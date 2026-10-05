from videoclip.models import SharedModelCooldown


def test_shared_model_cooldown_deadline_index_is_declared() -> None:
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in SharedModelCooldown.__table__.indexes
    }

    assert indexes["ix_shared_model_cooldowns_until"] == ("cooldown_until",)
