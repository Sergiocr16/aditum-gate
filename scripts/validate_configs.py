#!/usr/bin/env python3
"""Valida config-default.json y examples/*.json contra config.schema.json.
Usado por el CI y utilizable a mano: python3 scripts/validate_configs.py"""
import json
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]


def main():
    with open(ROOT / "config.schema.json") as f:
        schema = json.load(f)

    targets = [ROOT / "config-default.json"] + sorted((ROOT / "examples").glob("*.json"))
    failures = 0
    for path in targets:
        with open(path) as f:
            try:
                config = json.load(f)
                jsonschema.validate(config, schema)
                print(f"OK   {path.relative_to(ROOT)}")
            except (ValueError, jsonschema.ValidationError) as e:
                print(f"FAIL {path.relative_to(ROOT)}: {getattr(e, 'message', e)}")
                failures += 1
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
