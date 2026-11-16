import pytest

from civitas.domain.court_sanctions import CourtSentence, SanctionTerm


@pytest.mark.parametrize("fine,hours", [(10, 1), (100, 24)])
def test_sentence_accepts_inclusive_season_and_article_limits(fine, hours):
    sentence = CourtSentence(" Article 7 ", SanctionTerm(fine, fine, fine), SanctionTerm(hours, 0, 48))

    assert sentence.article == "Article 7"
    assert sentence.fine.value == fine
    assert sentence.custody.article_maximum == 48


@pytest.mark.parametrize(
    "kind,value,minimum,maximum",
    [
        ("fine", 9, 0, 100),
        ("fine", 101, 0, 200),
        ("fine", 25, 26, 50),
        ("fine", 50, 20, 49),
        ("fine", 50, 70, 30),
        ("fine", 50.0, 20, 80),
        ("custody", True, 0, 24),
        ("custody", 25, 1, 48),
        ("custody", 0, 0, 24),
    ],
)
def test_sentence_rejects_terms_outside_either_set_of_limits(kind, value, minimum, maximum):
    with pytest.raises(ValueError, match="invalid"):
        CourtSentence("Article 7", **{kind: SanctionTerm(value, minimum, maximum)})


@pytest.mark.parametrize("article", ["", "   ", "A" * 121, "7\n8"])
def test_sentence_requires_single_printable_article_reference(article):
    with pytest.raises(ValueError, match="invalid article"):
        CourtSentence(article, fine=SanctionTerm(25, 10, 30))


def test_sentence_requires_at_least_one_sanction():
    with pytest.raises(ValueError, match="requires"):
        CourtSentence("Article 7")
