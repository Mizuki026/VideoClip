from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from videoclip.models import SharedModelCooldown
from videoclip.services import db_transaction

ASR_BACKPRESSURE_ERROR_CODES = frozenset({"ASR_RATE_LIMITED", "ASR_MODEL_OVERLOADED"})
LLM_BACKPRESSURE_ERROR_CODES = frozenset({"LLM_RATE_LIMITED", "LLM_MODEL_OVERLOADED"})
MODEL_BACKPRESSURE_ERROR_CODES = ASR_BACKPRESSURE_ERROR_CODES | LLM_BACKPRESSURE_ERROR_CODES

_DELAY_SECONDS = re.compile(r"^[0-9]+$")
_ASCTIME_DATE = re.compile(
    r"^[A-Za-z]{3} [A-Za-z]{3} (?:[0-9]{2}| [0-9]) [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}$"
)
_HTTP_DATE = re.compile(
    r"^(?:"
    r"[A-Za-z]{3}, [0-9]{2} [A-Za-z]{3} [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT"
    r"|[A-Za-z]+, [0-9]{2}-[A-Za-z]{3}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT"
    r"|[A-Za-z]{3} [A-Za-z]{3} (?:[0-9]{2}| [0-9]) [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}"
    r")$"
)


@dataclass(frozen=True)
class ModelIdentity:
    provider: str
    account_reference: str
    model: str


@dataclass(frozen=True)
class CooldownPolicy:
    backoff_base_seconds: int = 60
    backoff_max_seconds: int = 900


@dataclass(frozen=True)
class CooldownUpdate:
    identity: ModelIdentity
    cooldown_until: datetime
    consecutive_backpressure_responses: int
    retry_after_accepted: bool


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("model cooldown timestamps must include a timezone")
    return value.astimezone(UTC)


# 将服务商、非敏感账户引用和模型名组合成共享冷却键，不把访问密钥放进标识。
def model_identity(provider: object, account_reference: object, model: object) -> ModelIdentity:
    if not isinstance(provider, str) or not re.fullmatch(r"[a-z0-9_-]{1,32}", provider):
        raise ValueError("invalid model provider identity")
    if (
        not isinstance(account_reference, str)
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", account_reference)
        or account_reference.lower().startswith(("sk-", "sf-"))
    ):
        raise ValueError("invalid non-secret model account reference")
    if not isinstance(model, str) or not model or len(model) > 128:
        raise ValueError("invalid model identity")
    return ModelIdentity(provider, account_reference, model)


def model_identity_from_config(
    value: Mapping[str, object], *, default_account_reference: str
) -> ModelIdentity:
    return model_identity(
        value.get("provider"),
        value.get("account_reference", default_account_reference),
        value.get("model"),
    )


def retry_after_deadline(value: str | None, *, now: datetime) -> datetime | None:
    """Parse RFC 9110 delay-seconds or HTTP-date; invalid fields request local fallback."""
    timestamp = require_utc(now)
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    if any(ord(character) < 32 or ord(character) > 126 for character in value):
        return None
    candidate = value.strip()
    if _DELAY_SECONDS.fullmatch(candidate):
        try:
            seconds = int(candidate)
            return timestamp + timedelta(seconds=seconds)
        except (ValueError, OverflowError):
            return datetime.max.replace(tzinfo=UTC)
    if not _HTTP_DATE.fullmatch(candidate):
        return None
    try:
        parsed = parsedate_to_datetime(candidate)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        if _ASCTIME_DATE.fullmatch(candidate):
            parsed = parsed.replace(tzinfo=UTC)
        else:
            return None
    return require_utc(parsed)


def fallback_cooldown_seconds(
    consecutive_backpressure_responses: int,
    *,
    policy: CooldownPolicy,
    random_fraction: float,
) -> int:
    """Bounded equal-jitter exponential backoff with an injected random fraction."""
    if consecutive_backpressure_responses < 1:
        raise ValueError("consecutive backpressure response count must be positive")
    if policy.backoff_base_seconds < 1 or policy.backoff_max_seconds < policy.backoff_base_seconds:
        raise ValueError("invalid cooldown backoff policy")
    if not math.isfinite(random_fraction) or not 0 <= random_fraction <= 1:
        raise ValueError("random fraction must be between zero and one")
    exponent = min(consecutive_backpressure_responses - 1, 30)
    nominal = min(policy.backoff_max_seconds, policy.backoff_base_seconds * (2**exponent))
    lower = int(math.ceil(nominal / 2))
    width = nominal - lower
    return lower + int(random_fraction * width)


# 优先采用服务端返回的重试时间；缺失或无效时使用有上限的退避时长。
def proposed_cooldown_deadline(
    *,
    now: datetime,
    retry_after: str | None,
    consecutive_backpressure_responses: int,
    random_fraction: float,
    policy: CooldownPolicy,
) -> tuple[datetime, bool]:
    timestamp = require_utc(now)
    provider_deadline = retry_after_deadline(retry_after, now=timestamp)
    if provider_deadline is not None:
        return provider_deadline, True
    seconds = fallback_cooldown_seconds(
        consecutive_backpressure_responses,
        policy=policy,
        random_fraction=random_fraction,
    )
    return timestamp + timedelta(seconds=seconds), False


def current_cooldown_until(session: Session, identity: ModelIdentity) -> datetime | None:
    row = session.scalar(
        select(SharedModelCooldown).where(
            SharedModelCooldown.provider == identity.provider,
            SharedModelCooldown.account_reference == identity.account_reference,
            SharedModelCooldown.model == identity.model,
        )
    )
    return require_utc(row.cooldown_until) if row is not None else None


# 429 与模型过载的冷却期限按服务商、账户引用和模型共享。
# 通过数据库行锁合并并发冷却更新，只延长到更晚的期限并累计限流次数。
def extend_shared_cooldown(
    factory: sessionmaker[Session],
    identity: ModelIdentity,
    *,
    now: datetime,
    retry_after: str | None,
    response_received_at: datetime | None,
    random_fraction: float,
    policy: CooldownPolicy,
) -> CooldownUpdate:
    timestamp = require_utc(now)
    observed_at = require_utc(response_received_at) if response_received_at is not None else timestamp
    with db_transaction(factory) as session:
        session.execute(
            insert(SharedModelCooldown)
            .values(
                id=uuid.uuid4(),
                provider=identity.provider,
                account_reference=identity.account_reference,
                model=identity.model,
                cooldown_until=timestamp,
                consecutive_backpressure_responses=0,
                last_backpressure_at=timestamp,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    SharedModelCooldown.provider,
                    SharedModelCooldown.account_reference,
                    SharedModelCooldown.model,
                ]
            )
        )
        row = session.scalar(
            select(SharedModelCooldown)
            .where(
                SharedModelCooldown.provider == identity.provider,
                SharedModelCooldown.account_reference == identity.account_reference,
                SharedModelCooldown.model == identity.model,
            )
            .with_for_update()
        )
        if row is None:
            raise RuntimeError("shared model cooldown reservation disappeared")
        count = row.consecutive_backpressure_responses + 1
        candidate, accepted = proposed_cooldown_deadline(
            now=observed_at,
            retry_after=retry_after,
            consecutive_backpressure_responses=count,
            random_fraction=random_fraction,
            policy=policy,
        )
        current = require_utc(row.cooldown_until)
        row.cooldown_until = max(current, candidate)
        row.consecutive_backpressure_responses = count
        row.last_backpressure_at = timestamp
        return CooldownUpdate(identity, require_utc(row.cooldown_until), count, accepted)


# 成功响应清除连续限流计数，为之后的新一轮限流重新计算退避。
def record_model_success(
    factory: sessionmaker[Session],
    identity: ModelIdentity,
    *,
    request_started_at: datetime,
) -> None:
    started = require_utc(request_started_at)
    with db_transaction(factory) as session:
        row = session.scalar(
            select(SharedModelCooldown)
            .where(
                SharedModelCooldown.provider == identity.provider,
                SharedModelCooldown.account_reference == identity.account_reference,
                SharedModelCooldown.model == identity.model,
            )
            .with_for_update()
        )
        if row is not None and require_utc(row.cooldown_until) <= started:
            row.consecutive_backpressure_responses = 0
