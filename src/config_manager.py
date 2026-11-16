from __future__ import annotations

import json
from pathlib import Path


class ConfigManager:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self):
        default = self.default_config()

        if not self.path.exists():
            return default

        try:
            with self.path.open("r", encoding="utf-8") as f:
                incoming = json.load(f)
        except (OSError, json.JSONDecodeError):
            return default

        self._merge(default, incoming)
        return default

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self.path)

    @staticmethod
    def default_config():
        return {
            "bo": {
                "username": "",
                "password": "",
                "port": 22,
                "connect_timeout": 5,
                "operation_timeout": 60,
            },
            "counter": {
                "username": "",
                "password": "",
                "port": 22,
                "connect_timeout": 5,
                "operation_timeout": 60,
            },
            "ip_generation": {
                "counter_start": 112,
            },
            "ui": {
                "window_width": 1200,
                "window_height": 780,
            },
        }

    @staticmethod
    def _merge(base, incoming):
        for key, value in incoming.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                ConfigManager._merge(base[key], value)
            else:
                base[key] = value
