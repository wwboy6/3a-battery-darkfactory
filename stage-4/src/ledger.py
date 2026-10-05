"""Temporal ledger.

Pure helpers for revision selection, balances, historical holds and the
statement window. Kept separate from the mutable Store so the money math is
easy to read and reason about.
"""

from datetime import datetime, timedelta, timezone

from models import ApiError


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(value: str) -> datetime:
    text = value
    if isinstance(text, str) and (text.endswith("Z") or text.endswith("z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_instant(value, field: str) -> datetime:
    """Parse a strict RFC 3339 instant that must carry an offset."""
    if not isinstance(value, str) or value == "":
        raise ApiError(422, "validation_failed", "{} must be an RFC 3339 instant".format(field))
    text = value
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        raise ApiError(422, "validation_failed", "{} must be an RFC 3339 instant".format(field))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApiError(422, "validation_failed", "{} must include a UTC offset".format(field))
    return parsed


def effective_status(authorization, now=None):
    if authorization.status == "open":
        now = now or datetime.now(timezone.utc)
        if parse_ts(authorization.expires_at) <= now:
            return "expired"
    return authorization.status


def remaining_amount(authorization, now=None):
    if effective_status(authorization, now) == "open":
        return authorization.amount - authorization.captured_amount
    return 0


def select_revision(revisions, known_at):
    """Latest revision recorded at or before known_at (None means everything)."""
    chosen = None
    for revision in revisions:
        if known_at is None or parse_ts(revision.recorded_at) <= known_at:
            chosen = revision
        else:
            break
    return chosen


def selected_map(revisions_by_payment, known_at):
    selected = {}
    for payment_id, revisions in revisions_by_payment.items():
        revision = select_revision(revisions, known_at)
        if revision is not None:
            selected[payment_id] = revision
    return selected


def total_for(opening, payments, selected, user_id, as_of=None, inclusive=True):
    total = opening
    for payment_id, revision in selected.items():
        payment = payments.get(payment_id)
        if payment is None:
            continue
        if user_id != payment.from_user_id and user_id != payment.to_user_id:
            continue
        if as_of is not None:
            effective = parse_ts(revision.effective_at)
            if inclusive:
                if effective > as_of:
                    continue
            elif effective >= as_of:
                continue
        if payment.from_user_id == user_id:
            total -= revision.amount
        else:
            total += revision.amount
    return total


def closure(authorization, captures):
    """Release time and the instant the release became known, or (None, None)."""
    status = authorization.status
    created = parse_ts(authorization.created_at)
    if status in ("captured", "voided", "expired"):
        if authorization.closed_at:
            closed = parse_ts(authorization.closed_at)
            return closed, closed
        # Seeded closed holds need no prior-lifecycle reconstruction: treat
        # them as already closed at creation.
        return created, created
    expiry = parse_ts(authorization.expires_at)
    return expiry, created


def hold_events(authorization, captures):
    """Return (event_time, known_from, hold_after) sorted by event time."""
    created = parse_ts(authorization.created_at)
    events = [(created, created, authorization.amount)]
    running = authorization.amount
    ordered = sorted(captures, key=lambda payment: (parse_ts(payment.created_at), payment.id))
    for capture in ordered:
        running -= capture.amount
        when = parse_ts(capture.created_at)
        events.append((when, when, running))
    close_time, close_known = closure(authorization, captures)
    events.append((close_time, close_known, 0))
    events.sort(key=lambda event: event[0])
    return events


def hold_value(authorization, captures, as_of, known_at):
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    created = parse_ts(authorization.created_at)
    if known_at is not None and created > known_at:
        return 0
    if created > as_of:
        return 0
    value = authorization.amount
    for capture in captures:
        when = parse_ts(capture.created_at)
        if known_at is not None and when > known_at:
            continue
        if when > as_of:
            continue
        value -= capture.amount
    close_time, close_known = closure(authorization, captures)
    if known_at is not None and close_known > known_at:
        pass
    elif close_time <= as_of:
        value = 0
    if value < 0:
        value = 0
    return value


def held_for(authorizations, captures_by_auth, user_id, as_of, known_at):
    total = 0
    for authorization in authorizations:
        if authorization.from_user_id != user_id:
            continue
        total += hold_value(authorization, captures_by_auth.get(authorization.id, []), as_of, known_at)
    return total


def historical_overdraft(openings, payments, revisions_by_payment, authorizations, captures_by_auth):
    """True if any user's total or available is negative at an event boundary."""
    selected = selected_map(revisions_by_payment, None)
    boundaries = set()
    for revision in selected.values():
        boundaries.add(parse_ts(revision.effective_at))
    for authorization in authorizations:
        for when, known, hold_after in hold_events(authorization, captures_by_auth.get(authorization.id, [])):
            boundaries.add(when)
    for instant in sorted(boundaries):
        for user_id, opening in openings.items():
            total = total_for(opening, payments, selected, user_id, as_of=instant)
            if total < 0:
                return True
            held = held_for(authorizations, captures_by_auth, user_id, instant, None)
            if total - held < 0:
                return True
    return False


def next_recorded_at(previous):
    now = datetime.now(timezone.utc)
    if previous is not None:
        previous_dt = parse_ts(previous)
        if now <= previous_dt:
            now = previous_dt + timedelta(microseconds=1)
    return now.isoformat()


def revision_json(revision):
    return {
        "revision": revision.revision,
        "amount": revision.amount,
        "effective_at": revision.effective_at,
        "recorded_at": revision.recorded_at,
        "reason": revision.reason,
        "correction_batch_id": revision.correction_batch_id,
    }
