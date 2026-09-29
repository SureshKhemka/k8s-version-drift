import pytest

from drift.domain.versions import Minor, Version, parse_version


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("v1.31.4", "1.31.4"),
        ("1.31.4", "1.31.4"),
        ("v1.31.4-eks-abc123", "1.31.4"),
        ("v1.28.15-eks-7c1e2d4", "1.28.15"),
        ("v1.30.5-gke.1200", "1.30.5"),
        ("v1.29.3+k3s1", "1.29.3"),
        (" v1.3.0 ", "1.3.0"),
        ("v1.30.0", "1.30.0"),
    ],
)
def test_parseable(raw, expected):
    r = parse_version(raw)
    assert r.ok and str(r.parsed) == expected
    assert r.raw == raw.strip()


@pytest.mark.parametrize(
    "raw", ["", None, "unknown", "v1.33", "v1.x.3-eks-bad", "v1.34.x", "1.31", "v01.31.4", "1.31.4.5"]
)
def test_unparseable_keeps_raw(raw):
    r = parse_version(raw)
    assert not r.ok
    assert r.raw == (raw or "")


def test_look_alike_minors_are_distinct():
    a, b = parse_version("v1.3.0").parsed, parse_version("v1.30.0").parsed
    assert a.minor_key == "1.3" and b.minor_key == "1.30" and a < b


def test_ordering_and_minor():
    assert Version(1, 9, 9) < Version(1, 10, 0)
    assert str(Minor.parse("1.35")) == "1.35"
    with pytest.raises(ValueError):
        Minor.parse("1.35.1")
