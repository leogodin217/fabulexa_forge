#!/usr/bin/env python
"""
Demo: Duplicate-key config loading refuses silent last-wins YAML.

Loads a clean stream config, then one with a duplicate `content` key at the
top level. The clean config loads normally; the duplicate-key one fails with
a ConfigError naming the file, key, and line — instead of silently resolving
to the last value, as bare `yaml.safe_load` would.

Sprint: author-selectable-identity
Phase: 1
"""

import tempfile
from pathlib import Path

from fabulexa_forge.config.loader import load_stream_config
from fabulexa_forge.errors import ConfigError

CLEAN_STREAM_CONFIG = """
content: state-changes
streams:
  - name: patients
    kind: patient
    properties:
      - name
      - status
"""

DUPLICATE_KEY_STREAM_CONFIG = """
content: state-changes
streams:
  - name: patients
    kind: patient
    properties:
      - name
      - status
    keys: [record_index]
    keys: [presentation_id]
"""


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        clean_path = tmp_dir / "clean_stream.yaml"
        clean_path.write_text(CLEAN_STREAM_CONFIG, encoding="utf-8")
        clean_config = load_stream_config(clean_path)
        print(f"Clean config loaded: content={clean_config.content!r}")

        dup_path = tmp_dir / "duplicate_keys_stream.yaml"
        dup_path.write_text(DUPLICATE_KEY_STREAM_CONFIG, encoding="utf-8")
        try:
            load_stream_config(dup_path)
        except ConfigError as exc:
            print(f"Duplicate-key config refused: {exc}")
            if "keys" not in str(exc) or str(dup_path) not in str(exc):
                print("FAILURE: error message missing key name or file path")
                return 1
        else:
            print("FAILURE: duplicate-key config loaded silently")
            return 1

    print("SUCCESS: clean config loads; duplicate-key config fails fast, naming key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
