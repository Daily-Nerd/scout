import pytest

from scout.models import normalize_repo


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Owner/Repo", "owner/repo"),
        ("owner/repo", "owner/repo"),
        ("https://github.com/Owner/Repo", "owner/repo"),
        ("https://github.com/Owner/Repo.git", "owner/repo"),
        ("github.com/Owner/Repo/", "owner/repo"),
        ("http://www.github.com/Owner/Repo.git/", "owner/repo"),
    ],
)
def test_normalize_repo(raw, expected):
    assert normalize_repo(raw) == expected


@pytest.mark.parametrize("raw", ["", "owneronly", "github.com/onepart"])
def test_normalize_repo_rejects_garbage(raw):
    with pytest.raises(ValueError):
        normalize_repo(raw)
