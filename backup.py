#!/bin/python3

import argparse
import collections
import difflib
import enum
import functools
import hashlib
import os
import shutil
import subprocess
import sys

from typing import Iterable, Tuple, Optional, Callable, List

try:
    import pyalpm
    import pycman.config
except ImportError:
    print("Please install pyalpm package first", file=sys.stderr)
    raise


@functools.cache
def get_handle() -> pyalpm.Handle:
    """get pyalpm.Handle with system pacman config loaded"""
    return pycman.config.init_with_config("/etc/pacman.conf")


@functools.cache
def get_localdb() -> pyalpm.DB:
    return get_handle().get_localdb()


@functools.cache
def get_sync_dbs() -> List[pyalpm.DB]:
    """get pyalpm.DBs, like [core, extra]"""
    return get_handle().get_syncdbs()


@functools.cache
def get_explicitly_installed_packages():
    return [
        pkg
        for pkg in get_localdb().pkgcache
        if pkg.reason == pyalpm.PKG_REASON_EXPLICIT
    ]


@functools.cache
def get_hostname() -> str:
    with open("/etc/hostname") as f:
        return f.read().strip()


@functools.cache
def list_tarball(tar_path: str) -> List[str]:
    return (
        subprocess.check_output(["tar", "-tf", tar_path], stderr=subprocess.DEVNULL)
        .decode()
        .splitlines()
    )


def extract_tarball_single_path(tar_path: str, inner_path: str) -> Optional[bytes]:
    """Return the content of `inner_path` stored in `tar_path`. Best effort."""
    inner_path = inner_path.lstrip("/")
    if inner_path in list_tarball(tar_path):
        return subprocess.check_output(
            ["tar", "-xO", inner_path, "-f", tar_path], stderr=subprocess.DEVNULL
        )
    return None


@functools.cache
def get_check_ignore() -> Callable[[str], bool]:
    """Returns a function that checks if a path is git-ignored"""
    proc = subprocess.Popen(
        ["git", "check-ignore", "--stdin", "--non-matching", "--no-index", "--verbose"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    def check_ignore(path: str) -> bool:
        assert proc.stdin and proc.stdout
        proc.stdin.write(f"{path.lstrip('/')}\n".encode())
        proc.stdin.flush()
        line = proc.stdout.readline()
        return not line.startswith(b"::")

    return check_ignore


class Status(enum.Enum):
    NOT_OWNED = 0
    BACKUP_UNCHANGED = 4
    BACKUP_CHANGED = 5
    BACKUP_UNKNOWN = 6
    OWNED = 8


def walk_dir(dir_path: str) -> Iterable[os.DirEntry]:
    """Yield DirEntry for files/symlinks. Does not walk into directory symlinks."""
    try:
        with os.scandir(dir_path) as it:
            for entry in it:
                if entry.is_symlink() or entry.is_file():
                    yield entry
                elif entry.is_dir():
                    yield from walk_dir(entry.path)
    except IOError:
        pass


def is_path_ignored(path: str) -> bool:
    return get_check_ignore()(path)


def calculate_md5_for_path(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except IOError:
        return None
    else:
        return hashlib.md5(data).hexdigest()


def walk_etc() -> Iterable[str]:
    """Yield paths in /etc. Skip ignored paths."""
    for entry in walk_dir("/etc"):
        path = entry.path
        if not is_path_ignored(path):
            yield path


def with_status(
    paths: Iterable[str],
) -> Iterable[Tuple[str, Status, Optional[pyalpm.Package]]]:
    """Figure out the `status` per path. Yield (path, status, pkg)."""
    path_status = {p: Status.NOT_OWNED for p in paths}
    path_pkg = {}

    localpkgs = get_handle().get_localdb().pkgcache
    for pkg in localpkgs:
        for rel_path, _file_size, _file_mode in pkg.files:
            path = f"/{rel_path}"
            if path in path_status:
                path_status[path] = Status.OWNED
        for rel_path, original_md5 in pkg.backup:
            path = f"/{rel_path}"
            if path in path_status:
                match calculate_md5_for_path(path):
                    case None:
                        status = Status.BACKUP_UNKNOWN
                    case s if s == original_md5:
                        status = Status.BACKUP_UNCHANGED
                    case _:
                        status = Status.BACKUP_CHANGED
                path_pkg[path] = pkg
                path_status[path] = status

    for path, status in path_status.items():
        yield path, status, path_pkg.get(path)


def copy_path_to_local(path: str, message: str = ""):
    """Backup /{path} to ./{path}. Path usually starts with '/etc/'"""
    rel_path = path.lstrip("/")
    os.makedirs(os.path.dirname(rel_path), exist_ok=True)
    print(f"Copying {message} {path}")
    shutil.copyfile(path, rel_path, follow_symlinks=False)
    shutil.copymode(path, rel_path, follow_symlinks=False)


def get_file_content_from_package(
    local_pkg: pyalpm.Package, path: str
) -> Optional[bytes]:
    for db in get_sync_dbs():
        pkg = db.get_pkg(local_pkg.name)
        if pkg is None or pkg.version != local_pkg.version or not pkg.filename:
            continue
        for cache_dir in get_handle().cachedirs:
            tar_path = os.path.join(cache_dir, pkg.filename)
            if os.path.isfile(tar_path):
                return extract_tarball_single_path(tar_path, path)
    return None


def maybe_write_diff(local_pkg: pyalpm.Package, path: str):
    assert os.path.isabs(path)
    with open(path, "rb") as f:
        current_content = f.read()
    past_content = get_file_content_from_package(local_pkg, path)
    if past_content is None:
        return
    try:
        current_text = current_content.decode()
        past_text = past_content.decode()
    except UnicodeDecodeError:
        return
    diff_text = "".join(
        difflib.unified_diff(
            past_text.splitlines(True),
            current_text.splitlines(True),
            fromfile=f"{local_pkg.name}-{local_pkg.version}/{path.lstrip('/')}",
            tofile=f"current/{path.lstrip('/')}",
            n=65536,
        )
    )
    diff_path = f"{path.lstrip('/')}.diff"
    print(f" Writing diff to {diff_path}")
    with open(diff_path, "wb") as f:
        f.write(diff_text.encode())


def backup_etc():
    os.chdir(os.path.dirname(__file__))
    shutil.rmtree("./etc", ignore_errors=True)
    for path, status, pkg in with_status(walk_etc()):
        match status:
            case Status.NOT_OWNED:
                copy_path_to_local(path, "untracked")
            case Status.BACKUP_CHANGED:
                copy_path_to_local(path, "modified ")
                maybe_write_diff(pkg, path)


def group_packages(
    packages: Iterable[pyalpm.Package],
) -> Iterable[Tuple[str, List[pyalpm.Package]]]:
    groups = collections.defaultdict(list)
    for pkg in packages:
        if pkg.groups:
            for group in pkg.groups:
                groups[group].append(pkg)
        else:
            group = "aur" if pkg.packager == "Unknown Packager" else "ungrouped"
            groups[group].append(pkg)

    # sort group by package count
    visited = set()
    for group, pkgs in sorted(
        groups.items(), key=lambda item: (-len(item[1]), item[0])
    ):
        # skip this (small, duplicated) group if all pkgs are mentioned elsewhere
        if all(p.name in visited for p in pkgs):
            continue
        visited.update(pkgs)
        yield group, pkgs


def backup_pkglist():
    os.chdir(os.path.dirname(__file__))
    os.makedirs("pkglist", exist_ok=True)
    installed_pkgs = get_explicitly_installed_packages()
    print("Writing pkglist/installed")
    with open("pkglist/installed", "w", newline="\n") as f:
        for pkg in installed_pkgs:
            f.write(f"{pkg.name} {pkg.version}\n")

    print("Writing pkglist/grouped")
    with open("pkglist/grouped", "w", newline="\n") as f:
        for group, pkgs in group_packages(installed_pkgs):
            f.write(f"[{group}]\n")
            for pkg in pkgs:
                f.write(f"  {pkg.name} {pkg.version}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Backup system configuration and package list."
    )
    parser.add_argument("--etc", action="store_true", help="Backup /etc directory")
    parser.add_argument("--pkglist", action="store_true", help="Backup package list")
    args = parser.parse_args()

    if not args.etc and not args.pkglist:
        args.etc = args.pkglist = True

    if args.etc:
        backup_etc()
    if args.pkglist:
        backup_pkglist()


if __name__ == "__main__":
    main()

