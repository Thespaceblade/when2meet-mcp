"""Tests for when2meet MCP scheduling logic."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from when2meet_mcp.client import Poll, PollRef, Slot
from when2meet_mcp.scheduling import (
    WeeklyBusyBlock,
    compute_from_weekly_busy_blocks,
    infer_week_start_date,
    validate_slot_indices,
)


def _sample_poll() -> Poll:
    slots = []
    labels = [
        ("Sunday", "09:00:00 AM"),
        ("Sunday", "09:15:00 AM"),
        ("Monday", "09:00:00 AM"),
        ("Monday", "10:00:00 AM"),
    ]
    for index, (day, clock) in enumerate(labels):
        slots.append(
            Slot(
                index=index,
                slot_id=1000 + index,
                label=f"{day} {clock}",
                column=index // 2,
                row=index % 2,
            )
        )
    return Poll(
        ref=PollRef("1", "abc"),
        title="Test",
        timezone="America/New_York",
        slots=slots,
        participants=[],
        rows_per_day=2,
        first_slot_time=datetime.strptime("09:00:00 AM", "%I:%M:%S %p").time(),
    )


def test_weekly_busy_blocks():
    poll = _sample_poll()
    plan = compute_from_weekly_busy_blocks(
        poll,
        [WeeklyBusyBlock("Monday", datetime.strptime("09:00", "%H:%M").time(),
                         datetime.strptime("09:30", "%H:%M").time())],
    )
    assert plan.busy_indices == [2]
    assert plan.free_indices == [0, 1, 3]


def test_infer_week_start_date():
    # 2026-09-01 is a Tuesday
    assert infer_week_start_date(date(2026, 9, 1)).isoformat() == "2026-08-30"


def test_validate_slot_indices():
    poll = _sample_poll()
    valid, invalid, warnings = validate_slot_indices(poll, [0, 1, 99, 99])
    assert valid == [0, 1]
    assert invalid == [99]
    assert warnings
