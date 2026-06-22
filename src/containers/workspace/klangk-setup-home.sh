#!/bin/bash
# Populate a new user's home directory with /etc/skel files and
# append the Klangk default prompt.  Called by the backend when a
# per-user home directory is first created.
#
# Usage: klangk-setup-home <home-dir>

set -e

home="$1"
if [ -z "$home" ]; then
  echo "Usage: klangk-setup-home.sh <home-dir>" >&2
  exit 1
fi

# Copy skeleton files (.profile, .bashrc, etc.)
if [ -d /etc/skel ]; then
  cp -a /etc/skel/. "$home"/
fi

# Append the Klangk default prompt
printf '%s\n' "PS1='\[\033[01;34m\]\w\[\033[00m\]\$ '" >>"$home"/.bashrc
