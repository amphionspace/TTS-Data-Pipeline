"""Selected executable feature-contract rules; no extraction or training executor."""

import hashlib
import json
import math
import re
from datetime import datetime

import numpy as np

from .timestamps import BEIJING, parse_timestamp


def validate_profile_name(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", value) is None:
        raise ValueError("profile_name must be a nonempty lowercase name using a-z, 0-9, _ or -")


def make_feature_run_id(dataset_id, kind, profile_name, created_at, sequence=1):
    """Format a name; the publisher must reserve it atomically and persist it on retry."""
    validate_profile_name(profile_name)
    if not isinstance(dataset_id, str) or re.fullmatch(r"[a-z][a-z0-9_]*", dataset_id) is None:
        raise ValueError("Invalid dataset_id")
    if kind not in {"codec", "speaker_embedding", "text", "merged"}:
        raise ValueError("Invalid feature kind")
    if type(sequence) is not int or sequence < 1:
        raise ValueError("Run sequence must be a positive integer")
    stamp = parse_timestamp(created_at).astimezone(BEIJING).strftime("%Y%m%dT%H%M%Sbjt")
    return f"{dataset_id}-{kind}-{profile_name}-{stamp}-{sequence:02d}"


def validate_feature_metadata(manifest):
    """Validate new feature publication names and timezone-aware completion times."""
    validate_profile_name(manifest.get("profile_name"))
    run_id = manifest.get("run_id")
    match = re.search(r"-(\d{8}T\d{6})bjt-(\d{2,})$", run_id or "")
    if match is None:
        raise ValueError("run_id must end in YYYYMMDDTHHMMSSbjt-NN")
    created = datetime.strptime(match[1], "%Y%m%dT%H%M%S").replace(tzinfo=BEIJING)
    expected = make_feature_run_id(
        manifest["dataset_id"],
        manifest["kind"],
        manifest["profile_name"],
        created.isoformat(),
        int(match[2]),
    )
    if run_id != expected:
        raise ValueError("run_id must preserve dataset_id, kind and profile_name exactly")
    finished = parse_timestamp(manifest["finished_at"])
    if finished < created:
        raise ValueError("finished_at precedes run creation")
    if datetime.fromisoformat(manifest["finished_at"]).utcoffset() != BEIJING.utcoffset(None):
        raise ValueError("New feature finished_at must use Beijing time (+08:00)")


def target_set_sha256(sorted_rows):
    """Hash a sorted target stream without loading a full selection into memory.

    Existence and view-to-parent foreign keys require separate snapshot audits.
    """
    checksum, previous, count = hashlib.sha256(), None, 0
    for row in sorted_rows:
        kind, target, parent = (row[k] for k in ("target_kind", "target_id", "parent_sample_id"))
        if kind != "sample" or any(not isinstance(v, str) or not v for v in (target, parent)):
            raise ValueError("Invalid target identity")
        if kind == "sample" and parent != target:
            raise ValueError("Sample must reference itself as parent")
        key = (kind, target)
        if previous is not None and (key <= previous or kind != previous[0]):
            raise ValueError("Targets must be unique, sorted and of one kind")
        previous = key
        checksum.update(
            (
                json.dumps([kind, target, parent], ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
        )
        count += 1
    if not count:
        raise ValueError("Target selection must not be empty")
    return checksum.hexdigest()


def equivalent_payloads(kind, canonical, candidate, *, atol=0.0, rtol=0.0):
    """Compare already validated payloads, never replace canonical artifact hashes.

    Callers must first match profile, feature key and effective waveform hash.
    This comparison does not establish input identity or validate a full record.
    """
    left, right = np.asarray(canonical), np.asarray(candidate)
    if kind not in {"codec", "speaker_embedding"}:
        raise ValueError("Unknown feature kind")
    if not all(math.isfinite(v) and v >= 0 for v in (atol, rtol)):
        raise ValueError("Tolerances must be finite and nonnegative")
    if kind == "codec" and (atol or rtol):
        raise ValueError("Codec token comparison must be exact")
    if left.shape != right.shape or not np.isfinite(left).all() or not np.isfinite(right).all():
        return False
    if kind == "codec":
        return (
            np.issubdtype(left.dtype, np.integer)
            and np.issubdtype(right.dtype, np.integer)
            and bool(np.array_equal(left, right))
        )
    if not np.issubdtype(left.dtype, np.floating) or not np.issubdtype(right.dtype, np.floating):
        return False
    # Canonical is the reference operand; relative tolerance is not symmetric.
    return bool(
        np.all(
            np.abs(right.astype(np.float64) - left.astype(np.float64))
            <= atol + rtol * np.abs(left.astype(np.float64))
        )
    )


def validate_feature_coverage(manifest):
    """Check feature count/selection metadata; table contents need a separate audit."""
    validate_feature_metadata(manifest)
    if manifest["target_kind"] != "sample":
        raise ValueError("Unknown feature target kind")
    coverage, selection = manifest["coverage"], manifest["selection"]
    counts = [manifest["rows"], selection["available_target_rows"], selection["target_count"]]
    counts += [
        coverage[k] for k in ("total_targets", "ok", "failed", "unsupported", "skipped", "missing")
    ]
    if any(type(n) is not int or n < 0 for n in counts):
        raise ValueError("Counts must be nonnegative integers")
    selected = selection["target_count"]
    if selected == 0 or selected > selection["available_target_rows"]:
        raise ValueError("Selection must be a nonempty subset of its target table")
    if coverage["missing"] or not (
        manifest["rows"]
        == selected
        == coverage["total_targets"]
        == sum(coverage[k] for k in ("ok", "failed", "unsupported", "skipped"))
    ):
        raise ValueError("Published feature coverage must account for every selected target")
    mode = selection["mode"]
    if mode == "all_samples":
        if selected != selection["available_target_rows"] or "table" in selection:
            raise ValueError("All-target selection cannot hide a subset table")
    elif mode == "subset":
        table = selection["table"]
        if (
            table["rows"] != selected
            or type(table["lance_version"]) is not int
            or table["lance_version"] < 1
        ):
            raise ValueError("Subset table must pin a snapshot with the selected row count")
    elif mode == "selection_branch":
        matches = [i for i in manifest["inputs"] if i["alias"] == selection["input_alias"]]
        if manifest["target_kind"] != "sample" or len(matches) != 1 or "table" in selection:
            raise ValueError("Selection branch requires one samples input and no targets table")
        source = matches[0]
        if (
            not source.get("branch")
            or type(source.get("lance_version")) is not int
            or source["lance_version"] < 1
            or selection.get("filter") != "selection_reason = 0"
            or not selection.get("manifest_path")
            or re.fullmatch(r"[0-9a-f]{64}", selection.get("manifest_sha256", "")) is None
        ):
            raise ValueError("Selection branch must pin its manifest, branch, version and filter")
        # The publisher must additionally open the referenced selection manifest,
        # verify this exact output, and audit IDs/fingerprints against both tables.
    else:
        raise ValueError("Unknown selection mode")
    # Sample identity and source membership are verified against the pinned input table.
