import pytest

from synology_mcp.utils.config import load_config


def test_loads_nas_from_env_file(tmp_path, monkeypatch):
    f = tmp_path / "synology.env"
    f.write_text("SYNOLOGY_NAS1_HOST=nas.example.test\nSYNOLOGY_NAS1_PASSWORD=pw\nSYNOLOGY_NAS1_NAME=lab\n")
    for k in ("SYNOLOGY_NAS1_HOST", "SYNOLOGY_NAS1_QUICKCONNECT", "SYNOLOGY_NAS1_PASSWORD", "SYNOLOGY_NAS1_NAME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SYNOLOGY_ENV_FILE", str(f))
    assert "lab" in load_config().nas_names


def test_missing_env_file_raises(monkeypatch):
    monkeypatch.setenv("SYNOLOGY_ENV_FILE", "/nonexistent/synology.env")
    with pytest.raises(FileNotFoundError):
        load_config()
