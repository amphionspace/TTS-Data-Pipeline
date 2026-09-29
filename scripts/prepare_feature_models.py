"""Download pinned encoder sources for the repository-local feature pilot."""

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from huggingface_hub import list_repo_files, snapshot_download

DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "cache/feature-models"

SOURCES = {
    "codec": ("Qwen/Qwen3-TTS-Tokenizer-12Hz", "7dd38ad4e9bad454aae9cd937d0cd577604fe229"),
    "speaker": ("Qwen/Qwen3-TTS-12Hz-0.6B-Base", "5d83992436eae1d760afd27aff78a71d676296fc"),
}


def prepare(root, kind):
    repo, revision = SOURCES[kind]
    path = root / kind
    # These two model entry points use top-level configs/weights. Enumerate exact
    # names: fnmatch's '*' also matches '/', so extension globs include submodels.
    selected = sorted(
        name
        for name in list_repo_files(repo, revision=revision)
        if "/" not in name and Path(name).suffix in {".json", ".safetensors"}
    )
    if "config.json" not in selected or not any(n.endswith(".safetensors") for n in selected):
        raise ValueError(f"Missing model config/weights in {repo}@{revision}")
    snapshot_download(
        repo,
        revision=revision,
        local_dir=path,
        allow_patterns=selected,
        max_workers=4,
    )
    files = {}
    for name in selected:
        with (path / name).open("rb") as stream:
            files[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    result = {"repo_id": repo, "revision": revision, "files": files}
    print(f"Prepared {kind}: {len(files)} files", flush=True)
    return kind, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output directory (default: repository cache/feature-models; relative paths use CWD)",
    )
    args = parser.parse_args(argv)
    args.output = args.output.expanduser().resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = dict(pool.map(lambda kind: prepare(args.output, kind), SOURCES))
    # A failed download must not publish a partial source inventory.
    temporary = args.output / "sources.json.tmp"
    temporary.write_text(json.dumps(results, indent=2) + "\n")
    temporary.replace(args.output / "sources.json")


if __name__ == "__main__":
    main()
