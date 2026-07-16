# etcfiles

Similar to `dotfiles`, but for `/etc`. Currently designed for Arch Linux.

Also tracks the package list.

Also provides `merge-pacnew.py` to do 3-way merge of `.pacnew` changes.

To use this repo, fork or copy the Python scripts, run `./backup.py` on your Arch system, then commit. Repeat periodically. Run `merge-pacnew.py` as needed.

## `backup.py`

Backup the system state. Equivalent to `backup.py --etc --pkglist`.

Depends on `pyalpm`, to install:

```bash
sudo pacman -S pyalpm git tar
```

### `backup.py --etc`

Copy files in `/etc` to `./etc`.

- Files matching `.gitignore` are skipped.
- Files owned by packages are skipped, unless they are marked as ["backup"](https://wiki.archlinux.org/title/PKGBUILD#backup) and differ from the package's version.

For a modified file, this script will try to write a `.diff` file that contains the change compared to the original package. This is a best effort and might fail if the original package is no longer in the pacman cache.

### `backup.py --pkglist`

Backup the list of manually installed packages to `pkglist`.

- `pkglist/installed`: Explicitly installed packages. Similar to `pacman -Qe` output.
- `pkglist/minimal`: `installed` excluding packages required by other installed packages.
- `pkglist/grouped`: Organize packages by groups. Packages without a group will be assigned to either `[aur]`, or `[ungrouped]`.

## `merge-pacnew.py`

Perform 3-way merge (base: old stock config; local: modified config; other: new stock config) to handle config updates.

This script checks the in-repo `.diff` file history to get the accurate version of "old stock config". So it is technically more capable than tools that can only guess what the "old" version is.

Dry-run by default. Does not actually require `.pacnew` files to be on disk (file content is read from cached pkgs).

## Background

When choosing between NixOS and Arch for my laptop, I found NixOS too complex. However, I do like the idea of tracking what I have done to the system.

The system state is primarily determined by (1) name and version of installed packages, and (2) modifications to the system config files.

- For (1), listing packages is straightforward. Technically, [Arch Linux Archive](https://wiki.archlinux.org/title/Arch_Linux_Archive) can be used to restore to the exact state like NixOS. Practically, people probably just want the latest version of everything.
- For (2), version controlling `/etc` is not a bad idea. Tracking every file in `/etc` is too verbose and fragile to package upgrades. `backup.py` helps identify those manually edited files.
