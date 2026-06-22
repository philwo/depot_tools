# Copyright 2022 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import os
import pathlib
import shutil
import subprocess
import sys

DEPOT_TOOLS_ROOT = os.path.dirname(os.path.abspath(__file__))


def depot_tools_version():
    depot_tools_root = os.path.dirname(os.path.abspath(__file__))
    try:
        commit_hash = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                              cwd=depot_tools_root).decode(
                                                  'utf-8', 'ignore')
        return 'git-%s' % commit_hash
    except Exception:
        pass

    # git check failed, let's check last modification of frequently checked file
    try:
        mtime = os.path.getmtime(
            os.path.join(depot_tools_root, 'infra', 'config', 'recipes.cfg'))
        return 'recipes.cfg-%d' % (mtime)
    except Exception:
        return 'unknown'


def depot_tools_config_dir():
    # Use depot tools path for mac, windows.
    if not sys.platform.startswith('linux'):
        return DEPOT_TOOLS_ROOT

    # Use $XDG_CONFIG_HOME/depot_tools or $HOME/.config/depot_tools on linux.
    config_root = os.getenv('XDG_CONFIG_HOME', os.path.expanduser('~/.config'))
    return os.path.join(config_root, 'depot_tools')


def depot_tools_config_path(file):
    config_dir = depot_tools_config_dir()
    expected_path = os.path.join(config_dir, file)

    # Silently create config dir if necessary.
    pathlib.Path(config_dir).mkdir(parents=True, exist_ok=True)

    # Silently migrate cfg from legacy path if it exists.
    if not os.path.isfile(expected_path):
        legacy_path = os.path.join(DEPOT_TOOLS_ROOT, file)
        if os.path.isfile(legacy_path):
            shutil.move(legacy_path, expected_path)

    return expected_path


def depot_tools_cache_dir():
    """Returns the per-user cache directory for depot_tools.

    Unlike depot_tools_config_dir(), this never points inside the depot_tools
    install directory: caches (e.g. git mirrors, virtualenvs) can grow to many
    GB and are regenerable, so they belong in the platform's user cache area
    alongside vpython's own `~/.cache/vpython-root.<uid>`.
    """
    if sys.platform == 'win32':
        # %LOCALAPPDATA%, e.g. C:\Users\<user>\AppData\Local.
        cache_root = os.getenv('LOCALAPPDATA',
                               os.path.expanduser('~\\AppData\\Local'))
    elif sys.platform == 'darwin':
        cache_root = os.path.expanduser('~/Library/Caches')
    else:
        # $XDG_CACHE_HOME/depot_tools or $HOME/.cache/depot_tools on linux.
        cache_root = os.getenv('XDG_CACHE_HOME', os.path.expanduser('~/.cache'))
    return os.path.join(cache_root, 'depot_tools')


def find_config_file(path, config_filename, top_dir=None):
    """Recursively finds a configuration file in parent directories."""
    current_path = os.path.abspath(path)
    if os.path.isfile(current_path):
        current_path = os.path.dirname(current_path)

    while True:
        config_file = os.path.join(current_path, config_filename)
        if os.path.isfile(config_file):
            return config_file
        if current_path == top_dir or current_path == os.path.dirname(
                current_path):
            break
        current_path = os.path.dirname(current_path)

    return None
