#!/usr/bin/env python3
"""Prepare Emilia2 sources and publish deduplicated short base samples."""

import _bootstrap  # noqa: F401

from tts_data_pipeline.ingest.emilia2.cli import main

if __name__ == "__main__":
    main()
