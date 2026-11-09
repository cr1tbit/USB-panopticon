"""Config file (TOML): settings + first-match device rules. See default_config.toml."""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

DEFAULT = Path(__file__).with_name("default_config.toml")
PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "usb-panopticon" / "config.toml"
KINDS = ("mcu", "bridge", "probe", "serial", "ignore")


@dataclass
class Rule:
    match: str
    label: str
    kind: str
    mode: str = ""
    product: re.Pattern | None = None
    has: str = ""

    def matches(self, dev: dict) -> bool:
        if not fnmatchcase(f"{dev['vid']}:{dev['pid']}", self.match):
            return False
        if self.product and not self.product.search(dev.get("product", "")):
            return False
        if self.has == "tty" and not dev.get("ttys"):
            return False
        if self.has == "msc" and not dev.get("msc"):
            return False
        return True


@dataclass
class Config:
    settings: dict
    rules: list[Rule]

    def classify(self, dev: dict) -> Rule | None:
        return next((r for r in self.rules if r.matches(dev)), None)


def parse(text: str) -> Config:
    data = tomllib.loads(text)
    base = tomllib.loads(DEFAULT.read_text())["settings"]
    settings = {**base, **data.get("settings", {})}
    rules = []
    for i, r in enumerate(data.get("rule", [])):
        kind = r.get("kind", "mcu")
        if kind not in KINDS:
            raise ValueError(f"rule {i + 1}: kind must be one of {', '.join(KINDS)}")
        if "match" not in r:
            raise ValueError(f"rule {i + 1}: missing match")
        rules.append(Rule(match=str(r["match"]).lower(), label=str(r.get("label", "")), kind=kind,
                          mode=str(r.get("mode", "")), has=str(r.get("has", "")),
                          product=re.compile(r["product"], re.I) if r.get("product") else None))
    return Config(settings, rules)


def load() -> tuple[Config, str]:
    """Returns (config, error). On a broken user file the packaged default is used and the error reported."""
    if not PATH.exists():
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(DEFAULT.read_text())
    try:
        return parse(PATH.read_text()), ""
    except Exception as e:
        return parse(DEFAULT.read_text()), f"{PATH}: {e}"


def mtime() -> float:
    try:
        return PATH.stat().st_mtime
    except OSError:
        return 0.0
