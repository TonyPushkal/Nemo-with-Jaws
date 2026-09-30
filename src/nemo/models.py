"""Shared vocabulary for jobs. Persistence arrives in M2; these are the terms."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class Verification(str, Enum):
    VERIFIED_EMPLOYER = "verified_employer"  # found on the employer domain / its ATS
    VERIFIED_LIVE = "verified_live"          # page fetched and open, not on employer domain
    UNVERIFIED = "unverified"                # snippet-only lead (e.g. LinkedIn link from search)
    INACCESSIBLE = "inaccessible"


class JobStatus(str, Enum):
    OPEN = "open"
    POSSIBLY_CLOSED = "possibly_closed"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class SourceKind(str, Enum):
    """Where a piece of evidence came from."""

    PAGE_TEXT = "page_text"
    ATS_API = "ats_api"
    JSON_LD = "json_ld"
    SNIPPET = "snippet"


class DateBasis(str, Enum):
    STRUCTURED = "structured"                  # e.g. JSON-LD datePosted
    ATS_API = "ats_api"                        # e.g. Greenhouse first_published / updated_at
    ATS_LAST_PUBLISHED = "ats_last_published"  # e.g. Ashby publishedAt ("last published")
    PAGE_TEXT = "page_text"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class JobDates:
    """Posting date and update date are separate and never copied into each other."""

    posted_at: date | None = None
    posted_basis: DateBasis = DateBasis.UNKNOWN
    updated_at: date | None = None
    updated_basis: DateBasis = DateBasis.UNKNOWN
