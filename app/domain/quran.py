"""Mushaf geometry.

The Tayseer guide prescribes the Misri (Othman Taha) mushaf specifically because
"each siparah has a fixed number of 20 pages, each page ends on an ayat".

That fixed 20-pages-per-juz geometry is load-bearing for this app: it is what
makes the marhala tasmee counts land on clean fractions of a juz (5 pages = 1/4
juz, 10 pages = 1/2 juz), and it is why page <-> juz math here is arithmetic
rather than a lookup table. Do NOT swap in the 604-page Madani pagination
without revisiting `app/domain/marhala.py` as well.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List

PAGES_PER_JUZ = 20
JUZ_COUNT = 30
TOTAL_PAGES = PAGES_PER_JUZ * JUZ_COUNT  # 600

# Traditional names, taken from the opening words of each juz.
JUZ_NAMES: List[str] = [
    "Alif Lam Mim",
    "Sayaqul",
    "Tilkar Rusul",
    "Lan Tanalu",
    "Wal Muhsanat",
    "La Yuhibbullah",
    "Wa Idha Sami'u",
    "Wa Lau Annana",
    "Qalal Mala'u",
    "Wa'lamu",
    "Ya'tadhiruna",
    "Wa Ma Min Dabbah",
    "Wa Ma Ubarri'u",
    "Rubama",
    "Subhanalladhi",
    "Qala Alam",
    "Iqtaraba",
    "Qad Aflaha",
    "Wa Qalalladhina",
    "Amman Khalaqa",
    "Utlu Ma Uhiya",
    "Wa Manyaqnut",
    "Wa Mali",
    "Faman Azlam",
    "Ilaihi Yuraddu",
    "Ha Mim",
    "Qala Fama Khatbukum",
    "Qad Sami Allah",
    "Tabarakalladhi",
    "Amma Yatasa'alun",
]


class PageOutOfRange(ValueError):
    """Raised when a page number falls outside 1..600."""


class JuzOutOfRange(ValueError):
    """Raised when a juz number falls outside 1..30."""


def validate_page(page: int) -> int:
    if not isinstance(page, int) or isinstance(page, bool):
        raise PageOutOfRange(f"page must be an int, got {page!r}")
    if page < 1 or page > TOTAL_PAGES:
        raise PageOutOfRange(f"page {page} outside 1..{TOTAL_PAGES}")
    return page


def validate_juz(juz: int) -> int:
    if not isinstance(juz, int) or isinstance(juz, bool):
        raise JuzOutOfRange(f"juz must be an int, got {juz!r}")
    if juz < 1 or juz > JUZ_COUNT:
        raise JuzOutOfRange(f"juz {juz} outside 1..{JUZ_COUNT}")
    return juz


def juz_of_page(page: int) -> int:
    """Global page number (1..600) -> juz number (1..30)."""
    validate_page(page)
    return (page - 1) // PAGES_PER_JUZ + 1


def page_index_in_juz(page: int) -> int:
    """Global page number -> its 1..20 position inside its own juz."""
    validate_page(page)
    return (page - 1) % PAGES_PER_JUZ + 1


def global_page(juz: int, index_in_juz: int) -> int:
    """(juz, 1..20) -> global page number."""
    validate_juz(juz)
    if index_in_juz < 1 or index_in_juz > PAGES_PER_JUZ:
        raise PageOutOfRange(f"index_in_juz {index_in_juz} outside 1..{PAGES_PER_JUZ}")
    return (juz - 1) * PAGES_PER_JUZ + index_in_juz


def juz_page_range(juz: int) -> range:
    """All global page numbers belonging to `juz`."""
    validate_juz(juz)
    start = (juz - 1) * PAGES_PER_JUZ + 1
    return range(start, start + PAGES_PER_JUZ)


def juz_pages(juz: int) -> List[int]:
    return list(juz_page_range(juz))


def juz_name(juz: int) -> str:
    validate_juz(juz)
    return JUZ_NAMES[juz - 1]


@dataclass(frozen=True)
class JuzInfo:
    number: int
    name: str
    first_page: int
    last_page: int

    @property
    def pages(self) -> range:
        return range(self.first_page, self.last_page + 1)


def juz_info(juz: int) -> JuzInfo:
    rng = juz_page_range(juz)
    return JuzInfo(number=juz, name=juz_name(juz), first_page=rng.start, last_page=rng.stop - 1)


def all_juz() -> Iterator[JuzInfo]:
    for n in range(1, JUZ_COUNT + 1):
        yield juz_info(n)


def label_page(page: int) -> str:
    """Human label a Muhaffiz can read out loud: 'Juz 3 p.7 (global 47)'."""
    return f"Juz {juz_of_page(page)} p.{page_index_in_juz(page)}"
