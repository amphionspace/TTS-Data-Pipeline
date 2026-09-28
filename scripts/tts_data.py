"""Local command entry point: python scripts/tts_data.py --help."""

import _bootstrap  # noqa: F401

from tts_data_pipeline.cli import main

if __name__ == "__main__":
    main()
