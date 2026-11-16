"""Validate the numerical terms of an in-game sentence."""

from dataclasses import dataclass

from .ledger import MAX_AMOUNT

ARTICLE_MAX_LENGTH = 120
FINE_RANGE = (10, 100)
CUSTODY_RANGE = (1, 24)


@dataclass(frozen=True, slots=True)
class SanctionTerm:
    value: int
    article_minimum: int
    article_maximum: int

    def fits(self, season_range: tuple[int, int]) -> bool:
        if any(type(value) is not int for value in (self.value, self.article_minimum, self.article_maximum)):
            return False
        season_minimum, season_maximum = season_range
        return (
            0 <= self.article_minimum <= self.value <= self.article_maximum <= MAX_AMOUNT
            and season_minimum <= self.value <= season_maximum
        )


@dataclass(frozen=True, slots=True)
class CourtSentence:
    article: str
    fine: SanctionTerm | None = None
    custody: SanctionTerm | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.article, str):
            raise ValueError("invalid article")
        article = self.article.strip()
        if not 1 <= len(article) <= ARTICLE_MAX_LENGTH or not article.isprintable():
            raise ValueError("invalid article")
        if self.fine is None and self.custody is None:
            raise ValueError("sentence requires a fine or custody term")
        if self.fine is not None and not self.fine.fits(FINE_RANGE):
            raise ValueError("invalid fine")
        if self.custody is not None and not self.custody.fits(CUSTODY_RANGE):
            raise ValueError("invalid custody term")
        object.__setattr__(self, "article", article)
