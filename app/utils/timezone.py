from datetime import date, datetime, time, timedelta, timezone

VENEZUELA_TIMEZONE = timezone(timedelta(hours=-4), name='GMT-4')


def now_ve() -> datetime:
    """Return current timezone-aware datetime in Venezuela (GMT-4)."""
    return datetime.now(VENEZUELA_TIMEZONE)


def now_ve_naive() -> datetime:
    """Return Venezuela local time as naive datetime (for legacy filename/code use)."""
    return now_ve().replace(tzinfo=None)


def to_ve(value: datetime | None) -> datetime | None:
    """
    Convert a datetime to Venezuela timezone.

    Naive values are treated as UTC because app records are stored in UTC.
    """
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(VENEZUELA_TIMEZONE)


def format_ve(value: datetime | None, fmt: str = '%d/%m/%Y %H:%M') -> str:
    """Format datetime in Venezuela timezone."""
    converted = to_ve(value)
    if converted is None:
        return ''
    return converted.strftime(fmt)


def ve_day_start_utc_naive(day: date) -> datetime:
    """Return UTC-naive datetime for 00:00 of the given Venezuela local day."""
    start_ve = datetime.combine(day, time.min, tzinfo=VENEZUELA_TIMEZONE)
    return start_ve.astimezone(timezone.utc).replace(tzinfo=None)
