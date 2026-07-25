"""Central data directory configuration.

Provides a single source of truth for the user data directory path.
On first run, migrates legacy ~/.atorch/ data to the OS-standard location.
Users can override the directory via Preferences > Database.
"""

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QStandardPaths


# App name used for QStandardPaths
_APP_NAME = "Load Test Bench"

# Cached data directory (set on first call to get_data_dir())
_data_dir: Optional[Path] = None


def _bootstrap_dir() -> Path:
    """Return the fixed bootstrap config directory (never moves).

    macOS:   ~/Library/Application Support/Load Test Bench/
    Windows: C:/Users/<user>/AppData/Local/Load Test Bench/
    Linux:   ~/.local/share/Load Test Bench/
    """
    path = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    if not path:
        # Fallback if QStandardPaths fails
        return Path.home() / ".atorch"
    # QStandardPaths may return the app-specific path already, but on macOS
    # it returns ~/Library/Application Support/<AppName> based on QApplication name.
    # Since we may not have set the app name, use our own subdirectory.
    result = Path(path).parent / _APP_NAME
    result.mkdir(parents=True, exist_ok=True)
    return result


def _config_file() -> Path:
    """Return path to the bootstrap config.json."""
    return _bootstrap_dir() / "config.json"


def _read_config() -> dict:
    """Read the bootstrap config file."""
    cf = _config_file()
    if cf.exists():
        try:
            return json.loads(cf.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_config(config: dict) -> None:
    """Write the bootstrap config file."""
    cf = _config_file()
    cf.parent.mkdir(parents=True, exist_ok=True)
    cf.write_text(json.dumps(config, indent=2))


def get_default_data_dir() -> Path:
    """Return the OS-standard default data directory."""
    return _bootstrap_dir()


def get_data_dir() -> Path:
    """Return the current data directory, creating it if needed.

    Resolution order:
    1. Cached value (if already resolved this session)
    2. Custom path from config.json
    3. OS-standard default (with legacy migration if needed)
    """
    global _data_dir
    if _data_dir is not None:
        return _data_dir

    config = _read_config()
    custom = config.get("data_dir")

    if custom:
        _data_dir = Path(custom)
    else:
        _data_dir = get_default_data_dir()
        _migrate_legacy(_data_dir)

    # Ensure directory and common subdirectories exist
    _data_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("sessions", "test_data",
                "presets/battery_presets", "presets/test_presets",
                "presets/battery_load_presets", "presets/charger_presets",
                "presets/battery_charger_presets",
                "presets/battery_charger_test_presets",
                "presets/power_bank_presets",
                "presets/power_bank_test_presets"):
        (_data_dir / sub).mkdir(parents=True, exist_ok=True)

    return _data_dir


def set_data_dir(path: Optional[Path]) -> None:
    """Set a custom data directory (written to config.json).

    Pass None to reset to default.
    """
    global _data_dir
    config = _read_config()

    if path is None:
        config.pop("data_dir", None)
    else:
        config["data_dir"] = str(path)

    _write_config(config)
    # Clear cache so next call to get_data_dir() re-resolves
    _data_dir = None


def _migrate_legacy(target: Path) -> None:
    """Copy legacy ~/.atorch/ contents to target if needed.

    Only migrates if:
    - ~/.atorch/ exists
    - target does not yet contain tests.db (fresh install)
    - target != ~/.atorch/ (avoid copying to self)
    """
    legacy = Path.home() / ".atorch"
    if not legacy.exists():
        return
    if legacy.resolve() == target.resolve():
        return
    if (target / "tests.db").exists():
        return

    target.mkdir(parents=True, exist_ok=True)
    for item in legacy.iterdir():
        dest = target / item.name
        if dest.exists():
            continue
        try:
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
        except OSError:
            # Best-effort migration — skip files that fail
            pass


@dataclass
class MeterSettings:
    """Optional SCPI voltage-meter connection settings."""

    enabled: bool = False
    transport: str = "usb"  # "usb" | "lan"
    serial_port: str = ""
    host: str = ""
    lan_port: int = 5555
    profile_key: str = "hds200"
    use_for_cutoff: bool = False


def load_meter_settings(settings_path) -> MeterSettings:
    """Read the 'meter' key of settings.json, merged over defaults."""
    defaults = MeterSettings()
    try:
        with open(settings_path) as f:
            stored = json.load(f).get("meter", {})
    except (OSError, json.JSONDecodeError):
        return defaults
    merged = {**asdict(defaults), **stored}
    # Only keep known fields so a stale/foreign key can't break construction.
    known = {f: merged[f] for f in asdict(defaults) if f in merged}
    return MeterSettings(**known)


def save_meter_settings(settings_path, settings: MeterSettings) -> None:
    """Write settings under the 'meter' key, preserving other top-level keys."""
    data = {}
    try:
        with open(settings_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    data["meter"] = asdict(settings)
    try:
        with open(settings_path, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass
