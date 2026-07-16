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


class HistoryError(RuntimeError):
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


def historical_diff(repo_root: Path, tracked_path: Path, diff_path: Path) -> bytes:
    tracked_relative = tracked_path.relative_to(repo_root).as_posix()
    diff_relative = diff_path.relative_to(repo_root).as_posix()
    log = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", tracked_relative],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    commit = log.stdout.strip()
    if log.returncode != 0:
        raise HistoryError(log.stderr.decode(errors="replace").strip())
    if not commit:
        raise HistoryError("tracked file has no commit history")

    blob = subprocess.run(
        ["git", "cat-file", "blob", commit + b":" + diff_relative.encode()],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if blob.returncode != 0:
        reason = blob.stderr.decode(errors="replace").strip()
        raise HistoryError(reason or "historical diff is missing")
    return blob.stdout


def print_change(system_path: Path, merged: bytes, delete_pacnew: bool) -> None:
    current = system_path.read_bytes()
    diff = difflib.diff_bytes(
        difflib.unified_diff,
        current.splitlines(keepends=True),
        merged.splitlines(keepends=True),
        fromfile=str(system_path).encode(),
        tofile=f"{system_path} (merged)".encode(),
    )
    sys.stdout.write(b"".join(diff).decode(errors="replace"))
    if delete_pacnew:
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

    try:
        package_current, current_desired = files_from_unified_diff(
            diff_path.read_bytes()
        )
    except DiffError as error:
        print(f"Skipping {system_path}: cannot reconstruct current diff: {error}")
        return False

    tracked = tracked_path.read_bytes()
    if current_desired != tracked:
        print(f"Skipping {system_path}: current diff does not reconstruct tracked file")
        return False

    try:
        old_diff = historical_diff(repo_etc.parent, tracked_path, diff_path)
        package_old, historical_desired = files_from_unified_diff(old_diff)
    except (HistoryError, DiffError) as error:
        print(f"Skipping {system_path}: cannot read historical diff: {error}")
        return False
    if historical_desired != tracked:
        print(
            f"Skipping {system_path}: historical diff does not reconstruct tracked file"
        )
        return False

    current = system_path.read_bytes()
    if current != tracked:
        print(f"Skipping {system_path}: system file differs from tracked version")
        return False

    has_pacnew = pacnew_path.is_file()
    if has_pacnew and pacnew_path.read_bytes() != package_current:
        print(f"Skipping {system_path}: pacnew differs from current package version")
        return False

    if package_old == package_current:
        merged = tracked
    else:
        returncode, merged, error = merge_files(tracked, package_old, package_current)
        if returncode != 0:
            reason = "merge has conflicts" if returncode == 1 else error.strip()
            print(f"Skipping {system_path}: {reason or 'git merge-file failed'}")
            return False

    content_changed = merged != current
    if not content_changed and not has_pacnew:
        return False

    if dry_run:
        print_change(system_path, merged, has_pacnew)
    else:
        actions = []
        if content_changed:
            system_path.write_bytes(merged)
            actions.append(f"updated {system_path}")
        if has_pacnew:
            pacnew_path.unlink()
            actions.append(f"deleted {pacnew_path}")
        print(" and ".join(actions).capitalize())
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebase tracked /etc customizations onto current package versions."
    )
    parser.add_argument(
        "-Y",
        "--no-dry-run",
        action="store_true",
        help="write merged files and delete matching .pacnew files",
    )
    args = parser.parse_args()

    repo_etc = Path(__file__).resolve().parent / "etc"
    changed = False
    for diff_path in sorted(repo_etc.rglob("*.diff")):
        changed |= process_diff(diff_path, repo_etc, Path("/etc"), not args.no_dry_run)

    if not args.no_dry_run:
        if not changed:
            print("No configuration updates can be safely applied.")
        print("Dry run only; re-run with -Y or --no-dry-run to apply these changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
