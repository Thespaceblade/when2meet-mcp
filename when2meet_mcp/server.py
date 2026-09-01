"""When2meet MCP server."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer

from when2meet_mcp.auth import WrongPasswordError, login_with_fallback
from when2meet_mcp.client import (
    When2MeetClient,
    find_overlap_slots,
    parse_poll_url,
    poll_to_dict,
)
from when2meet_mcp.scheduling import (
    AvailabilityPlan,
    WeeklyBusyBlock,
    build_bitmask,
    compute_from_busy_intervals,
    compute_from_weekly_busy_blocks,
    expand_all_day_events,
    find_participant_by_name,
    infer_week_start_date,
    summarize_free_time_by_day,
    validate_slot_indices,
)

mcp = MCPServer(
    "when2meet",
    instructions=(
        "Tools for reading and filling when2meet scheduling polls via HTTP (no browser). "
        "Recommended flow: get_poll → fill_from_weekly_schedule or compute_availability_from_busy_times "
        "(dry_run=true) → submit_availability. "
        "For recurring weekly schedules, prefer fill_from_weekly_schedule (no week_start_date needed). "
        "For calendar APIs, use compute_availability_from_busy_times with week_start_date. "
        "If login fails with Wrong password, pass password or on_name_conflict='alternate_suffix'."
    ),
)


def _parse_iso_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def _plan_response(
    poll_title: str,
    plan: AvailabilityPlan,
    poll,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preview = [
        {
            "index": poll.slots[i].index,
            "slot_id": poll.slots[i].slot_id,
            "label": poll.slots[i].label,
        }
        for i in plan.free_indices[:50]
    ]
    payload: dict[str, Any] = {
        "title": poll_title,
        **plan.to_dict(),
        "free_time_by_day": summarize_free_time_by_day(poll, set(plan.free_indices)),
        "preview": preview,
        "preview_truncated": len(plan.free_indices) > 50,
    }
    if extra:
        payload.update(extra)
    return payload


@mcp.tool()
def get_poll(
    url: str,
    timezone: str = "America/New_York",
    include_slots: bool = False,
) -> dict[str, Any]:
    """Load a when2meet poll: title, participants, time range, and optional slot grid.

    Args:
        url: Full when2meet URL, e.g. https://www.when2meet.com/?38093485-OZAlv
        timezone: IANA timezone for slot labels (e.g. America/New_York)
        include_slots: If true, include all slot indices (can be 300+ entries)
    """
    with When2MeetClient() as client:
        poll = client.get_poll(url, timezone=timezone)
        data = poll_to_dict(poll, include_slots=include_slots)
        data["inferred_week_start_date"] = infer_week_start_date().isoformat()
        data["warnings"] = [
            "Participants with 0 marked slots appear fully free in overlap calculations.",
            f"Poll grid covers {data['poll_time_range'].get('first_slot')} – "
            f"{data['poll_time_range'].get('last_slot')} in {timezone}.",
        ]
        if data["unmarked_participants"]:
            data["warnings"].append(
                "Unmarked participants (may look free everywhere): "
                + ", ".join(data["unmarked_participants"])
            )
        return data


@mcp.tool()
def find_common_availability(
    url: str,
    timezone: str = "America/New_York",
    participant_names: list[str] | None = None,
    min_participants: int | None = None,
    only_marked_participants: bool = True,
) -> dict[str, Any]:
    """Find times when participants overlap.

    Args:
        url: when2meet poll URL
        timezone: IANA timezone for slot labels
        participant_names: Optional subset of names
        min_participants: Minimum number of people free (default: all selected)
        only_marked_participants: Exclude participants who have not marked any slots
    """
    with When2MeetClient() as client:
        poll = client.get_poll(url, timezone=timezone)
        selected = poll.participants
        if only_marked_participants:
            selected = [p for p in selected if p.available_slot_indices]
        if participant_names:
            wanted = {name.casefold() for name in participant_names}
            selected = [p for p in selected if p.name.casefold() in wanted]

        overlaps = find_overlap_slots(
            poll,
            participant_names=[p.name for p in selected],
            min_participants=min_participants,
        )
        return {
            "title": poll.title,
            "timezone": timezone,
            "selected_participants": [p.name for p in selected],
            "match_count": len(overlaps),
            "matches": overlaps[:100],
            "truncated": len(overlaps) > 100,
            "warnings": [
                "Only participants who marked availability are included by default."
            ]
            if only_marked_participants
            else [],
        }


@mcp.tool()
def fill_from_weekly_schedule(
    url: str,
    name: str,
    busy_blocks: list[dict[str, str]],
    timezone: str = "America/New_York",
    password: str = "",
    buffer_minutes: int = 0,
    block_before: str | None = None,
    block_after: str | None = None,
    dry_run: bool = True,
    on_name_conflict: str = "error",
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Fill a poll from recurring weekly busy blocks (best for class schedules).

    Matches by weekday + local time from poll labels — no week_start_date needed.

    Args:
        url: when2meet poll URL
        name: Your name on the poll
        busy_blocks: List of {day, start, end} e.g. {"day":"Monday","start":"09:00","end":"10:15"}
        timezone: IANA timezone for slot labels
        password: Per-event password if returning to an existing name
        buffer_minutes: Expand each busy block by this many minutes before/after
        block_before: Daily cutoff like "10:00" — treat earlier slots as busy
        block_after: Daily cutoff like "18:00" — treat later slots as busy
        dry_run: Preview only (default true for safety)
        on_name_conflict: error | alternate_suffix | create_new
        agent_name: Model label for alternate name, e.g. Claude (default: WHEN2MEET_AGENT_NAME env)
    """
    blocks = [WeeklyBusyBlock.from_dict(item) for item in busy_blocks]
    ref = parse_poll_url(url)

    with When2MeetClient() as client:
        poll = client.get_poll(url, timezone=timezone)
        plan = compute_from_weekly_busy_blocks(
            poll,
            blocks,
            buffer_minutes=buffer_minutes,
            block_before=block_before,
            block_after=block_after,
        )

        matches = find_participant_by_name(poll, name)
        if matches:
            plan.warnings.append(
                f"Existing name matches: {', '.join(m['name'] for m in matches)}"
            )

        response = _plan_response(poll.title, plan, poll, {"name": name, "dry_run": dry_run})

        if dry_run:
            return response

        login = login_with_fallback(
            client,
            ref,
            poll,
            name,
            password,
            on_name_conflict=on_name_conflict,
            agent_name=agent_name,
        )
        bits = build_bitmask(poll, plan.free_indices)
        client.save_availability(
            ref,
            person_id=login.person_id,
            slot_ids=[slot.slot_id for slot in poll.slots],
            availability_bits=bits,
            password=password if login.name_used == name else "",
        )
        response.update(
            {
                "submitted": True,
                "person_id": login.person_id,
                "name_used": login.name_used,
                "warnings": plan.warnings + login.warnings,
            }
        )
        return response


@mcp.tool()
def compute_availability_from_busy_times(
    url: str,
    busy_times: list[dict[str, str]],
    timezone: str = "America/New_York",
    week_start_date: str | None = None,
    all_day_dates: list[str] | None = None,
    buffer_minutes: int = 0,
    block_before: str | None = None,
    block_after: str | None = None,
) -> dict[str, Any]:
    """Compute free slots from ISO calendar busy intervals.

    Args:
        url: when2meet poll URL
        busy_times: List of {start, end} ISO-8601 datetimes when busy
        timezone: IANA timezone for comparisons
        week_start_date: Date of column 0 (YYYY-MM-DD). Inferred from today if omitted.
        all_day_dates: Optional list of YYYY-MM-DD all-day busy dates
        buffer_minutes: Expand busy intervals by this many minutes
        block_before: Daily cutoff like "10:00"
        block_after: Daily cutoff like "18:00"
    """
    week_start = (
        date.fromisoformat(week_start_date)
        if week_start_date
        else infer_week_start_date()
    )
    intervals = [
        (_parse_iso_datetime(item["start"]), _parse_iso_datetime(item["end"]))
        for item in busy_times
    ]
    if all_day_dates:
        intervals.extend(expand_all_day_events(all_day_dates, timezone))

    with When2MeetClient() as client:
        poll = client.get_poll(url, timezone=timezone)
        plan = compute_from_busy_intervals(
            poll,
            week_start_date=week_start,
            busy_intervals=intervals,
            timezone=timezone,
            buffer_minutes=buffer_minutes,
            block_before=block_before,
            block_after=block_after,
        )
        return _plan_response(
            poll.title,
            plan,
            poll,
            {"week_start_date": week_start.isoformat(), "timezone": timezone},
        )


@mcp.tool()
def preview_availability(
    url: str,
    slot_indices: list[int],
    timezone: str = "America/New_York",
) -> dict[str, Any]:
    """Validate slot indices and preview availability before submitting.

    Args:
        url: when2meet poll URL
        slot_indices: Zero-based indices to mark available
        timezone: IANA timezone for labels
    """
    with When2MeetClient() as client:
        poll = client.get_poll(url, timezone=timezone)
        valid, invalid, warnings = validate_slot_indices(poll, slot_indices)
        bits = build_bitmask(poll, valid)
        if len(bits) != len(poll.slots):
            warnings.append("Bitmask length does not match poll slot count")

        return {
            "title": poll.title,
            "available_count": len(valid),
            "invalid_indices": invalid,
            "availability_length": len(bits),
            "bitmask_valid": len(bits) == len(poll.slots),
            "free_time_by_day": summarize_free_time_by_day(poll, set(valid)),
            "preview": [
                {
                    "index": poll.slots[i].index,
                    "label": poll.slots[i].label,
                }
                for i in valid[:50]
            ],
            "warnings": warnings,
        }


@mcp.tool()
def submit_availability(
    url: str,
    name: str,
    slot_indices: list[int],
    password: str = "",
    timezone: str = "America/New_York",
    dry_run: bool = True,
    on_name_conflict: str = "error",
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Sign in and mark availability on a when2meet poll.

    Args:
        url: when2meet poll URL
        name: Your name as it should appear on the poll
        slot_indices: Zero-based slot indices to mark available
        password: Per-event password for returning participants
        timezone: IANA timezone used when loading the poll
        dry_run: Preview only (default true for safety)
        on_name_conflict: error | alternate_suffix — when password fails for existing name
        agent_name: Model label for alternate name, e.g. Claude (default: WHEN2MEET_AGENT_NAME env)
    """
    ref = parse_poll_url(url)

    with When2MeetClient() as client:
        poll = client.get_poll(url, timezone=timezone)
        valid, invalid, warnings = validate_slot_indices(poll, slot_indices)
        availability_bits = build_bitmask(poll, valid)

        payload: dict[str, Any] = {
            "event_id": ref.event_id,
            "name": name,
            "slot_indices": valid,
            "invalid_indices": invalid,
            "available_count": len(valid),
            "availability_length": len(availability_bits),
            "free_time_by_day": summarize_free_time_by_day(poll, set(valid)),
            "warnings": warnings,
            "dry_run": dry_run,
        }

        if dry_run:
            return payload

        try:
            login = login_with_fallback(
                client,
                ref,
                poll,
                name,
                password,
                on_name_conflict=on_name_conflict,
                agent_name=agent_name,
            )
        except WrongPasswordError as exc:
            return {
                **payload,
                "submitted": False,
                "error": str(exc),
                "existing_matches": find_participant_by_name(poll, name),
            }

        client.save_availability(
            ref,
            person_id=login.person_id,
            slot_ids=[slot.slot_id for slot in poll.slots],
            availability_bits=availability_bits,
            password=password if login.name_used == name else "",
        )

        payload.update(
            {
                "submitted": True,
                "person_id": login.person_id,
                "name_used": login.name_used,
                "warnings": warnings + login.warnings,
            }
        )
        return payload


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
