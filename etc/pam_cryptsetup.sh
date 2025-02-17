#!/bin/sh

DEV=/dev/nvme0n1p3
NAME=home

if [[ "$PAM_USER" = quark ]] && ! [[ -e /dev/mapper/$NAME ]]; then
	/bin/cryptsetup open --allow-discards "$DEV" "$NAME" && mount /dev/mapper/$NAME /home -o relatime,discard,lazytime
fi
