"""Show the platform state of the latest ACP job submitted for a work root."""

import argparse
import json
from pathlib import Path

import yaml

from .api import jobs_url, request

PROJECT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=PROJECT / "configs/acp.yaml")
    args = parser.parse_args()
    platform = yaml.safe_load(args.config.read_text())
    record = json.loads(sorted((args.work / "submissions").glob("*/job.json"))[-1].read_text())
    name = record["response"]["name"]
    job = request("GET", f"{jobs_url(platform)}/{name}", home=platform["home"])
    keys = ("name", "display_name", "state", "create_time", "start_time", "complete_time")
    print(json.dumps({k: job.get(k) for k in keys}, ensure_ascii=False))


if __name__ == "__main__":
    main()
