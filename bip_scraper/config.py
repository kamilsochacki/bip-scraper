"""Wczytanie konfiguracji z config.yaml."""
from pathlib import Path

import yaml


def load_config(config_path: str | Path | None = None) -> dict:
    path = Path(config_path or "config.yaml")
    if not path.is_file():
        raise FileNotFoundError(
            f"Brak pliku konfiguracji: {path}. Skopiuj config.example.yaml do config.yaml."
        )
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
