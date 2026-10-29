# Copyright 2024 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Defines common conditions for the new auth stack migration."""

from __future__ import annotations

import os

import scm


def Enabled() -> bool:
    """Returns True if new auth stack is enabled.

    A .gitcookies file doesn't disable it: hosts with credentials in it keep
    using them through gerrit_util.GitcookiesHostAuthenticator.
    """
    return SwitchedOn()


def SwitchedOn() -> bool:
    """Returns True if new auth stack is "switched on"."""
    if Default():
        return not ExplicitlyDisabled()
    return ExplicitlyEnabled()


def Default() -> bool:
    "Returns default enablement status for new auth stack."
    return True


def ExplicitlyEnabled() -> bool:
    """Returns True if new auth stack is explicitly enabled.

    Directly checks config and doesn't do gitcookie check.
    """
    return scm.GIT.GetConfig(os.getcwd(), "depot-tools.usenewauthstack") in (
        "yes",
        "on",
        "true",
        "1",
    )


def ExplicitlyDisabled() -> bool:
    """Returns True if new auth stack is explicitly disabled."""
    return scm.GIT.GetConfig(os.getcwd(), "depot-tools.usenewauthstack") in (
        "no",
        "off",
        "false",
        "0",
    )
