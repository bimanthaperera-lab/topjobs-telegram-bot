import pytest

from jobbot.normalize import normalize_keyword


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Accountant", "accountant"),
        ("  Software Engineer  ", "software engineer"),
        ("DATA\t  Analyst\n", "data analyst"),
        ("ගණකාධිකාරී", "ගණකාධිකාරී"),
        ("", ""),
        ("   ", ""),
        (None, ""),
    ],
)
def test_normalize_keyword(raw, expected):
    assert normalize_keyword(raw) == expected


def test_equivalent_keywords_share_cache_key():
    assert normalize_keyword(" Accountant") == normalize_keyword("accountant ")
