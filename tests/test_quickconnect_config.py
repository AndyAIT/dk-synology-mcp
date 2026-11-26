import pytest

from synology_mcp.utils.config import normalize_quickconnect_id


@pytest.mark.parametrize(
    "value",
    ["myid", "myid.quickconnect.to", "https://myid.sg3.quickconnect.to/", "myid.quickconnect.to:443"],
)
def test_normalize_extracts_bare_id(value):
    assert normalize_quickconnect_id(value) == "myid"


def test_normalize_rejects_invalid():
    with pytest.raises(ValueError):
        normalize_quickconnect_id("bad id!")
