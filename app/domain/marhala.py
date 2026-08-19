"""Marhala definitions and the tasmee page counts they drive.

The marhala determines how many pages the student tasmee's to the Stage 1
Muhaffiz from the revised juz each day. It does NOT determine *which* pages --
that judgment stays with the Muhaffiz, by design. See `app/domain/tasmee.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

NIHAAI = 8  # stored as integer 8 so ordering/comparison stays trivial in SQL


@dataclass(frozen=True)
class Marhala:
    number: int
    key: str
    label: str
    tasmee_pages: int

    @property
    def is_final(self) -> bool:
        return self.number == NIHAAI

    @property
    def fraction_of_juz(self) -> str:
        """Tasmee load expressed against the 20-page juz, for the UI."""
        from app.domain.quran import PAGES_PER_JUZ

        if self.tasmee_pages * 4 == PAGES_PER_JUZ:
            return "¼ juz"
        if self.tasmee_pages * 2 == PAGES_PER_JUZ:
            return "½ juz"
        return f"{self.tasmee_pages}/{PAGES_PER_JUZ} juz"


MARAHIL: List[Marhala] = [
    Marhala(1, "m1", "Marhala 1", 5),
    Marhala(2, "m2", "Marhala 2", 5),
    Marhala(3, "m3", "Marhala 3", 5),
    Marhala(4, "m4", "Marhala 4", 7),
    Marhala(5, "m5", "Marhala 5", 7),
    Marhala(6, "m6", "Marhala 6", 10),
    Marhala(7, "m7", "Marhala 7", 10),
    Marhala(NIHAAI, "nihaai", "Marhala Nihaai", 10),
]

_BY_NUMBER: Dict[int, Marhala] = {m.number: m for m in MARAHIL}


class UnknownMarhala(ValueError):
    pass


def get_marhala(number: Optional[int]) -> Marhala:
    if number is None:
        return MARAHIL[0]
    m = _BY_NUMBER.get(int(number))
    if m is None:
        raise UnknownMarhala(f"no marhala numbered {number}")
    return m


def tasmee_page_target(marhala_number: Optional[int]) -> int:
    """Pages the student must tasmee from the revised juz, per the marhala."""
    return get_marhala(marhala_number).tasmee_pages


def marhala_choices() -> List[Marhala]:
    return list(MARAHIL)
