"""Scheduling logic: slot matching, validation, and edge-case handling."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from when2meet_mcp.client import Poll, Slot, _indices_to_bits, _parse_show_slot_label

WEEKDAYS = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)
DAY_TO_INDEX = {day: index for index, day in enumerate(WEEKDAYS)}


@dataclass
class WeeklyBusyBlock:
    day: str
    start: time
    end: time

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> WeeklyBusyBlock:
        day = data["day"]
        if day not in DAY_TO_INDEX:
            raise ValueError(
                f"Invalid day {day!r}. Expected one of: {', '.join(WEEKDAYS)}"
            )
        return cls(
            day=day,
            start=datetime.strptime(data["start"], "%H:%M").time(),
            end=datetime.strptime(data["end"], "%H:%M").time(),
        )


@dataclass
class AvailabilityPlan:
    free_indices: list[int]
    busy_indices: list[int]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "free_slot_count": len(self.free_indices),
            "busy_slot_count": len(self.busy_indices),
            "free_slot_indices": self.free_indices,
            "warnings": self.warnings,
        }


def parse_slot_label(label: str) -> tuple[str, time, time]:
    day, start = _parse_show_slot_label(label)
    start_dt = datetime.combine(date.min, start)
    end_dt = start_dt + timedelta(minutes=15)
    return day, start, end_dt.time()


def _times_overlap(
    a_start: time,
    a_end: time,
    b_start: time,
    b_end: time,
) -> bool:
    a0 = datetime.combine(date.min, a_start)
    a1 = datetime.combine(date.min, a_end)
    b0 = datetime.combine(date.min, b_start)
    b1 = datetime.combine(date.min, b_end)
    return a0 < b1 and a1 > b0


def _expand_time(value: time, minutes: int) -> time:
    base = datetime.combine(date.min, value) + timedelta(minutes=minutes)
    return base.time()


def poll_local_time_range(poll: Poll) -> dict[str, str]:
    if not poll.slots:
        return {}
    first_day, first_start, _ = parse_slot_label(poll.slots[0].label)
    last_day, last_start, last_end = parse_slot_label(poll.slots[-1].label)
    return {
        "timezone": poll.timezone,
        "first_slot": f"{first_day} {first_start.strftime('%I:%M %p')}",
        "last_slot": f"{last_day} {last_end.strftime('%I:%M %p')}",
        "slot_minutes": 15,
        "rows_per_day": poll.rows_per_day,
        "day_count": len(poll.slots) // poll.rows_per_day,
    }


def infer_week_start_date(
    reference: date | None = None,
    first_column_day: str = "Sunday",
) -> date:
    """Infer column-0 date from a reference day (defaults to today)."""
    reference = reference or date.today()
    if first_column_day not in DAY_TO_INDEX:
        raise ValueError(f"Invalid first_column_day: {first_column_day!r}")
    target_index = DAY_TO_INDEX[first_column_day]
    current_index = (reference.weekday() + 1) % 7
    return reference - timedelta(days=(current_index - target_index) % 7)


def compute_from_weekly_busy_blocks(
    poll: Poll,
    busy_blocks: list[WeeklyBusyBlock],
    buffer_minutes: int = 0,
    block_before: str | None = None,
    block_after: str | None = None,
) -> AvailabilityPlan:
    """Match busy blocks by weekday + local time using poll slot labels."""
    warnings: list[str] = []
    block_before_time = (
        datetime.strptime(block_before, "%H:%M").time() if block_before else None
    )
    block_after_time = (
        datetime.strptime(block_after, "%H:%M").time() if block_after else None
    )

    expanded_blocks: list[WeeklyBusyBlock] = []
    for block in busy_blocks:
        if block.end <= block.start:
            warnings.append(
                f"Ignored invalid block {block.day} {block.start}-{block.end} (end <= start)"
            )
            continue
        expanded_blocks.append(
            WeeklyBusyBlock(
                day=block.day,
                start=_expand_time(block.start, -buffer_minutes),
                end=_expand_time(block.end, buffer_minutes),
            )
        )

    free_indices: list[int] = []
    busy_indices: list[int] = []

    for slot in poll.slots:
        day, slot_start, slot_end = parse_slot_label(slot.label)

        if block_before_time and slot_start < block_before_time:
            busy_indices.append(slot.index)
            continue
        if block_after_time and slot_end > block_after_time:
            busy_indices.append(slot.index)
            continue

        is_busy = any(
            block.day == day
            and _times_overlap(slot_start, slot_end, block.start, block.end)
            for block in expanded_blocks
        )
        if is_busy:
            busy_indices.append(slot.index)
        else:
            free_indices.append(slot.index)

    poll_range = poll_local_time_range(poll)
    warnings.append(
        "Matched slots using weekday labels in "
        f"{poll.timezone} ({poll_range.get('first_slot')} – {poll_range.get('last_slot')}). "
        "Times outside this range cannot be expressed on the poll."
    )

    return AvailabilityPlan(
        free_indices=free_indices,
        busy_indices=busy_indices,
        warnings=warnings,
    )


def expand_all_day_events(
    all_day_dates: list[str],
    timezone: str,
) -> list[tuple[datetime, datetime]]:
    tz = ZoneInfo(timezone)
    intervals: list[tuple[datetime, datetime]] = []
    for value in all_day_dates:
        day = date.fromisoformat(value)
        start = datetime.combine(day, time.min, tzinfo=tz)
        end = start + timedelta(days=1)
        intervals.append((start, end))
    return intervals


def compute_from_busy_intervals(
    poll: Poll,
    week_start_date: date,
    busy_intervals: list[tuple[datetime, datetime]],
    timezone: str,
    buffer_minutes: int = 0,
    block_before: str | None = None,
    block_after: str | None = None,
) -> AvailabilityPlan:
    from when2meet_mcp.client import slot_window

    warnings: list[str] = []
    tz = ZoneInfo(timezone)

    normalized_busy: list[tuple[datetime, datetime]] = []
    for start, end in busy_intervals:
        if end <= start:
            warnings.append(f"Ignored invalid interval {start.isoformat()} – {end.isoformat()}")
            continue
        normalized_busy.append(
            (
                start.astimezone(tz) - timedelta(minutes=buffer_minutes),
                end.astimezone(tz) + timedelta(minutes=buffer_minutes),
            )
        )

    block_before_time = (
        datetime.strptime(block_before, "%H:%M").time() if block_before else None
    )
    block_after_time = (
        datetime.strptime(block_after, "%H:%M").time() if block_after else None
    )

    free_indices: list[int] = []
    busy_indices: list[int] = []
    grid_starts: list[datetime] = []
    grid_ends: list[datetime] = []

    for slot in poll.slots:
        start, end = slot_window(poll, slot, week_start_date, tz)
        grid_starts.append(start)
        grid_ends.append(end)

        if block_before_time and start.time() < block_before_time:
            busy_indices.append(slot.index)
            continue
        if block_after_time and end.time() > block_after_time:
            busy_indices.append(slot.index)
            continue

        overlaps = any(
            start < busy_end and end > busy_start
            for busy_start, busy_end in normalized_busy
        )
        if overlaps:
            busy_indices.append(slot.index)
        else:
            free_indices.append(slot.index)

    if grid_starts and grid_ends:
        grid_start = min(grid_starts)
        grid_end = max(grid_ends)
        for busy_start, busy_end in normalized_busy:
            if busy_end <= grid_start or busy_start >= grid_end:
                warnings.append(
                    "Busy interval outside poll grid and cannot be represented: "
                    f"{busy_start.isoformat()} – {busy_end.isoformat()}"
                )

    warnings.append(
        f"Used week_start_date={week_start_date.isoformat()} for column 0 "
        f"({WEEKDAYS[0]}). Verify this matches the poll week."
    )

    return AvailabilityPlan(
        free_indices=free_indices,
        busy_indices=busy_indices,
        warnings=warnings,
    )


def find_participant_by_name(poll: Poll, name: str) -> list[dict[str, Any]]:
    wanted = name.casefold()
    return [
        {
            "name": participant.name,
            "person_id": participant.person_id,
            "available_slot_count": len(participant.available_slot_indices),
            "exact_match": participant.name == name,
        }
        for participant in poll.participants
        if participant.name.casefold() == wanted
        or wanted in participant.name.casefold()
    ]


def validate_slot_indices(
    poll: Poll,
    slot_indices: list[int],
) -> tuple[list[int], list[int], list[str]]:
    warnings: list[str] = []
    valid: list[int] = []
    invalid: list[int] = []
    seen: set[int] = set()

    for index in slot_indices:
        if index in seen:
            warnings.append(f"Duplicate slot index {index} removed")
            continue
        seen.add(index)
        if 0 <= index < len(poll.slots):
            valid.append(index)
        else:
            invalid.append(index)

    if invalid:
        warnings.append(
            f"Dropped {len(invalid)} out-of-range indices (valid: 0–{len(poll.slots) - 1})"
        )

    return sorted(valid), invalid, warnings


def build_bitmask(poll: Poll, free_indices: list[int]) -> str:
    valid, _, warnings = validate_slot_indices(poll, free_indices)
    bits = _indices_to_bits(len(poll.slots), valid)
    if len(bits) != len(poll.slots):
        raise ValueError(
            f"Bitmask length mismatch: expected {len(poll.slots)}, got {len(bits)}"
        )
    if warnings:
        pass
    return bits


def summarize_free_time_by_day(poll: Poll, free_indices: set[int]) -> dict[str, str]:
    by_day: dict[str, list[str]] = {day: [] for day in WEEKDAYS}
    for slot in poll.slots:
        if slot.index in free_indices:
            day, start, _ = parse_slot_label(slot.label)
            by_day[day].append(start.strftime("%H:%M"))

    summary: dict[str, str] = {}
    for day, times in by_day.items():
        if not times:
            continue
        summary[day] = f"{times[0]}–{times[-1]} ({len(times)} slots)"
    return summary


def unmarked_participants(poll: Poll) -> list[str]:
    return [
        participant.name
        for participant in poll.participants
        if not participant.available_slot_indices
    ]
