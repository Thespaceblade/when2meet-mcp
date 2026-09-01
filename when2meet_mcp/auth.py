"""Login helpers and typed errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from when2meet_mcp.agent import agent_display_name, alternate_participant_name
from when2meet_mcp.client import Poll, PollRef, When2MeetClient
from when2meet_mcp.scheduling import find_participant_by_name


class When2MeetError(Exception):
    """Base error for when2meet operations."""


class LoginError(When2MeetError):
    """Could not authenticate to the poll."""


class WrongPasswordError(LoginError):
    """Name exists but password is incorrect."""


@dataclass
class LoginResult:
    person_id: int
    name_used: str
    warnings: list[str] = field(default_factory=list)
    existing_matches: list[dict[str, Any]] = field(default_factory=list)


def login_with_fallback(
    client: When2MeetClient,
    ref: PollRef,
    poll: Poll,
    name: str,
    password: str = "",
    on_name_conflict: str = "error",
    agent_name: str | None = None,
) -> LoginResult:
    """Login to poll, handling duplicate names and password conflicts.

    on_name_conflict:
      - error: raise WrongPasswordError with existing participant info
      - alternate_suffix: use \"{name} ({agent})\" when password fails
      - create_new: always create/login with exact name (may duplicate)

    agent_name: Override for attribution label (default: WHEN2MEET_AGENT_NAME env or Cursor)
    """
    matches = find_participant_by_name(poll, name)
    exact_matches = [m for m in matches if m["exact_match"]]

    try:
        person_id = client.login(ref, name=name, password=password)
        warnings: list[str] = []
        if exact_matches and exact_matches[0]["person_id"] != person_id:
            warnings.append(
                f"Logged in as new participant {person_id}; "
                f"existing '{name}' entries: "
                + ", ".join(str(m["person_id"]) for m in exact_matches)
            )
        return LoginResult(
            person_id=person_id,
            name_used=name,
            warnings=warnings,
            existing_matches=matches,
        )
    except ValueError as exc:
        message = str(exc)
        if "Wrong password" not in message:
            raise LoginError(message) from exc

        if on_name_conflict == "alternate_suffix":
            alternate = alternate_participant_name(name, agent_name)
            person_id = client.login(ref, name=alternate, password="")
            return LoginResult(
                person_id=person_id,
                name_used=alternate,
                warnings=[
                    f"Password required for existing '{name}' entry. "
                    f"Submitted under '{alternate}' instead."
                ],
                existing_matches=matches,
            )

        if on_name_conflict == "create_new":
            raise WrongPasswordError(
                f"Wrong password for existing participant '{name}'. "
                f"Provide the correct password or set on_name_conflict='alternate_suffix'."
            ) from exc

        hint = (
            f"Participant '{name}' already exists (person_id="
            f"{exact_matches[0]['person_id'] if exact_matches else 'unknown'}). "
            "Provide the event password, or set on_name_conflict='alternate_suffix'."
        )
        raise WrongPasswordError(hint) from exc
