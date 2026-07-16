#!/usr/bin/env python3

import argparse
import difflib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HUNK_HEADER = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class DiffError(ValueError):
    pass


def files_from_unified_diff(diff: bytes) -> tuple[bytes, bytes]:
    """Return the original and modified files embedded in a full-context diff."""
    diff_lines = diff.splitlines(keepends=True)
    original_lines: list[bytes] = []
    modified_lines: list[bytes] = []
    hunk_count = 0
    line_no = 0

    while line_no < len(diff_lines):
        match = HUNK_HEADER.match(diff_lines[line_no])
        if match is None:
            line_no += 1
            continue

        hunk_count += 1
        old_count = int(match.group(2) or b"1")
        new_count = int(match.group(4) or b"1")
        seen_old = 0
        seen_new = 0
        line_no += 1

        while line_no < len(diff_lines) and not diff_lines[line_no].startswith(b"@@ "):
            line = diff_lines[line_no]
            line_no += 1
            if line.startswith(b"\\"):
                continue
            if not line or line[:1] not in (b" ", b"+", b"-"):
                raise DiffError("invalid line in unified diff hunk")

            marker, content = line[:1], line[1:]
            if marker in (b" ", b"-"):
                original_lines.append(content)
                seen_old += 1
            if marker in (b" ", b"+"):
                modified_lines.append(content)
                seen_new += 1

        if (seen_old, seen_new) != (old_count, new_count):
            raise DiffError("unified diff hunk has inconsistent line counts")

    if hunk_count == 0 and diff.strip():
        raise DiffError("no unified diff hunks found")
    if hunk_count == 0:
        raise DiffError("diff is empty")
    return b"".join(original_lines), b"".join(modified_lines)


def merge_files(local: bytes, base: bytes, other: bytes) -> tuple[int, bytes, str]:
    with tempfile.TemporaryDirectory(prefix="merge-pacnew-") as temp_dir:
        temp = Path(temp_dir)
        paths = [temp / name for name in ("local", "base", "other")]
        for path, content in zip(paths, (local, base, other), strict=True):
            path.write_bytes(content)
        result = subprocess.run(
            ["git", "merge-file", "-p", *(str(path) for path in paths)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    return result.returncode, result.stdout, result.stderr.decode(errors="replace")


def print_change(system_path: Path, merged: bytes) -> None:
    current = system_path.read_bytes()
    diff = difflib.diff_bytes(
        difflib.unified_diff,
        current.splitlines(keepends=True),
        merged.splitlines(keepends=True),
        fromfile=str(system_path).encode(),
        tofile=f"{system_path} (merged)".encode(),
    )
    sys.stdout.write(b"".join(diff).decode(errors="replace"))
    print(f"Would delete {system_path}.pacnew")


def process_diff(
    diff_path: Path, repo_etc: Path, system_etc: Path, dry_run: bool
) -> bool:
    relative_diff = diff_path.relative_to(repo_etc)
    relative_path = relative_diff.with_suffix("")
    tracked_path = repo_etc / relative_path
    system_path = system_etc / relative_path
    pacnew_path = Path(f"{system_path}.pacnew")

    if not tracked_path.is_file():
        print(f"Skipping {system_path}: tracked modified file is missing")
        return False
    if not system_path.is_file():
        print(f"Skipping {system_path}: system file is missing")
        return False
    if not pacnew_path.is_file():
        return False

    try:
        base, reconstructed_tracked = files_from_unified_diff(diff_path.read_bytes())
    except DiffError as error:
        print(f"Skipping {system_path}: cannot reconstruct files from diff: {error}")
        return False

    tracked = tracked_path.read_bytes()
    if reconstructed_tracked != tracked:
        print(f"Skipping {system_path}: diff does not reconstruct tracked version")
        return False

    current = system_path.read_bytes()
    if current != tracked:
        print(f"Skipping {system_path}: system file differs from tracked version")
        return False

    returncode, merged, error = merge_files(tracked, base, pacnew_path.read_bytes())
    if returncode != 0:
        reason = "merge has conflicts" if returncode == 1 else error.strip()
        print(f"Skipping {system_path}: {reason or 'git merge-file failed'}")
        return False

    if dry_run:
        print_change(system_path, merged)
    else:
        system_path.write_bytes(merged)
        pacnew_path.unlink()
        print(f"Updated {system_path} and deleted {pacnew_path}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge matching /etc/*.pacnew files using tracked configuration diffs."
    )
    parser.add_argument(
        "-Y",
        "--no-dry-run",
        action="store_true",
        help="write merged files and delete successfully merged .pacnew files",
    )
    args = parser.parse_args()

    repo_etc = Path(__file__).resolve().parent / "etc"
    changed = False
    for diff_path in sorted(repo_etc.rglob("*.diff")):
        changed |= process_diff(diff_path, repo_etc, Path("/etc"), not args.no_dry_run)

    if not args.no_dry_run:
        if not changed:
            print("No safely mergeable .pacnew files found.")
        print("Dry run only; re-run with -Y or --no-dry-run to apply these changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
