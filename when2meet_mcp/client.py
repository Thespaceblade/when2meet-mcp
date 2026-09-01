"""When2meet HTTP client (reverse-engineered API, no official docs)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx

BASE_URL = "https://www.when2meet.com"
SLOT_STEP_SECONDS = 900
SHOW_SLOT_RE = re.compile(r'ShowSlot\((\d+),"([^"]+)"\)')
TIME_OF_SLOT_RE = re.compile(r"TimeOfSlot\[(\d+)\]=(\d+);")
PEOPLE_RE = re.compile(
    r"PeopleNames\[(\d+)\] = '([^']*)';PeopleIDs\[\1\] = (\d+);"
)
AVAIL_PUSH_RE = re.compile(r"AvailableAtSlot\[(\d+)\]\.push\((\d+)\);")
TITLE_RE = re.compile(r"<title>([^<]+)</title>")


@dataclass(frozen=True)
class PollRef:
    event_id: str
    code: str

    @property
    def slug(self) -> str:
        return f"{self.event_id}-{self.code}"


@dataclass
class Slot:
    index: int
    slot_id: int
    label: str
    column: int
    row: int


@dataclass
class Participant:
    name: str
    person_id: int
    available_slot_indices: list[int] = field(default_factory=list)


@dataclass
class Poll:
    ref: PollRef
    title: str
    timezone: str
    slots: list[Slot]
    participants: list[Participant]
    rows_per_day: int
    event_timezone: str = "UTC"
    first_slot_time: time | None = None


def parse_poll_url(url: str) -> PollRef:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    slug = query.get("", [None])[0]
    if slug is None:
        # when2meet uses ?38093485-OZAlv with an empty query key
        if parsed.query and "=" not in parsed.query:
            slug = parsed.query
        else:
            parts = [p for p in parsed.path.split("/") if p]
            if parts:
                slug = parts[-1]

    if not slug or "-" not in slug:
        raise ValueError(
            f"Could not parse when2meet poll URL: {url!r}. "
            "Expected format: https://www.when2meet.com/?<event_id>-<code>"
        )

    event_id, code = slug.split("-", 1)
    if not event_id.isdigit() or not code:
        raise ValueError(f"Invalid when2meet poll slug: {slug!r}")

    return PollRef(event_id=event_id, code=code)


def _parse_title(html: str) -> str:
    match = TITLE_RE.search(html)
    if not match:
        return "When2meet poll"
    title = match.group(1).removesuffix(" - When2meet").strip()
    return title or "When2meet poll"


def _parse_slot_ids(html: str) -> list[int]:
    pairs = sorted(
        ((int(index), int(slot_id)) for index, slot_id in TIME_OF_SLOT_RE.findall(html)),
        key=lambda item: item[0],
    )
    if not pairs:
        raise ValueError("No TimeOfSlot entries found in poll HTML")
    return [slot_id for _, slot_id in pairs]


def _parse_show_slots(html: str) -> dict[int, str]:
    return {int(slot_id): label for slot_id, label in SHOW_SLOT_RE.findall(html)}


def _parse_grid_positions(html: str) -> dict[int, tuple[int, int]]:
    positions: dict[int, tuple[int, int]] = {}
    pattern = re.compile(
        r"id='(?:You|Group)Time(\d+)' data-col=\"(\d+)\" data-row=\"(\d+)\""
    )
    for slot_id, col, row in pattern.findall(html):
        positions[int(slot_id)] = (int(col), int(row))
    return positions


def _rows_per_day(slot_ids: list[int]) -> int:
    if len(slot_ids) < 2:
        return 1
    count = 1
    for index in range(1, len(slot_ids)):
        if slot_ids[index] - slot_ids[index - 1] == SLOT_STEP_SECONDS:
            count += 1
            continue
        return count
    return count


def _parse_show_slot_label(label: str) -> tuple[str, time]:
    # Example: "Sunday 09:00:00 AM"
    day_name, clock = label.split(" ", 1)
    parsed = datetime.strptime(clock, "%I:%M:%S %p")
    return day_name, parsed.time()


def _bits_to_indices(bits: str) -> list[int]:
    return [index for index, bit in enumerate(bits) if bit == "1"]


def _indices_to_bits(length: int, indices: list[int]) -> str:
    bits = ["0"] * length
    for index in indices:
        if 0 <= index < length:
            bits[index] = "1"
    return "".join(bits)


class When2MeetClient:
    def __init__(self, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"User-Agent": "when2meet-mcp/1.0"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> When2MeetClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def fetch_poll_page(self, ref: PollRef) -> str:
        response = self._client.get(f"/?{ref.slug}")
        response.raise_for_status()
        return response.text

    def fetch_availability_grids(self, ref: PollRef, timezone: str) -> str:
        response = self._client.post(
            "/AvailabilityGrids.php",
            data={
                ref.slug: "",
                "id": ref.event_id,
                "code": ref.code,
                "participantTimeZone": timezone,
            },
        )
        response.raise_for_status()
        return response.text

    def get_poll(self, url: str, timezone: str = "America/New_York") -> Poll:
        ref = parse_poll_url(url)
        page_html = self.fetch_poll_page(ref)
        grids_html = self.fetch_availability_grids(ref, timezone)
        grids_utc_html = self.fetch_availability_grids(ref, "UTC")

        title = _parse_title(page_html)
        slot_ids = _parse_slot_ids(page_html)
        labels = _parse_show_slots(grids_html)
        labels_utc = _parse_show_slots(grids_utc_html)
        positions = _parse_grid_positions(grids_html)
        rows_per_day = _rows_per_day(slot_ids)

        slots: list[Slot] = []
        for index, slot_id in enumerate(slot_ids):
            col, row = positions.get(slot_id, (index // rows_per_day, index % rows_per_day))
            slots.append(
                Slot(
                    index=index,
                    slot_id=slot_id,
                    label=labels.get(slot_id, f"slot {slot_id}"),
                    column=col,
                    row=row,
                )
            )

        participants = self._parse_participants(page_html, len(slot_ids))

        first_label = labels_utc.get(slot_ids[0])
        first_slot_time = None
        if first_label:
            _, first_slot_time = _parse_show_slot_label(first_label)

        return Poll(
            ref=ref,
            title=title,
            timezone=timezone,
            slots=slots,
            participants=participants,
            rows_per_day=rows_per_day,
            event_timezone="UTC",
            first_slot_time=first_slot_time,
        )

    def _parse_participants(self, html: str, slot_count: int) -> list[Participant]:
        people = [
            (name, int(person_id))
            for _, name, person_id in PEOPLE_RE.findall(html)
        ]
        # Deduplicate while preserving order
        seen: set[int] = set()
        unique_people: list[tuple[str, int]] = []
        for name, person_id in people:
            if person_id in seen:
                continue
            seen.add(person_id)
            unique_people.append((name, person_id))

        grouped: dict[int, list[int]] = {}
        for slot_index, person_id in AVAIL_PUSH_RE.findall(html):
            grouped.setdefault(int(person_id), []).append(int(slot_index))

        return [
            Participant(
                name=name,
                person_id=person_id,
                available_slot_indices=sorted(grouped.get(person_id, [])),
            )
            for name, person_id in unique_people
        ]

    def login(self, ref: PollRef, name: str, password: str = "") -> int:
        response = self._client.post(
            "/ProcessLogin.php",
            data={"id": ref.event_id, "name": name, "password": password},
        )
        response.raise_for_status()
        text = response.text.strip()
        if not text.isdigit():
            raise ValueError(f"Login failed: {text}")
        return int(text)

    def save_availability(
        self,
        ref: PollRef,
        person_id: int,
        slot_ids: list[int],
        availability_bits: str,
        password: str = "",
        change_to_available: bool = True,
    ) -> None:
        response = self._client.post(
            "/SaveTimes.php",
            data={
                "person": str(person_id),
                "event": ref.event_id,
                "slots": ",".join(str(slot_id) for slot_id in slot_ids),
                "availability": availability_bits,
                "password": password,
                "ChangeToAvailable": "true" if change_to_available else "false",
            },
        )
        response.raise_for_status()


def slot_window(
    poll: Poll,
    slot: Slot,
    week_start_date: date,
    tz: ZoneInfo,
) -> tuple[datetime, datetime]:
    """Map a poll slot to an absolute [start, end) window in tz."""
    if poll.first_slot_time is None:
        raise ValueError("Poll is missing event time anchor")

    event_tz = ZoneInfo(poll.event_timezone)
    day = week_start_date + timedelta(days=slot.column)
    start = datetime.combine(day, poll.first_slot_time, tzinfo=event_tz)
    start += timedelta(minutes=15 * slot.row)
    end = start + timedelta(minutes=15)

    return start.astimezone(tz), end.astimezone(tz)


def compute_free_slot_indices(
    poll: Poll,
    week_start_date: date,
    busy_intervals: list[tuple[datetime, datetime]],
    timezone: str,
    block_before: str | None = None,
    block_after: str | None = None,
) -> list[int]:
    """Return slot indices that are free given busy intervals and optional daily bounds."""
    tz = ZoneInfo(timezone)
    normalized_busy = [
        (start.astimezone(tz), end.astimezone(tz)) for start, end in busy_intervals
    ]

    block_before_time = (
        datetime.strptime(block_before, "%H:%M").time() if block_before else None
    )
    block_after_time = (
        datetime.strptime(block_after, "%H:%M").time() if block_after else None
    )

    free_indices: list[int] = []
    for slot in poll.slots:
        start, end = slot_window(poll, slot, week_start_date, tz)

        if block_before_time and start.time() < block_before_time:
            continue
        if block_after_time and end.time() > block_after_time:
            continue

        overlaps = any(start < busy_end and end > busy_start for busy_start, busy_end in normalized_busy)
        if not overlaps:
            free_indices.append(slot.index)

    return free_indices


def find_overlap_slots(
    poll: Poll,
    participant_names: list[str] | None = None,
    min_participants: int | None = None,
) -> list[dict[str, Any]]:
    selected = poll.participants
    if participant_names:
        wanted = {name.casefold() for name in participant_names}
        selected = [p for p in poll.participants if p.name.casefold() in wanted]

    if not selected:
        return []

    threshold = min_participants or len(selected)
    slot_count = len(poll.slots)
    counts = [0] * slot_count
    names_by_slot: list[list[str]] = [[] for _ in range(slot_count)]

    for participant in selected:
        for index in participant.available_slot_indices:
            counts[index] += 1
            names_by_slot[index].append(participant.name)

    results: list[dict[str, Any]] = []
    for slot in poll.slots:
        if counts[slot.index] < threshold:
            continue
        results.append(
            {
                "slot_index": slot.index,
                "slot_id": slot.slot_id,
                "label": slot.label,
                "available_count": counts[slot.index],
                "available_names": names_by_slot[slot.index],
            }
        )
    return results


def poll_to_dict(poll: Poll, include_slots: bool = True) -> dict[str, Any]:
    from when2meet_mcp.scheduling import poll_local_time_range, unmarked_participants

    data: dict[str, Any] = {
        "title": poll.title,
        "event_id": poll.ref.event_id,
        "code": poll.ref.code,
        "url": f"{BASE_URL}/?{poll.ref.slug}",
        "timezone": poll.timezone,
        "event_timezone": poll.event_timezone,
        "slot_count": len(poll.slots),
        "rows_per_day": poll.rows_per_day,
        "day_count": len(poll.slots) // poll.rows_per_day,
        "poll_time_range": poll_local_time_range(poll),
        "participants": [
            {
                "name": participant.name,
                "person_id": participant.person_id,
                "available_slot_count": len(participant.available_slot_indices),
                "has_marked_availability": bool(participant.available_slot_indices),
            }
            for participant in poll.participants
        ],
        "unmarked_participants": unmarked_participants(poll),
    }
    if include_slots:
        data["slots"] = [
            {
                "index": slot.index,
                "slot_id": slot.slot_id,
                "label": slot.label,
                "column": slot.column,
                "row": slot.row,
            }
            for slot in poll.slots
        ]
    return data
