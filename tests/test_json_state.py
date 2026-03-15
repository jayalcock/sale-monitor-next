import json
from pathlib import Path
import pytest
from sale_monitor.storage.json_state import load_state, save_state, prune_stale_entries


def test_load_state_missing_file():
    """Test loading from a non-existent file returns empty dict."""
    result = load_state("non_existent_file.json")
    assert result == {}


def test_load_state_empty_file(tmp_path):
    """Test loading from an empty file returns empty dict."""
    file_path = tmp_path / "empty.json"
    file_path.write_text("", encoding="utf-8")
    
    result = load_state(str(file_path))
    assert result == {}


def test_load_state_invalid_json(tmp_path):
    """Test loading invalid JSON returns empty dict."""
    file_path = tmp_path / "invalid.json"
    file_path.write_text("{invalid json", encoding="utf-8")
    
    result = load_state(str(file_path))
    assert result == {}


def test_save_and_load_state(tmp_path):
    """Test saving and loading state data."""
    file_path = tmp_path / "state.json"
    test_data = {
        "https://example.com/product": {
            "name": "Test Product",
            "current_price": 99.99,
            "last_checked": "2025-10-30T12:00:00",
        }
    }
    
    save_state(str(file_path), test_data)
    
    # Verify file exists
    assert file_path.exists()
    
    # Load and verify
    loaded = load_state(str(file_path))
    assert loaded == test_data


def test_save_state_creates_directory(tmp_path):
    """Test that save_state creates parent directories if needed."""
    nested_path = tmp_path / "nested" / "dir" / "state.json"
    test_data = {"key": "value"}
    
    save_state(str(nested_path), test_data)
    
    assert nested_path.exists()
    loaded = load_state(str(nested_path))
    assert loaded == test_data


def test_save_state_overwrites_existing(tmp_path):
    """Test that save_state overwrites existing file atomically."""
    file_path = tmp_path / "state.json"
    
    # Write initial data
    initial_data = {"old": "data"}
    save_state(str(file_path), initial_data)
    
    # Overwrite with new data
    new_data = {"new": "data", "more": "fields"}
    save_state(str(file_path), new_data)
    
    # Verify only new data exists
    loaded = load_state(str(file_path))
    assert loaded == new_data
    assert "old" not in loaded


def test_save_state_json_formatting(tmp_path):
    """Test that saved JSON is properly formatted (indented, sorted)."""
    file_path = tmp_path / "state.json"
    test_data = {"z_key": "last", "a_key": "first", "m_key": "middle"}
    
    save_state(str(file_path), test_data)
    
    # Read raw file content
    content = file_path.read_text(encoding="utf-8")
    
    # Verify it's indented (not minified)
    assert "\n" in content
    assert "  " in content  # 2-space indent
    
    # Verify keys are sorted (a_key should appear before z_key)
    assert content.index("a_key") < content.index("z_key")


# ---- Pruning tests ----

def test_prune_removes_stale_entries(tmp_path):
    file_path = tmp_path / "state.json"
    state = {
        "https://keep.com/1": {"current_price": 10},
        "https://stale.com/2": {"current_price": 20},
        "https://keep.com/3": {"current_price": 30},
    }
    save_state(str(file_path), state)

    removed = prune_stale_entries(
        str(file_path),
        active_urls={"https://keep.com/1", "https://keep.com/3"},
    )
    assert removed == 1

    after = load_state(str(file_path))
    assert "https://stale.com/2" not in after
    assert "https://keep.com/1" in after
    assert "https://keep.com/3" in after


def test_prune_caps_identifiers(tmp_path):
    file_path = tmp_path / "state.json"
    big_ids = {f"key_{i}": f"val_{i}" for i in range(60)}
    state = {"https://a.com": {"identifiers": big_ids}}
    save_state(str(file_path), state)

    prune_stale_entries(str(file_path), active_urls={"https://a.com"}, max_identifiers=50)

    after = load_state(str(file_path))
    assert len(after["https://a.com"]["identifiers"]) == 50


def test_prune_no_stale_entries(tmp_path):
    file_path = tmp_path / "state.json"
    state = {"https://a.com": {"current_price": 5}}
    save_state(str(file_path), state)

    removed = prune_stale_entries(str(file_path), active_urls={"https://a.com"})
    assert removed == 0