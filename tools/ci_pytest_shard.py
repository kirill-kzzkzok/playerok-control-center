"""Partition the actual pytest collection; no tests are deselected or retried."""

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest


def partition(nodeids, count):
    if count < 1 or len(set(nodeids)) != len(nodeids):
        raise ValueError("Invalid shard count or duplicate collected test IDs")
    modules = defaultdict(list)
    for ident in nodeids:
        modules[ident.split("::", 1)[0]].append(ident)
    groups, weights = [[] for _ in range(count)], [0] * count
    for module in sorted(modules, key=lambda name: (-len(modules[name]), name)):
        index = min(range(count), key=lambda value: (weights[value], value))
        groups[index].append(module)
        weights[index] += len(modules[module])
    return [sorted(group) for group in groups]


class Collection:
    def __init__(self):
        self.nodeids = []

    def pytest_collection_finish(self, session):
        self.nodeids = [item.nodeid for item in session.items]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.index < args.count:
        parser.error("Need 0 <= index < count")
    collection = Collection()
    result = pytest.main(["--collect-only", "-qq", "-p", "no:cacheprovider"], plugins=[collection])
    if result != 0:
        return int(result)
    if not collection.nodeids:
        raise ValueError("Empty test collection")
    groups = partition(collection.nodeids, args.count)
    selected = set(groups[args.index])
    nodeids = [ident for ident in collection.nodeids if ident.split("::", 1)[0] in selected]
    if not nodeids:
        raise ValueError("Empty shard")
    manifest = {
        "head": os.environ.get("GITHUB_SHA"),
        "index": args.index,
        "count": args.count,
        "collected": len(collection.nodeids),
        "selected": len(nodeids),
        "modules": sorted(selected),
        "nodeids": nodeids,
        "all_modules": dict(
            sorted(
                (module, sum(ident.split("::", 1)[0] == module for ident in collection.nodeids))
                for group in groups
                for module in group
            )
        ),
    }
    output = Path("data")
    output.mkdir(exist_ok=True)
    (output / f"ci-shard-{args.index}-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"Shard {args.index + 1}/{args.count}: {len(nodeids)} of {len(collection.nodeids)} tests", flush=True
    )
    if args.list_only:
        return 0
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--junitxml",
            str(output / f"ci-shard-{args.index}.xml"),
            *groups[args.index],
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
