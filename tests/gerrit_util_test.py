#!/usr/bin/env vpython3
# coding=utf-8
# Copyright 2019 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import base64
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

import email.message
import http.cookiejar
import io
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Optional
from unittest import mock
import urllib.error
import urllib.request
import urllib.response

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scm_mock

import gerrit_util
import auth
import metrics
import scm
import subprocess2

RUN_SUBPROC_TESTS = "RUN_SUBPROC_TESTS" in os.environ


def makeConn(host: str) -> gerrit_util.HttpConn:
    """Makes an empty gerrit_util.HttpConn for the given host."""
    return gerrit_util.HttpConn(
        req_uri="???",
        req_method="GET",
        req_host=host,
        req_headers={},
        req_body=None,
    )


class CookiesAuthenticatorTest(unittest.TestCase):
    _GITCOOKIES = "\n".join(
        [
            "\t".join(
                [
                    "chromium.googlesource.com",
                    "FALSE",
                    "/",
                    "TRUE",
                    "2147483647",
                    "o",
                    "git-user.chromium.org=1/chromium-secret",
                ]
            ),
            "\t".join(
                [
                    "chromium-review.googlesource.com",
                    "FALSE",
                    "/",
                    "TRUE",
                    "2147483647",
                    "o",
                    "git-user.chromium.org=1/chromium-secret",
                ]
            ),
            "\t".join(
                [
                    ".example.com",
                    "FALSE",
                    "/",
                    "TRUE",
                    "2147483647",
                    "o",
                    "example-bearer-token",
                ]
            ),
            "\t".join(
                [
                    "another-path.example.com",
                    "FALSE",
                    "/foo",
                    "TRUE",
                    "2147483647",
                    "o",
                    "git-example.com=1/another-path-secret",
                ]
            ),
            "\t".join(
                [
                    "another-key.example.com",
                    "FALSE",
                    "/",
                    "TRUE",
                    "2147483647",
                    "not-o",
                    "git-example.com=1/another-key-secret",
                ]
            ),
            "#"
            + "\t".join(
                [
                    "chromium-review.googlesource.com",
                    "FALSE",
                    "/",
                    "TRUE",
                    "2147483647",
                    "o",
                    "git-invalid-user.chromium.org=1/invalid-chromium-secret",
                ]
            ),
            "Some unrelated line\t that should not be here",
        ]
    )

    def setUp(self):
        mock.patch(
            "gclient_utils.FileRead", return_value=self._GITCOOKIES
        ).start()
        mock.patch("os.getenv", return_value={}).start()
        mock.patch("os.environ", {"HOME": "$HOME"}).start()
        mock.patch("os.getcwd", return_value="/fame/cwd").start()
        mock.patch("os.path.exists", return_value=True).start()
        mock.patch("os.path.isfile", return_value=True).start()
        mock.patch(
            "git_common.run",
            side_effect=[
                subprocess2.CalledProcessError(1, ["cmd"], "cwd", "out", "err")
            ],
        ).start()
        scm_mock.GIT(self)

        self.addCleanup(mock.patch.stopall)
        self.maxDiff = None

    def assertAuthenticatedConnAuth(
        self,
        cookieAuth: gerrit_util.CookiesAuthenticator,
        host: str,
        expected: str,
    ):
        conn = makeConn(host)
        cookieAuth.authenticate(conn)
        self.assertEqual(conn.req_headers["Authorization"], expected)

    def testGetNewPasswordUrl(self):
        cookieAuth = gerrit_util.CookiesAuthenticator()
        self.assertEqual(
            "https://chromium.googlesource.com/new-password",
            cookieAuth.get_new_password_url("chromium.googlesource.com"),
        )
        self.assertEqual(
            "https://chrome-internal.googlesource.com/new-password",
            cookieAuth.get_new_password_url(
                "chrome-internal-review.googlesource.com"
            ),
        )

    def testGetNewPasswordMessage(self):
        cookieAuth = gerrit_util.CookiesAuthenticator()
        self.assertIn(
            "https://chromium.googlesource.com/new-password",
            cookieAuth._get_new_password_message(
                "chromium-review.googlesource.com"
            ),
        )
        self.assertIn(
            "https://chrome-internal.googlesource.com/new-password",
            cookieAuth._get_new_password_message(
                "chrome-internal.googlesource.com"
            ),
        )

    def testGetGitcookiesPath(self):
        self.assertEqual(
            os.path.expanduser(os.path.join("~", ".gitcookies")),
            gerrit_util.CookiesAuthenticator().get_gitcookies_path(),
        )

        scm.GIT.SetConfig(os.getcwd(), "http.cookiefile", "/some/path")
        self.assertEqual(
            "/some/path",
            gerrit_util.CookiesAuthenticator().get_gitcookies_path(),
        )

        os.getenv.return_value = "git-cookies-path"
        self.assertEqual(
            "git-cookies-path",
            gerrit_util.CookiesAuthenticator().get_gitcookies_path(),
        )
        os.getenv.assert_called_with("GIT_COOKIES_PATH")

    def testGitcookies(self):
        cookieAuth = gerrit_util.CookiesAuthenticator()
        self.assertEqual(
            cookieAuth.gitcookies,
            {
                "chromium.googlesource.com": (
                    "git-user.chromium.org",
                    "1/chromium-secret",
                ),
                "chromium-review.googlesource.com": (
                    "git-user.chromium.org",
                    "1/chromium-secret",
                ),
                ".example.com": ("", "example-bearer-token"),
            },
        )

    def testGetAuthHeader(self):
        expected_chromium_header = (
            "Basic Z2l0LXVzZXIuY2hyb21pdW0ub3JnOjEvY2hyb21pdW0tc2VjcmV0"
        )

        cookieAuth = gerrit_util.CookiesAuthenticator()
        self.assertAuthenticatedConnAuth(
            cookieAuth, "chromium.googlesource.com", expected_chromium_header
        )
        self.assertAuthenticatedConnAuth(
            cookieAuth,
            "chromium-review.googlesource.com",
            expected_chromium_header,
        )
        self.assertAuthenticatedConnAuth(
            cookieAuth, "some-review.example.com", "Bearer example-bearer-token"
        )

    def testGetAuthEmail(self):
        cookieAuth = gerrit_util.CookiesAuthenticator()
        self.assertEqual(
            "user@chromium.org",
            cookieAuth.get_auth_email("chromium.googlesource.com"),
        )
        self.assertEqual(
            "user@chromium.org",
            cookieAuth.get_auth_email("chromium-review.googlesource.com"),
        )
        self.assertIsNone(cookieAuth.get_auth_email("some-review.example.com"))

    def testGetAuthForHostDomainBoundaryMatching(self):
        cookieAuth = gerrit_util.CookiesAuthenticator()
        cookieAuth._gitcookies = {
            ".googlesource.com": ("git-user.chromium.org", "wildcard-secret"),
            "chromium-review.googlesource.com": (
                "git-user.chromium.org",
                "exact-secret",
            ),
        }
        self.assertEqual(
            cookieAuth._get_auth_for_host("chromium.googlesource.com"),
            ("git-user.chromium.org", "wildcard-secret"),
        )
        self.assertEqual(
            cookieAuth._get_auth_for_host("googlesource.com"),
            ("git-user.chromium.org", "wildcard-secret"),
        )
        cookieAuth._gitcookies = {
            "chromium-review.googlesource.com": (
                "git-user.chromium.org",
                "exact-secret",
            ),
        }
        self.assertEqual(
            cookieAuth._get_auth_for_host(
                "CHROMIUM-REVIEW.GOOGLESOURCE.COM:443"
            ),
            ("git-user.chromium.org", "exact-secret"),
        )
        cookieAuth._gitcookies = {
            ".googlesource.com": ("git-user.chromium.org", "wildcard-secret"),
            "chromium-review.googlesource.com": (
                "git-user.chromium.org",
                "exact-secret",
            ),
        }
        self.assertIsNone(
            cookieAuth._get_auth_for_host("googlesource.com.evil.com")
        )
        self.assertIsNone(
            cookieAuth._get_auth_for_host("chromium.googlesource.com.evil.com")
        )
        self.assertIsNone(
            cookieAuth._get_auth_for_host(
                "chromium-review.googlesource.com.evil.com"
            )
        )
        self.assertIsNone(cookieAuth._get_auth_for_host("fakegooglesource.com"))

    def testDebugSummaryState(self) -> None:
        cookieAuth = gerrit_util.CookiesAuthenticator()
        with mock.patch("os.path.isfile", return_value=False):
            self.assertEqual(cookieAuth.debug_summary_state(), "")
        with mock.patch("gclient_utils.FileRead", side_effect=OSError("fail")):
            self.assertEqual(cookieAuth.debug_summary_state(), "")

        expected_default = "\n".join(
            [
                "\t".join(
                    [
                        "chromium.googlesource.com",
                        "FALSE",
                        "/",
                        "TRUE",
                        "2147483647",
                        "o",
                        "git-user.chromium.org=REDACTED",
                    ]
                ),
                "\t".join(
                    [
                        "chromium-review.googlesource.com",
                        "FALSE",
                        "/",
                        "TRUE",
                        "2147483647",
                        "o",
                        "git-user.chromium.org=REDACTED",
                    ]
                ),
                "\t".join(
                    [
                        ".example.com",
                        "FALSE",
                        "/",
                        "TRUE",
                        "2147483647",
                        "o",
                        "REDACTED",
                    ]
                ),
                "\t".join(
                    [
                        "another-path.example.com",
                        "FALSE",
                        "/foo",
                        "TRUE",
                        "2147483647",
                        "o",
                        "git-example.com=REDACTED",
                    ]
                ),
                "\t".join(
                    [
                        "another-key.example.com",
                        "FALSE",
                        "/",
                        "TRUE",
                        "2147483647",
                        "not-o",
                        "git-example.com=REDACTED",
                    ]
                ),
                "#"
                + "\t".join(
                    [
                        "chromium-review.googlesource.com",
                        "FALSE",
                        "/",
                        "TRUE",
                        "2147483647",
                        "o",
                        "git-invalid-user.chromium.org=REDACTED",
                    ]
                ),
                "REDACTED",
            ]
        )

        cases = (
            ("default_fixture", self._GITCOOKIES, expected_default),
            ("empty_file", "", ""),
            (
                "ya29_bearer_token",
                f".googlesource.com\tTRUE\t/\tTRUE\t2147483647\to\t{'ya29.'}fake-bearer-token\n",
                ".googlesource.com\tTRUE\t/\tTRUE\t2147483647\to\tREDACTED\n",
            ),
            (
                "git_user_ya29_token",
                f".googlesource.com\tTRUE\t/\tTRUE\t2147483647\to\tgit-user.google.com={'ya29.'}fake-bearer-token\n",
                ".googlesource.com\tTRUE\t/\tTRUE\t2147483647\to\tgit-user.google.com=REDACTED\n",
            ),
            (
                "non_google_gerrit_http_password",
                "review.example.org\tFALSE\t/\tTRUE\t2147483647\to\tgit-alice=PlaintextHttpPassword123!\n",
                "review.example.org\tFALSE\t/\tTRUE\t2147483647\to\tgit-alice=REDACTED\n",
            ),
            (
                "git_user_secret_containing_equals",
                "review.example.org\tFALSE\t/\tTRUE\t2147483647\to\tgit-bob.example.org=sec=ret==\n",
                "review.example.org\tFALSE\t/\tTRUE\t2147483647\to\tgit-bob.example.org=REDACTED\n",
            ),
            (
                "git_prefix_without_equals",
                "review.example.org\tFALSE\t/\tTRUE\t2147483647\to\tgit-raw-token-no-equals\n",
                "review.example.org\tFALSE\t/\tTRUE\t2147483647\to\tREDACTED\n",
            ),
            (
                "equals_without_git_prefix",
                "review.example.org\tFALSE\t/\tTRUE\t2147483647\to\tuser=secret-password\n",
                "review.example.org\tFALSE\t/\tTRUE\t2147483647\to\tREDACTED\n",
            ),
            (
                "httponly_sso_and_gsso_uberproxy_cookies",
                (
                    "#HttpOnly_login.corp.google.com\tFALSE\t/\tTRUE\t2147483647\tSSO\tsso-secret-cookie\n"
                    "#HttpOnly_.google.com\tTRUE\t/\tTRUE\t2147483647\tGSSO_Uberproxy\tQkxFRVBCTE9SUAo=\n"
                ),
                (
                    "#HttpOnly_login.corp.google.com\tFALSE\t/\tTRUE\t2147483647\tSSO\tREDACTED\n"
                    "#HttpOnly_.google.com\tTRUE\t/\tTRUE\t2147483647\tGSSO_Uberproxy\tREDACTED\n"
                ),
            ),
            (
                "malformed_lines_redacted",
                (
                    "\n"
                    "gitcookies 1/SECRET\n"
                    "malformed\tgit-user=1/legacy-secret\n"
                    "too\tmany\tfields\tTRUE\t2147483647\to\t1/extra-secret\textra\n"
                ),
                ("\nREDACTED\nREDACTED\nREDACTED\n"),
            ),
        )
        for name, raw_gitcookies, expected in cases:
            with (
                self.subTest(name=name),
                mock.patch(
                    "gclient_utils.FileRead", return_value=raw_gitcookies
                ),
            ):
                self.assertEqual(cookieAuth.debug_summary_state(), expected)

    def testGitcookiesHostAuthenticatorIsApplicable(self):
        a = gerrit_util.GitcookiesHostAuthenticator()
        self.assertTrue(
            a.is_applicable(gerrit_host="chromium-review.googlesource.com")
        )
        self.assertTrue(a.is_applicable(gerrit_host="some-review.example.com"))
        self.assertFalse(
            a.is_applicable(gerrit_host="gn-review.googlesource.com")
        )
        self.assertFalse(a.is_applicable(gerrit_host="another-key.example.org"))
        self.assertFalse(a.is_applicable())


class GceAuthenticatorTest(unittest.TestCase):
    def setUp(self):
        super(GceAuthenticatorTest, self).setUp()
        default_resp = mock.MagicMock(
            status=200,
            reason="OK",
            headers={"metadata-flavor": "Google"},
            read=mock.Mock(return_value=b""),
        )
        default_resp.__enter__.return_value = default_resp
        self.mock_open = mock.patch(
            "urllib.request.OpenerDirector.open",
            return_value=default_resp,
        ).start()
        mock.patch("os.getenv", return_value=None).start()
        mock.patch("gerrit_util.time_sleep").start()
        mock.patch("gerrit_util.time_time").start()
        self.addCleanup(mock.patch.stopall)

        # GceAuthenticator has class variables that cache the results. Build a
        # new class for every test to avoid inter-test dependencies.
        class GceAuthenticator(gerrit_util.GceAuthenticator):
            pass

        self.GceAuthenticator = GceAuthenticator

    def assertAuthenticatedToken(self, token: Optional[str]):
        conn = makeConn("chromium-review.googlesource.com")
        self.GceAuthenticator().authenticate(conn)
        if token is None:
            self.assertNotIn("Authorization", conn.req_headers)
        else:
            self.assertEqual(conn.req_headers["Authorization"], token)

    def testIsGce_EnvVarSkip(self, *_mocks):
        os.getenv.return_value = "1"
        self.assertFalse(self.GceAuthenticator.is_applicable())
        os.getenv.assert_called_once_with("SKIP_GCE_AUTH_FOR_GIT")

    def testIsGce_Error(self):
        self.mock_open.side_effect = urllib.error.URLError("error")
        self.assertFalse(self.GceAuthenticator.is_applicable())

    def testIsGce_500(self):
        self.mock_open.side_effect = urllib.error.HTTPError(
            "", 500, "Server Error", {}, io.BytesIO(b"")
        )
        self.assertFalse(self.GceAuthenticator.is_applicable())
        last_call = gerrit_util.time_sleep.mock_calls[-1]
        self.assertLessEqual(last_call, mock.call(43.0))

    def testIsGce_FailsThenSucceeds(self):
        response = mock.MagicMock(
            status=200,
            reason="OK",
            headers={"metadata-flavor": "Google"},
            read=mock.Mock(return_value=b"who cares"),
        )
        response.__enter__.return_value = response
        self.mock_open.side_effect = [
            urllib.error.HTTPError(
                "", 500, "Server Error", {}, io.BytesIO(b"")
            ),
            response,
        ]
        self.assertTrue(self.GceAuthenticator.is_applicable())

    def testIsGce_MetadataFlavorIsNotGoogle(self):
        response = mock.MagicMock(
            status=200,
            reason="OK",
            headers={},
            read=mock.Mock(return_value=b"who cares"),
        )
        response.__enter__.return_value = response
        self.mock_open.return_value = response
        self.assertFalse(self.GceAuthenticator.is_applicable())

    def testIsGce_ResultIsCached(self):
        response = mock.MagicMock(
            status=200,
            reason="OK",
            headers={"metadata-flavor": "Google"},
            read=mock.Mock(return_value=b"who cares"),
        )
        response.__enter__.return_value = response
        self.mock_open.side_effect = [response]
        self.assertTrue(self.GceAuthenticator.is_applicable())
        self.assertTrue(self.GceAuthenticator.is_applicable())
        self.mock_open.assert_called_once()

    def testIsApplicable_NonGooglesourceHost(self):
        for host in (
            "review.coreboot.org",
            "evil.com/.googlesource.com",
            "foo.googlesource.com:443@evil.com",
        ):
            self.assertFalse(
                self.GceAuthenticator.is_applicable(gerrit_host=host)
            )
        self.mock_open.assert_not_called()
        self.assertTrue(
            self.GceAuthenticator.is_applicable(
                gerrit_host="chromium-review.googlesource.com"
            )
        )

    def testGetAuthHeader_Error(self):
        self.mock_open.side_effect = urllib.error.URLError("error")
        self.assertAuthenticatedToken(None)

    def testGetAuthHeader_500(self):
        self.mock_open.side_effect = urllib.error.HTTPError(
            "", 500, "Server Error", {}, io.BytesIO(b"")
        )
        self.assertAuthenticatedToken(None)

    def testGetAuthHeader_Non200(self):
        self.mock_open.side_effect = urllib.error.HTTPError(
            "", 403, "Forbidden", {}, io.BytesIO(b"")
        )
        self.assertAuthenticatedToken(None)

    def testGetAuthHeader_OK(self):
        response = mock.MagicMock(
            status=200,
            reason="OK",
            headers={},
            read=mock.Mock(
                return_value=b'{"expires_in": 125, "token_type": "TYPE", "access_token": "TOKEN"}'
            ),
        )
        response.__enter__.return_value = response
        self.mock_open.return_value = response
        gerrit_util.time_time.return_value = 0
        self.assertAuthenticatedToken("TYPE TOKEN")

    def testGetAuthHeader_Cache(self):
        response = mock.MagicMock(
            status=200,
            reason="OK",
            headers={},
            read=mock.Mock(
                return_value=b'{"expires_in": 125, "token_type": "TYPE", "access_token": "TOKEN"}'
            ),
        )
        response.__enter__.return_value = response
        self.mock_open.return_value = response
        gerrit_util.time_time.return_value = 0
        self.assertAuthenticatedToken("TYPE TOKEN")
        self.assertAuthenticatedToken("TYPE TOKEN")
        self.mock_open.assert_called_once()

    def testGetAuthHeader_CacheOld(self):
        response = mock.MagicMock(
            status=200,
            reason="OK",
            headers={},
            read=mock.Mock(
                return_value=b'{"expires_in": 125, "token_type": "TYPE", "access_token": "TOKEN"}'
            ),
        )
        response.__enter__.return_value = response
        self.mock_open.return_value = response
        gerrit_util.time_time.side_effect = [0, 100, 200]
        self.assertAuthenticatedToken("TYPE TOKEN")
        self.assertAuthenticatedToken("TYPE TOKEN")
        self.assertEqual(2, self.mock_open.call_count)

    def testAuthenticate_NonGooglesourceHost(self):
        authn = self.GceAuthenticator()
        for host in (
            "review.coreboot.org",
            "evil.com/.googlesource.com",
            "foo.googlesource.com:443@evil.com",
        ):
            conn = makeConn(host)
            authn.authenticate(conn)
            self.assertNotIn("Authorization", conn.req_headers)
        self.mock_open.assert_not_called()

    def testAttemptAuthenticateWithReAuth(self):
        authn = self.GceAuthenticator()
        with mock.patch.object(authn, "authenticate") as mock_authenticate:
            conn = makeConn("chromium-review.googlesource.com")
            context = auth.ReAuthContext(
                host="chromium-review.googlesource.com", project="some/project"
            )
            self.assertTrue(
                authn.attempt_authenticate_with_reauth(conn, context)
            )
            mock_authenticate.assert_called_once_with(conn)

    def testAttemptAuthenticateWithReAuth_NonGooglesourceHost(self):
        authn = self.GceAuthenticator()
        conn = makeConn("review.coreboot.org")
        context = auth.ReAuthContext(
            host="review.coreboot.org", project="some/project"
        )
        self.assertFalse(authn.attempt_authenticate_with_reauth(conn, context))
        self.assertNotIn("Authorization", conn.req_headers)
        self.mock_open.assert_not_called()


class LuciContextAuthenticatorTest(unittest.TestCase):
    @mock.patch("auth.has_luci_context_local_auth", return_value=True)
    def testIsApplicable_GooglesourceOnly(self, mock_has_luci):
        for host in (
            "review.coreboot.org",
            "evil.com/.googlesource.com",
            "foo.googlesource.com:443@evil.com",
        ):
            self.assertFalse(
                gerrit_util.LuciContextAuthenticator.is_applicable(
                    gerrit_host=host
                )
            )
        mock_has_luci.assert_not_called()
        self.assertTrue(gerrit_util.LuciContextAuthenticator.is_applicable())
        self.assertTrue(
            gerrit_util.LuciContextAuthenticator.is_applicable(
                gerrit_host="chromium-review.googlesource.com"
            )
        )

    @mock.patch("auth.Authenticator")
    def testAuthenticate_GooglesourceOnly(self, mock_auth_cls):
        mock_token = mock.Mock(token="secret-luci-token")
        mock_auth_cls.return_value.get_access_token.return_value = mock_token
        luci_context_auth = gerrit_util.LuciContextAuthenticator()

        for host in (
            "review.coreboot.org",
            "evil.com/.googlesource.com",
            "foo.googlesource.com:443@evil.com",
        ):
            conn = makeConn(host)
            luci_context_auth.authenticate(conn)
            self.assertNotIn("Authorization", conn.req_headers)
        mock_auth_cls.return_value.get_access_token.assert_not_called()

        conn = makeConn("chromium-review.googlesource.com")
        luci_context_auth.authenticate(conn)
        self.assertEqual(
            conn.req_headers["Authorization"], "Bearer secret-luci-token"
        )
        mock_auth_cls.return_value.get_access_token.assert_called_once_with()

    @mock.patch("gerrit_util.LuciContextAuthenticator.authenticate")
    def testAttemptAuthenticateWithReAuth(self, mock_authenticate):
        luci_context_auth = gerrit_util.LuciContextAuthenticator()
        conn = makeConn("chromium-review.googlesource.com")
        context = auth.ReAuthContext(
            host="chromium-review.googlesource.com", project="some/project"
        )
        self.assertTrue(
            luci_context_auth.attempt_authenticate_with_reauth(conn, context)
        )
        mock_authenticate.assert_called_once_with(conn)

    @mock.patch("auth.Authenticator")
    def testAttemptAuthenticateWithReAuth_NonGooglesourceHost(
        self, mock_auth_cls
    ):
        luci_context_auth = gerrit_util.LuciContextAuthenticator()
        conn = makeConn("review.coreboot.org")
        context = auth.ReAuthContext(
            host="review.coreboot.org", project="some/project"
        )
        self.assertFalse(
            luci_context_auth.attempt_authenticate_with_reauth(conn, context)
        )
        self.assertNotIn("Authorization", conn.req_headers)
        mock_auth_cls.return_value.get_access_token.assert_not_called()


class GitCredsAuthenticatorTest(unittest.TestCase):
    def setUp(self):
        super(GitCredsAuthenticatorTest, self).setUp()
        self.authenticator = gerrit_util.ChainedAuthenticator(
            [gerrit_util.GitCredsAuthenticator()]
        )

    @mock.patch(
        "gerrit_util.GitCredsAuthenticator.gerrit_account_exists",
        return_value=True,
    )
    @mock.patch(
        "gerrit_util.GitCredsAuthenticator._is_usehttppath_set",
        return_value=True,
    )
    @mock.patch(
        "auth.GerritAuthenticator.get_access_token", return_value="Bearer abcd"
    )
    def testEnsureAuthenticated(self, mock_get_access_token, _set, _exists):
        bypassable, err_msg = self.authenticator.ensure_authenticated(
            gerrit_host="chromium-review.googlesource.com",
            git_host="chromium.googlesource.com",
        )
        mock_get_access_token.assert_called_once()
        self.assertTrue(bypassable, msg=err_msg)
        self.assertEqual(err_msg, "")

    @mock.patch(
        "gerrit_util.GitCredsAuthenticator.gerrit_account_exists",
        return_value=True,
    )
    def testIsApplicableGooglesourceOnly(self, mock_exists):
        auth_inst = gerrit_util.GitCredsAuthenticator()
        self.assertTrue(
            auth_inst.is_applicable(
                gerrit_host="chromium-review.googlesource.com"
            )
        )
        self.assertTrue(
            auth_inst.is_applicable(
                gerrit_host="CHROMIUM-REVIEW.GOOGLESOURCE.COM"
            )
        )
        mock_exists.reset_mock()
        for bad_host in (
            None,
            "",
            "review.coreboot.org",
            "googlesource.com",
            ".googlesource.com",
            "googlesource.com.evil.com",
            "evil.com/.googlesource.com",
            "chromium-review.googlesource.com:443",
            "foo.googlesource.com:443@evil.com",
            "https://chromium-review.googlesource.com",
            "chromium-review.google\u017fource.com",
        ):
            self.assertFalse(auth_inst.is_applicable(gerrit_host=bad_host))
        mock_exists.assert_not_called()

    @mock.patch(
        "auth.GerritAuthenticator.get_access_token",
        return_value="secret-oauth-token",
    )
    def testAuthenticate_GooglesourceOnly(self, mock_get_access_token):
        auth_inst = gerrit_util.GitCredsAuthenticator()
        for host in (
            "review.coreboot.org",
            "evil.com/.googlesource.com",
            "foo.googlesource.com:443@evil.com",
        ):
            conn = makeConn(host)
            auth_inst.authenticate(conn)
            self.assertNotIn("Authorization", conn.req_headers)
        mock_get_access_token.assert_not_called()

        conn = makeConn("chromium-review.googlesource.com")
        auth_inst.authenticate(conn)
        self.assertEqual(
            conn.req_headers["Authorization"], "Bearer secret-oauth-token"
        )
        mock_get_access_token.assert_called_once_with()

    @mock.patch(
        "auth.GerritAuthenticator.get_authorization_header",
        return_value="BearerReAuth xyz",
    )
    def testAttemptAuthenticateWithReAuth_NonGooglesourceHost(
        self, mock_get_header
    ):
        auth_inst = gerrit_util.GitCredsAuthenticator()
        conn = makeConn("review.coreboot.org")
        context = auth.ReAuthContext(
            host="review.coreboot.org", project="some/project"
        )
        self.assertFalse(
            auth_inst.attempt_authenticate_with_reauth(conn, context)
        )
        self.assertNotIn("Authorization", conn.req_headers)
        mock_get_header.assert_not_called()

    @mock.patch("gerrit_util.EnsureAccountExists")
    def testGerritAccountExists_NonGooglesourceHost(self, mock_ensure_account):
        for host in (
            "review.coreboot.org",
            "evil.com/.googlesource.com",
            "foo.googlesource.com:443@evil.com",
        ):
            self.assertFalse(
                gerrit_util.GitCredsAuthenticator.gerrit_account_exists(host)
            )
        mock_ensure_account.assert_not_called()

    @mock.patch("gerrit_util.EnsureAccountExists")
    def testGerritAccountExists_Success(self, mock_ensure_account):
        self.assertTrue(
            gerrit_util.GitCredsAuthenticator.gerrit_account_exists(
                "chromium-review.googlesource.com"
            )
        )

    @mock.patch(
        "gerrit_util.EnsureAccountExists",
        side_effect=auth.GitLoginRequiredError(),
    )
    def testGerritAccountExists_GitLoginRequired(self, mock_ensure_account):
        self.assertFalse(
            gerrit_util.GitCredsAuthenticator.gerrit_account_exists(
                "chromium-review.googlesource.com"
            )
        )

    @mock.patch(
        "gerrit_util.EnsureAccountExists",
        side_effect=gerrit_util.GerritError(400, "Account missing"),
    )
    def testGerritAccountExists_400Error(self, mock_ensure_account):
        self.assertFalse(
            gerrit_util.GitCredsAuthenticator.gerrit_account_exists(
                "chromium-review.googlesource.com"
            )
        )

    @mock.patch(
        "gerrit_util.EnsureAccountExists",
        side_effect=gerrit_util.GerritError(404, "Account missing"),
    )
    def testGerritAccountExists_404Error(self, mock_ensure_account):
        self.assertFalse(
            gerrit_util.GitCredsAuthenticator.gerrit_account_exists(
                "chromium-review.googlesource.com"
            )
        )

    @mock.patch(
        "gerrit_util.EnsureAccountExists",
        side_effect=gerrit_util.GerritError(500, "Internal Server Error"),
    )
    def testGerritAccountExists_500ErrorRaises(self, mock_ensure_account):
        with self.assertRaises(gerrit_util.GerritError):
            gerrit_util.GitCredsAuthenticator.gerrit_account_exists(
                "chromium-review.googlesource.com"
            )

    @mock.patch(
        "gerrit_util.GitCredsAuthenticator._is_usehttppath_set",
        return_value=True,
    )
    @mock.patch(
        "gerrit_util.GitCredsAuthenticator.gerrit_account_exists",
        return_value=True,
    )
    @mock.patch(
        "auth.GerritAuthenticator.get_authorization_header",
        return_value="BearerReAuth xyz",
    )
    def testEnsureAuthenticatedWithReAuth(
        self, mock_get_authorization_header, _exists, _set
    ):
        reauth_context = auth.ReAuthContext(
            host="chromium-review.googlesource.com", project="chromium/src"
        )
        gerrit_host = "chromium-review.googlesource.com"
        git_host = "chromium.googlesource.com"

        bypassable, err_msg = self.authenticator.ensure_authenticated(
            gerrit_host=gerrit_host,
            git_host=git_host,
            reauth_context=reauth_context,
        )

        mock_get_authorization_header.assert_called_once_with(reauth_context)
        self.assertTrue(bypassable, msg=err_msg)
        self.assertEqual(err_msg, "")

    @mock.patch(
        "gerrit_util.GitCredsAuthenticator._is_usehttppath_set",
        return_value=False,
    )
    @mock.patch(
        "gerrit_util.GitCredsAuthenticator.gerrit_account_exists",
        return_value=True,
    )
    @mock.patch(
        "auth.GerritAuthenticator.get_authorization_header",
        return_value="BearerReAuth xyz",
    )
    def testEnsureAuthenticatedMissingUseHttpPath(
        self, mock_get_header, _exists, _set
    ):
        reauth_context = auth.ReAuthContext(
            host="chromium-review.googlesource.com", project="chromium/src"
        )
        gerrit_host = "chromium-review.googlesource.com"
        git_host = "chromium.googlesource.com"

        bypassable, err_msg = self.authenticator.ensure_authenticated(
            gerrit_host=gerrit_host,
            git_host=git_host,
            reauth_context=reauth_context,
        )

        self.assertFalse(bypassable)
        self.assertRegex(err_msg, "You have not set credential.useHttpPath")

    @mock.patch(
        "auth.GerritAuthenticator.get_authorization_header",
        side_effect=auth.GitReAuthRequiredError(),
    )
    @mock.patch(
        "gerrit_util.GitCredsAuthenticator.gerrit_account_exists",
        return_value=True,
    )
    def testEnsureAuthenticatedMissingReAuth(
        self, _exists, mock_get_authorization_header
    ):
        gerrit_host = "chromium-review.googlesource.com"
        git_host = "chromium.googlesource.com"
        reauth_context = auth.ReAuthContext(
            host="chromium-review.googlesource.com", project="chromium/src"
        )

        bypassable, err_msg = self.authenticator.ensure_authenticated(
            gerrit_host=gerrit_host,
            git_host=git_host,
            reauth_context=reauth_context,
        )

        mock_get_authorization_header.assert_called_once_with(reauth_context)
        self.assertFalse(bypassable)
        self.assertRegex(err_msg, "You have not done ReAuth")


class GerritUtilTest(unittest.TestCase):
    def setUp(self):
        super(GerritUtilTest, self).setUp()
        mock.patch("gerrit_util.LOGGER").start()
        mock.patch("gerrit_util.time_sleep").start()
        mock.patch("metrics.collector").start()
        mock.patch(
            "metrics_utils.extract_http_metrics", return_value="http_metrics"
        ).start()
        self.addCleanup(mock.patch.stopall)

    def testIsGooglesourceHost(self):
        for valid_host in (
            "chromium-review.googlesource.com",
            "a.googlesource.com",
            "a.b.googlesource.com",
            "CHROMIUM-REVIEW.GOOGLESOURCE.COM",
        ):
            self.assertTrue(
                gerrit_util._IsGooglesourceHost(valid_host), msg=valid_host
            )

        for invalid_host in (
            None,
            "",
            "googlesource.com",
            ".googlesource.com",
            "a..googlesource.com",
            "-bad.googlesource.com",
            "bad-.googlesource.com",
            "googlesource.com.evil.com",
            "notgooglesource.com",
            "review.coreboot.org",
            "evil.com/.googlesource.com",
            "chromium-review.googlesource.com/path",
            "foo.googlesource.com:443@evil.com",
            "https://chromium-review.googlesource.com",
            "chromium-review.googlesource.com:443",
            "chromium-review.googlesource.com:0",
            "chromium-review.googlesource.com:",
            "chromium-review.googlesource.com\n",
            "chromium-review.google\u017fource.com",
            "\u212a.googlesource.com",
        ):
            self.assertFalse(
                gerrit_util._IsGooglesourceHost(invalid_host),
                msg=repr(invalid_host),
            )

    def testQueryString(self):
        self.assertEqual("", gerrit_util._QueryString([]))
        self.assertEqual(
            "first%20param%2B", gerrit_util._QueryString([], "first param+")
        )
        self.assertEqual(
            "key:val+foo:bar",
            gerrit_util._QueryString([("key", "val"), ("foo", "bar")]),
        )
        self.assertEqual(
            "first%20param%2B+key:val+foo:bar",
            gerrit_util._QueryString(
                [("key", "val"), ("foo", "bar")], "first param+"
            ),
        )

    @mock.patch("gerrit_util.CookiesAuthenticator._get_auth_for_host")
    @mock.patch("gerrit_util._Authenticator.get")
    def testCreateHttpConn_Basic(self, mockAuth, cookieAuth):
        mockAuth.return_value = gerrit_util.CookiesAuthenticator()
        cookieAuth.return_value = None

        conn = gerrit_util.CreateHttpConn("host.example.com", "foo/bar")
        self.assertEqual("host.example.com", conn.req_host)
        self.assertEqual(
            {
                "uri": "https://host.example.com/a/foo/bar",
                "method": "GET",
                "headers": {},
                "body": None,
            },
            conn.req_params,
        )

    @mock.patch("gerrit_util.CookiesAuthenticator._get_auth_for_host")
    @mock.patch("gerrit_util._Authenticator.get")
    def testCreateHttpConn_Authenticated(self, mockAuth, cookieAuth):
        mockAuth.return_value = gerrit_util.CookiesAuthenticator()
        cookieAuth.return_value = (None, "token")

        conn = gerrit_util.CreateHttpConn(
            "host.example.com", "foo/bar", headers={"header": "value"}
        )
        self.assertEqual("host.example.com", conn.req_host)
        self.assertEqual(
            {
                "uri": "https://host.example.com/a/foo/bar",
                "method": "GET",
                "headers": {"Authorization": "Bearer token", "header": "value"},
                "body": None,
            },
            conn.req_params,
        )

    @mock.patch("gerrit_util.CookiesAuthenticator._get_auth_for_host")
    @mock.patch("gerrit_util._Authenticator")
    def testCreateHttpConn_Body(self, mockAuth, cookieAuth):
        mockAuth.return_value = gerrit_util.CookiesAuthenticator()
        cookieAuth.return_value = None

        conn = gerrit_util.CreateHttpConn(
            "host.example.com",
            "foo/bar",
            body={"l": [1, 2, 3], "d": {"k": "v"}},
        )
        self.assertEqual("host.example.com", conn.req_host)
        self.assertEqual(
            {
                "uri": "https://host.example.com/a/foo/bar",
                "method": "GET",
                "headers": {"Content-Type": "application/json"},
                "body": '{"d": {"k": "v"}, "l": [1, 2, 3]}',
            },
            conn.req_params,
        )

    @mock.patch("gerrit_util.EnsureAccountExists")
    @mock.patch("auth.GerritAuthenticator.get_access_token")
    @mock.patch("gerrit_util.GceAuthenticator._get")
    @mock.patch(
        "gerrit_util.ssoHelper.find_cmd", return_value="/fake/git-remote-sso"
    )
    @mock.patch("scm.GIT.GetConfig", return_value="user@google.com")
    def testCreateHttpConn_ChainedAuthenticatorSkipsNonGooglesource(
        self,
        _mockGitConfig,
        _mockSsoCmd,
        mockGceGet,
        mockGetAccessToken,
        mockEnsureAccount,
    ):
        chained = gerrit_util.ChainedAuthenticator(
            [
                gerrit_util.SSOAuthenticator(),
                gerrit_util.GceAuthenticator(),
                gerrit_util.GitCredsAuthenticator(),
                gerrit_util.NoAuthenticator(),
            ]
        )
        conn = gerrit_util.CreateHttpConn(
            "review.coreboot.org", "changes/", authenticator=chained
        )
        self.assertNotIn("Authorization", conn.req_headers)
        self.assertIsNone(conn.cookie_jar)
        mockGceGet.assert_not_called()
        mockGetAccessToken.assert_not_called()
        mockEnsureAccount.assert_not_called()

    @mock.patch("auth.GerritAuthenticator.get_authorization_header")
    @mock.patch("gerrit_util._Authenticator.get")
    def testCreateHttpConn_ReAuth(self, mockAuth, getAuthHeader):
        mockAuth.return_value = gerrit_util.GitCredsAuthenticator()
        getAuthHeader.return_value = "BearerReAuth cafe"
        reauth_context = auth.ReAuthContext(
            host="chromium", project="infra/infra"
        )
        gerrit_util.CreateHttpConn(
            "chromium-review.googlesource.com",
            "foo/bar",
            reauth_context=reauth_context,
        )
        getAuthHeader.assert_called_with(reauth_context)

    @mock.patch("auth.GerritAuthenticator.get_access_token")
    @mock.patch("auth.GerritAuthenticator.get_authorization_header")
    @mock.patch("gerrit_util._Authenticator.get")
    def testCreateHttpConn_ReAuthOptional(
        self, mockAuth, getAuthHeader, getAccessToken
    ):
        mockAuth.return_value = gerrit_util.GitCredsAuthenticator()
        getAuthHeader.side_effect = auth.GitReAuthRequiredError()
        getAccessToken.return_value = "decafe"
        reauth_context = auth.ReAuthContext(
            host="chromium", project="infra/infra"
        )
        gerrit_util.CreateHttpConn(
            "chromium-review.googlesource.com",
            "foo/bar",
            reauth_context=reauth_context,
            reauth_is_optional=True,
        )
        getAuthHeader.assert_called_with(reauth_context)
        getAccessToken.assert_called()

    @mock.patch("auth.GerritAuthenticator.get_access_token")
    @mock.patch("auth.GerritAuthenticator.get_authorization_header")
    @mock.patch("gerrit_util._Authenticator.get")
    def testCreateHttpConn_ReAuthRequired(
        self, mockAuth, getAuthHeader, getAccessToken
    ):
        mockAuth.return_value = gerrit_util.GitCredsAuthenticator()
        getAuthHeader.side_effect = auth.GitReAuthRequiredError()
        getAccessToken.return_value = "decafe"
        reauth_context = auth.ReAuthContext(
            host="chromium", project="infra/infra"
        )
        with self.assertRaises(auth.GitReAuthRequiredError):
            gerrit_util.CreateHttpConn(
                "chromium-review.googlesource.com",
                "foo/bar",
                reauth_context=reauth_context,
                reauth_is_optional=False,
            )
        getAuthHeader.assert_called_with(reauth_context)
        getAccessToken.assert_not_called()

    def testReadHttpResponse_200(self):
        conn = mock.Mock()
        conn.req_params = {"uri": "uri", "method": "method"}
        conn.request.return_value = (
            mock.Mock(status=200),
            b"content\xe2\x9c\x94",
        )

        content = gerrit_util.ReadHttpResponse(conn)
        self.assertEqual("content✔", content.getvalue())
        metrics.collector.add_repeated.assert_called_once_with(
            "http_requests", "http_metrics"
        )

    def testReadHttpResponse_AuthenticationIssue(self):
        for status in (302, 401, 403):
            response = mock.Mock(status=status)
            response.get.return_value = None
            conn = mock.Mock(req_params={"uri": "uri", "method": "method"})
            conn.request.return_value = (response, b"")

            with mock.patch("sys.stdout", StringIO()):
                with self.assertRaises(gerrit_util.GerritError) as cm:
                    gerrit_util.ReadHttpResponse(conn)

                self.assertEqual(status, cm.exception.http_status)
                self.assertIn(
                    "Your Gerrit credentials might be misconfigured",
                    sys.stdout.getvalue(),
                )

    def testReadHttpResponse_ClientError(self):
        conn = mock.Mock(req_params={"uri": "uri", "method": "method"})
        conn.request.return_value = (mock.Mock(status=404), b"")

        with self.assertRaises(gerrit_util.GerritError) as cm:
            gerrit_util.ReadHttpResponse(conn)

        self.assertEqual(404, cm.exception.http_status)

    def readHttpResponse_ServerErrorHelper(self, status):
        conn = mock.Mock(req_params={"uri": "uri", "method": "method"})
        conn.request.return_value = (mock.Mock(status=status), b"")

        with self.assertRaises(gerrit_util.GerritError) as cm:
            gerrit_util.ReadHttpResponse(conn)

        self.assertEqual(status, cm.exception.http_status)
        self.assertEqual(gerrit_util.TRY_LIMIT, len(conn.request.mock_calls))
        last_call = gerrit_util.time_sleep.mock_calls[-1]
        self.assertLessEqual(last_call, mock.call(422.0))

    def testReadHttpResponse_ServerError(self):
        self.readHttpResponse_ServerErrorHelper(status=404)
        self.readHttpResponse_ServerErrorHelper(status=409)
        self.readHttpResponse_ServerErrorHelper(status=429)
        self.readHttpResponse_ServerErrorHelper(status=500)

    def testReadHttpResponse_ServerErrorAndSuccess(self):
        conn = mock.Mock(req_params={"uri": "uri", "method": "method"})
        conn.request.side_effect = [
            (mock.Mock(status=500), b""),
            (mock.Mock(status=200), b"content\xe2\x9c\x94"),
        ]

        self.assertEqual(
            "content✔", gerrit_util.ReadHttpResponse(conn).getvalue()
        )
        self.assertEqual(2, len(conn.request.mock_calls))
        gerrit_util.time_sleep.assert_called_once_with(12.0)

    def testReadHttpResponse_TimeoutAndSuccess(self):
        conn = mock.Mock(
            req_host="example.com",
            req_params={"uri": "uri", "method": "method"},
        )
        conn.request.side_effect = [
            socket.timeout("timeout"),
            (mock.Mock(status=200), b"content\xe2\x9c\x94"),
        ]

        self.assertEqual(
            "content✔", gerrit_util.ReadHttpResponse(conn).getvalue()
        )
        self.assertEqual(2, len(conn.request.mock_calls))
        gerrit_util.time_sleep.assert_called_once_with(12.0)

    def testReadHttpResponse_SetMaxTries(self):
        conn = mock.Mock(
            req_host="example.com",
            req_params={"uri": "uri", "method": "method"},
        )
        conn.request.side_effect = [
            (mock.Mock(status=409), b"error!"),
            (mock.Mock(status=409), b"error!"),
            (mock.Mock(status=409), b"error!"),
        ]

        self.assertRaises(
            gerrit_util.GerritError,
            gerrit_util.ReadHttpResponse,
            conn,
            max_tries=2,
        )
        self.assertEqual(2, len(conn.request.mock_calls))
        gerrit_util.time_sleep.assert_called_once_with(12.0)

    def testReadHttpResponse_Expected404(self):
        conn = mock.Mock()
        conn.req_params = {"uri": "uri", "method": "method"}
        conn.request.return_value = (
            mock.Mock(status=404),
            b"content\xe2\x9c\x94",
        )

        content = gerrit_util.ReadHttpResponse(conn, (404,))
        self.assertEqual("", content.getvalue())

    @mock.patch("gerrit_util.ReadHttpResponse")
    def testReadHttpJsonResponse_NotJSON(self, mockReadHttpResponse):
        mockReadHttpResponse.return_value = StringIO("not json")
        with self.assertRaises(gerrit_util.GerritError) as cm:
            gerrit_util.ReadHttpJsonResponse(None)
        self.assertEqual(cm.exception.http_status, 200)
        self.assertEqual(
            cm.exception.message, "(200) Unexpected json output: not json"
        )

    @mock.patch("gerrit_util.ReadHttpResponse")
    def testReadHttpJsonResponse_EmptyValue(self, mockReadHttpResponse):
        mockReadHttpResponse.return_value = StringIO(")]}'")
        self.assertEqual(gerrit_util.ReadHttpJsonResponse(None), {})

    @mock.patch("gerrit_util.ReadHttpResponse")
    def testReadHttpJsonResponse_JSON(self, mockReadHttpResponse):
        expected_value = {"foo": "bar", "baz": [1, "2", 3]}
        mockReadHttpResponse.return_value = StringIO(
            ")]}'\n" + json.dumps(expected_value)
        )
        self.assertEqual(expected_value, gerrit_util.ReadHttpJsonResponse(None))

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    def testQueryChanges(self, mockJsonResponse, mockCreateHttpConn):
        gerrit_util.QueryChanges(
            "host",
            [("key", "val"), ("foo", "bar baz")],
            "first param",
            limit=500,
            o_params=["PARAM_A", "PARAM_B"],
            start="start",
        )
        mockCreateHttpConn.assert_called_once_with(
            "host",
            (
                "changes/?q=first%20param+key:val+foo:bar+baz"
                "&start=start"
                "&n=500"
                "&o=PARAM_A"
                "&o=PARAM_B"
            ),
            timeout=30.0,
        )

    def testQueryChanges_NoParams(self):
        self.assertRaises(RuntimeError, gerrit_util.QueryChanges, "host", [])

    @mock.patch("gerrit_util.QueryChanges")
    def testGenerateAllChanges(self, mockQueryChanges):
        mockQueryChanges.side_effect = [
            # First results page
            [
                {"_number": "4"},
                {"_number": "3"},
                {"_number": "2", "_more_changes": True},
            ],
            # Second results page, there are new changes, so second page
            # includes some results from the first page.
            [
                {"_number": "2"},
                {"_number": "1"},
            ],
            # GenerateAllChanges queries again from the start to get any new
            # changes (5 in this case).
            [
                {"_number": "5"},
                {"_number": "4"},
                {"_number": "3", "_more_changes": True},
            ],
        ]

        changes = list(gerrit_util.GenerateAllChanges("host", "params"))
        self.assertEqual(
            [
                {"_number": "4"},
                {"_number": "3"},
                {"_number": "2", "_more_changes": True},
                {"_number": "1"},
                {"_number": "5"},
            ],
            changes,
        )
        self.assertEqual(
            [
                mock.call("host", "params", None, 500, None, 0),
                mock.call("host", "params", None, 500, None, 3),
                mock.call("host", "params", None, 500, None, 0),
            ],
            mockQueryChanges.mock_calls,
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    def testIsCodeOwnersEnabledOnRepo_Disabled(
        self, mockJsonResponse, mockCreateHttpConn
    ):
        mockJsonResponse.return_value = {"status": {"disabled": True}}
        self.assertFalse(gerrit_util.IsCodeOwnersEnabledOnRepo("host", "repo"))

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    def testIsCodeOwnersEnabledOnRepo_Enabled(
        self, mockJsonResponse, mockCreateHttpConn
    ):
        mockJsonResponse.return_value = {"status": {}}
        self.assertTrue(gerrit_util.IsCodeOwnersEnabledOnRepo("host", "repo"))

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.GetChangeDetail")
    def testSetReview_ReAuth(
        self, mockGetChangeDetail, mockJsonResponse, mockCreateHttpConn
    ):
        mockJsonResponse.return_value = {"labels": {"Code-Review": 1}}
        mockGetChangeDetail.return_value = {
            "project": "infra/infra",
        }

        gerrit_util.SetReview("chromium", 123456, labels={"Code-Review": 1})

        # Check `project` in reauth_context is backfilled.
        mockGetChangeDetail.assert_called_with("chromium", 123456)
        httpConnKwargs = mockCreateHttpConn.call_args[1]
        self.assertIn("reauth_context", httpConnKwargs)
        self.assertEqual(
            auth.ReAuthContext(host="chromium", project="infra/infra"),
            httpConnKwargs["reauth_context"],
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.GetChangeDetail")
    def testSetReview_ReAuth_WithProject(
        self, mockGetChangeDetail, mockJsonResponse, mockCreateHttpConn
    ):
        mockJsonResponse.return_value = {"labels": {"Code-Review": 1}}

        gerrit_util.SetReview(
            "chromium",
            123456,
            labels={"Code-Review": 1},
            project="infra/experimental",
        )

        # Check reauth_context uses the given project.
        mockGetChangeDetail.assert_not_called()
        httpConnKwargs = mockCreateHttpConn.call_args[1]
        self.assertIn("reauth_context", httpConnKwargs)
        self.assertEqual(
            auth.ReAuthContext(host="chromium", project="infra/experimental"),
            httpConnKwargs["reauth_context"],
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.GetChangeDetail")
    def testSetReview_ReAuthNotNeededForCQLabel(
        self, mockGetChangeDetail, mockJsonResponse, mockCreateHttpConn
    ):
        mockJsonResponse.return_value = {
            "ready": True,
            "labels": {"Commit-Queue": 1},
        }

        gerrit_util.SetReview(
            "chromium", 123456, msg="test", labels={"Commit-Queue": 1}
        )

        # ReAuth not needed.
        mockGetChangeDetail.assert_not_called()
        httpConnKwargs = mockCreateHttpConn.call_args[1]
        self.assertIsNone(httpConnKwargs.get("reauth_context", None))

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.GetChangeDetail")
    def testSetReview_ReAuthNotNeededWithoutLabels(
        self, mockGetChangeDetail, mockJsonResponse, mockCreateHttpConn
    ):
        mockJsonResponse.return_value = {"ready": True}

        gerrit_util.SetReview("chromium", 123456, msg="test")

        # ReAuth not needed.
        mockGetChangeDetail.assert_not_called()
        httpConnKwargs = mockCreateHttpConn.call_args[1]
        self.assertIsNone(httpConnKwargs.get("reauth_context", None))

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.gclient_utils.IsEnvAi", return_value=True)
    def testSetReview_Ai(
        self, mockIsEnvAi, mockJsonResponse, mockCreateHttpConn
    ):
        mockJsonResponse.return_value = {}
        comments = {"file.py": [{"line": 1, "message": "hello"}]}
        gerrit_util.SetReview("chromium", 123456, comments=comments)

        expected_body = {
            "drafts": "KEEP",
            "comments": {
                "file.py": [{"line": 1, "message": "hello", "is_ai": True}]
            },
        }
        mockCreateHttpConn.assert_called_once_with(
            "chromium",
            "changes/123456/revisions/current/review",
            reqtype="POST",
            body=expected_body,
            reauth_context=None,
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.gclient_utils.IsEnvAi", return_value=False)
    def testSetReview_NotAi(
        self, mockIsEnvAi, mockJsonResponse, mockCreateHttpConn
    ):
        mockJsonResponse.return_value = {}
        comments = {"file.py": [{"line": 1, "message": "hello"}]}
        gerrit_util.SetReview("chromium", 123456, comments=comments)

        expected_body = {
            "drafts": "KEEP",
            "comments": {"file.py": [{"line": 1, "message": "hello"}]},
        }
        mockCreateHttpConn.assert_called_once_with(
            "chromium",
            "changes/123456/revisions/current/review",
            reqtype="POST",
            body=expected_body,
            reauth_context=None,
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.gclient_utils.IsEnvAi", return_value=True)
    def testCreateDraft_Ai(
        self, mockIsEnvAi, mockJsonResponse, mockCreateHttpConn
    ):
        mockJsonResponse.return_value = {}
        body = {"line": 1, "message": "hello"}
        gerrit_util.CreateDraft("chromium", 123456, body=body)

        expected_body = {"line": 1, "message": "hello", "is_ai": True}
        mockCreateHttpConn.assert_called_once_with(
            "chromium",
            "changes/123456/revisions/current/drafts",
            reqtype="PUT",
            body=expected_body,
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.gclient_utils.IsEnvAi", return_value=False)
    def testCreateDraft_NotAi(
        self, mockIsEnvAi, mockJsonResponse, mockCreateHttpConn
    ):
        mockJsonResponse.return_value = {}
        body = {"line": 1, "message": "hello"}
        gerrit_util.CreateDraft("chromium", 123456, body=body)

        expected_body = {"line": 1, "message": "hello"}
        mockCreateHttpConn.assert_called_once_with(
            "chromium",
            "changes/123456/revisions/current/drafts",
            reqtype="PUT",
            body=expected_body,
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    def testGetChangeDrafts(self, mockJsonResponse, mockCreateHttpConn):
        """Verifies that `GetChangeDrafts` queries the change drafts endpoint."""
        mockJsonResponse.return_value = {"a.py": [{"id": "d1"}]}
        self.assertEqual(
            {"a.py": [{"id": "d1"}]},
            gerrit_util.GetChangeDrafts("chromium", 123456),
        )
        mockCreateHttpConn.assert_called_once_with(
            "chromium", "changes/123456/drafts"
        )
        mockJsonResponse.assert_called_once_with(
            mockCreateHttpConn.return_value
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.gclient_utils.IsEnvAi", return_value=True)
    def testCallGerritApi_Drafts_Ai(
        self, mockIsEnvAi, mockJsonResponse, mockCreateHttpConn
    ):
        mockJsonResponse.return_value = {}
        body = {"path": "/PATCHSET_LEVEL", "message": "hello"}
        gerrit_util.CallGerritApi(
            "chromium",
            "/changes/123456/revisions/current/drafts",
            reqtype="PUT",
            body=body,
            accept_statuses=[200, 201],
        )

        expected_body = {
            "path": "/PATCHSET_LEVEL",
            "message": "hello",
            "is_ai": True,
        }
        mockCreateHttpConn.assert_called_once_with(
            "chromium",
            "/changes/123456/revisions/current/drafts",
            reqtype="PUT",
            body=expected_body,
        )
        mockJsonResponse.assert_called_once_with(
            mockCreateHttpConn.return_value, accept_statuses=[200, 201]
        )

        # Updating an existing draft at /a/changes/.../drafts/<id> also injects
        # is_ai, while respecting an explicit is_ai=False when provided.
        mockCreateHttpConn.reset_mock()
        update_body = {"path": "foo.cc", "line": 1, "message": "updated"}
        gerrit_util.CallGerritApi(
            "chromium",
            "/a/changes/123456/revisions/current/drafts/draft-uuid",
            reqtype="PUT",
            body=update_body,
        )
        mockCreateHttpConn.assert_called_once_with(
            "chromium",
            "/a/changes/123456/revisions/current/drafts/draft-uuid",
            reqtype="PUT",
            body={
                "path": "foo.cc",
                "line": 1,
                "message": "updated",
                "is_ai": True,
            },
        )

        mockCreateHttpConn.reset_mock()
        explicit_body = {
            "path": "/PATCHSET_LEVEL",
            "message": "human override",
            "is_ai": False,
        }
        gerrit_util.CallGerritApi(
            "chromium",
            "/changes/123456/revisions/current/drafts",
            reqtype="PUT",
            body=explicit_body,
        )
        mockCreateHttpConn.assert_called_once_with(
            "chromium",
            "/changes/123456/revisions/current/drafts",
            reqtype="PUT",
            body={
                "path": "/PATCHSET_LEVEL",
                "message": "human override",
                "is_ai": False,
            },
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.gclient_utils.IsEnvAi", return_value=False)
    def testCallGerritApi_Drafts_NotAi(
        self, mockIsEnvAi, mockJsonResponse, mockCreateHttpConn
    ):
        mockJsonResponse.return_value = {}
        body = {"path": "/PATCHSET_LEVEL", "message": "hello"}
        gerrit_util.CallGerritApi(
            "chromium",
            "/changes/123456/revisions/current/drafts",
            reqtype="PUT",
            body=body,
        )

        expected_body = {"path": "/PATCHSET_LEVEL", "message": "hello"}
        mockCreateHttpConn.assert_called_once_with(
            "chromium",
            "/changes/123456/revisions/current/drafts",
            reqtype="PUT",
            body=expected_body,
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.gclient_utils.IsEnvAi", return_value=True)
    def testCallGerritApi_Review_Ai(
        self, mockIsEnvAi, mockJsonResponse, mockCreateHttpConn
    ):
        mockJsonResponse.return_value = {}
        body = {"comments": {"file.py": [{"line": 1, "message": "hello"}]}}
        gerrit_util.CallGerritApi(
            "chromium",
            "/changes/123456/revisions/current/review",
            reqtype="POST",
            body=body,
        )

        expected_body = {
            "comments": {
                "file.py": [{"line": 1, "message": "hello", "is_ai": True}]
            }
        }
        mockCreateHttpConn.assert_called_once_with(
            "chromium",
            "/changes/123456/revisions/current/review",
            reqtype="POST",
            body=expected_body,
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.gclient_utils.IsEnvAi", return_value=True)
    def testCallGerritApi_OtherEndpoint_Ai(
        self, mockIsEnvAi, mockJsonResponse, mockCreateHttpConn
    ):
        mockJsonResponse.return_value = {}
        body = {"message": "abandoning"}
        gerrit_util.CallGerritApi(
            "chromium",
            "/changes/123456/abandon",
            reqtype="POST",
            body=body,
        )

        expected_body = {"message": "abandoning"}
        mockCreateHttpConn.assert_called_once_with(
            "chromium",
            "/changes/123456/abandon",
            reqtype="POST",
            body=expected_body,
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.GetChangeDetail")
    def testCherryPickWithConflicts(
        self, mockGetChangeDetail, mockJsonResponse, mockCreateHttpConn
    ):
        mockGetChangeDetail.return_value = {"project": "infra/infra"}
        mockJsonResponse.return_value = {"_number": 1}
        gerrit_util.CherryPick(
            "host", "change", "destination", allow_conflicts=True
        )
        mockGetChangeDetail.assert_called_once_with("host", "change")
        mockCreateHttpConn.assert_called_once_with(
            "host",
            "changes/change/revisions/current/cherrypick",
            reqtype="POST",
            body={"destination": "destination", "allow_conflicts": True},
            reauth_context=auth.ReAuthContext(
                host="host", project="infra/infra"
            ),
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.GetChangeDetail")
    def testCherryPick_WithProject(
        self, mockGetChangeDetail, mockJsonResponse, mockCreateHttpConn
    ):
        mockJsonResponse.return_value = {"_number": 1}
        gerrit_util.CherryPick(
            "host",
            "change",
            "destination",
            project="infra/experimental",
        )
        mockGetChangeDetail.assert_not_called()
        mockCreateHttpConn.assert_called_once_with(
            "host",
            "changes/change/revisions/current/cherrypick",
            reqtype="POST",
            body={"destination": "destination"},
            reauth_context=auth.ReAuthContext(
                host="host", project="infra/experimental"
            ),
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    def testCherryPickCommit_ReAuth(self, mockJsonResponse, mockCreateHttpConn):
        mockJsonResponse.return_value = {"_number": 1}
        gerrit_util.CherryPickCommit(
            "host", "chromium/src", "deadbeef", "refs/branch-heads/1234"
        )
        mockCreateHttpConn.assert_called_once_with(
            "host",
            "projects/chromium%2Fsrc/commits/deadbeef/cherrypick",
            reqtype="POST",
            body={"destination": "refs/branch-heads/1234"},
            reauth_context=auth.ReAuthContext(
                host="host", project="chromium/src"
            ),
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpResponse")
    @mock.patch("gerrit_util.GetChangeDetail")
    def testSetCommitMessage_ReAuth(
        self, mockGetChangeDetail, mockReadResponse, mockCreateHttpConn
    ):
        mockGetChangeDetail.return_value = {"project": "v8/v8"}
        gerrit_util.SetCommitMessage("chromium", 123456, "New msg")
        mockGetChangeDetail.assert_called_once_with("chromium", 123456)
        mockCreateHttpConn.assert_called_once_with(
            "chromium",
            "changes/123456/message",
            reqtype="PUT",
            body={"message": "New msg", "notify": "ALL"},
            reauth_context=auth.ReAuthContext(host="chromium", project="v8/v8"),
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpResponse")
    @mock.patch("gerrit_util.GetChangeDetail")
    def testSetCommitMessage_ReAuth_WithProject(
        self, mockGetChangeDetail, mockReadResponse, mockCreateHttpConn
    ):
        gerrit_util.SetCommitMessage(
            "chromium",
            123456,
            "New msg",
            notify="NONE",
            project="v8/v8",
        )
        mockGetChangeDetail.assert_not_called()
        mockCreateHttpConn.assert_called_once_with(
            "chromium",
            "changes/123456/message",
            reqtype="PUT",
            body={"message": "New msg", "notify": "NONE"},
            reauth_context=auth.ReAuthContext(host="chromium", project="v8/v8"),
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.GetChangeDetail")
    def testRebaseChange_ReAuth(
        self, mockGetChangeDetail, mockJsonResponse, mockCreateHttpConn
    ):
        mockGetChangeDetail.return_value = {"project": "infra/infra"}
        gerrit_util.RebaseChange("chromium", 123456, base="base_rev")
        mockGetChangeDetail.assert_called_once_with("chromium", 123456)
        mockCreateHttpConn.assert_called_once_with(
            "chromium",
            "changes/123456/rebase",
            reqtype="POST",
            body={"base": "base_rev"},
            reauth_context=auth.ReAuthContext(
                host="chromium", project="infra/infra"
            ),
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.GetChangeDetail")
    def testPublishChangeEdit_ReAuth(
        self, mockGetChangeDetail, mockJsonResponse, mockCreateHttpConn
    ):
        mockGetChangeDetail.return_value = {"project": "infra/infra"}
        gerrit_util.PublishChangeEdit("chromium", 123456, notify=False)
        mockGetChangeDetail.assert_called_once_with("chromium", 123456)
        mockCreateHttpConn.assert_called_once_with(
            "chromium",
            "changes/123456/edit:publish",
            reqtype="POST",
            body={"notify": "NONE"},
            reauth_context=auth.ReAuthContext(
                host="chromium", project="infra/infra"
            ),
        )

    @mock.patch("gerrit_util.CreateHttpConn")
    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    def testCreateChange_ReAuth(self, mockJsonResponse, mockCreateHttpConn):
        mockJsonResponse.return_value = {"_number": 123}
        gerrit_util.CreateChange(
            "chromium", "chromium/src", branch="main", subject="Test"
        )
        mockCreateHttpConn.assert_called_once_with(
            "chromium",
            "changes/",
            reqtype="POST",
            body={
                "project": "chromium/src",
                "branch": "main",
                "subject": "Test",
            },
            reauth_context=auth.ReAuthContext(
                host="chromium", project="chromium/src"
            ),
        )

    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.CreateHttpConn")
    def testGetChangeChecks(self, mockCreateHttpConn, mockReadHttpJsonResponse):
        checks = [{"check_name": "ClangTidy", "results": []}]
        mockReadHttpJsonResponse.return_value = checks

        result = gerrit_util.GetChangeChecks("host", "change", revision="7")

        self.assertIs(checks, result)
        mockCreateHttpConn.assert_called_once_with(
            "host", "changes/change/revisions/7/list_checks"
        )
        mockReadHttpJsonResponse.assert_called_once_with(
            mockCreateHttpConn.return_value
        )

    @mock.patch("gerrit_util.ReadHttpResponse")
    @mock.patch("gerrit_util.CreateHttpConn")
    def testGetPatchDefaults(self, mockCreateHttpConn, mockReadHttpResponse):
        expected = b"From commit\nformatted patch\n"
        mockReadHttpResponse.return_value = StringIO(
            base64.b64encode(expected).decode()
        )

        result = gerrit_util.GetPatch("host", "change")

        self.assertEqual(expected, result)
        mockCreateHttpConn.assert_called_once_with(
            "host",
            "changes/change/revisions/current/patch",
            reqtype="GET",
        )
        mockReadHttpResponse.assert_called_once_with(
            mockCreateHttpConn.return_value
        )

    @mock.patch("gerrit_util.ReadHttpResponse")
    @mock.patch("gerrit_util.CreateHttpConn")
    def testGetPatchOptions(self, mockCreateHttpConn, mockReadHttpResponse):
        mockReadHttpResponse.return_value = StringIO("")

        gerrit_util.GetPatch(
            "host",
            "change",
            revision="7",
            path="src/file name.cc",
            parent=2,
            context=0,
        )

        mockCreateHttpConn.assert_called_once_with(
            "host",
            (
                "changes/change/revisions/7/patch"
                "?path=src%2Ffile+name.cc&parent=2&context=0"
            ),
            reqtype="GET",
        )

    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.CreateHttpConn")
    def testCreateGerritTag(self, mockCreateHttpConn, mockReadHttpJsonResponse):
        expected = {"ref": "refs/tags/1.0", "revision": "deadbeef"}
        mockReadHttpJsonResponse.return_value = expected

        result = gerrit_util.CreateGerritTag(
            "host", "project", "1.0", "deadbeef"
        )

        self.assertEqual(expected, result)
        mockCreateHttpConn.assert_called_once_with(
            "host",
            "projects/project/tags/1.0",
            reqtype="PUT",
            body={"revision": "deadbeef"},
        )
        mockReadHttpJsonResponse.assert_called_once_with(
            mockCreateHttpConn.return_value, accept_statuses=[201, 409]
        )

    @mock.patch("gerrit_util.ReadHttpJsonResponse")
    @mock.patch("gerrit_util.CreateHttpConn")
    def testGetGerritTag(self, mockCreateHttpConn, mockReadHttpJsonResponse):
        expected = {"ref": "refs/tags/1.0", "revision": "deadbeef"}
        mockReadHttpJsonResponse.return_value = expected

        result = gerrit_util.GetGerritTag("host", "project", "1.0")

        self.assertEqual(expected, result)
        mockCreateHttpConn.assert_called_once_with(
            "host", "projects/project/tags/1.0", reqtype="GET"
        )
        mockReadHttpJsonResponse.assert_called_once_with(
            mockCreateHttpConn.return_value, accept_statuses=[200, 404]
        )


class SSOAuthenticatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._original_timeout_secs = gerrit_util.SSOAuthenticator._timeout_secs
        return super().setUpClass()

    def setUp(self) -> None:
        gerrit_util.SSOAuthenticator._sso_info = None
        gerrit_util.SSOAuthenticator._testing_load_expired_cookies = True
        gerrit_util.SSOAuthenticator._timeout_secs = self._original_timeout_secs
        self.sso = gerrit_util.SSOAuthenticator()
        return super().setUp()

    def tearDown(self) -> None:
        gerrit_util.SSOAuthenticator._sso_info = None
        gerrit_util.SSOAuthenticator._testing_load_expired_cookies = False
        gerrit_util.SSOAuthenticator._timeout_secs = self._original_timeout_secs
        return super().tearDown()

    @property
    def _input_dir(self) -> Path:
        base = Path(__file__).absolute().with_suffix(".inputs")
        # Here _testMethodName would be a string like "testCmdAssemblyFound"
        return base / self._testMethodName

    @mock.patch(
        "gerrit_util.ssoHelper.find_cmd", return_value="/fake/git-remote-sso"
    )
    def testCmdAssemblyFound(self, _):
        self.assertEqual(
            self.sso._resolve_sso_cmd(),
            (
                "/fake/git-remote-sso",
                "-print_config",
                "sso://*.git.corp.google.com",
            ),
        )
        with mock.patch("scm.GIT.GetConfig") as p:
            p.side_effect = ["firefly@google.com"]
            self.assertTrue(self.sso.is_applicable())

    @mock.patch("gerrit_util.ssoHelper.find_cmd", return_value="")
    def testCmdAssemblyNotFound(self, _):
        self.assertEqual(self.sso._resolve_sso_cmd(), ())
        self.assertFalse(self.sso.is_applicable())

    def testParseConfigOK(self):
        test_config = {
            "somekey": "a value with = in it",
            "novalue": "",
            "http.proxy": "localhost:12345",
            "http.cookiefile": str(self._input_dir / "cookiefile.txt"),
            "include.path": str(self._input_dir / "gitconfig"),
        }
        parsed = self.sso._parse_config(test_config)
        self.assertDictEqual(
            parsed.headers,
            {
                "Authorization": "Basic REALLY_COOL_TOKEN",
            },
        )
        self.assertEqual(parsed.proxy_host, "localhost")
        self.assertEqual(parsed.proxy_port, 12345)

        c = parsed.cookies._cookies
        self.assertEqual(
            c["login.example.com"]["/"]["SSO"].value, "TUVFUE1PUlAK"
        )
        self.assertEqual(
            c[".example.com"]["/"]["__CoolProxy"].value, "QkxFRVBCTE9SUAo="
        )

    @unittest.skipUnless(RUN_SUBPROC_TESTS, "subprocess tests are flakey")
    def testLaunchHelperOK(self):
        gerrit_util.SSOAuthenticator._sso_cmd = (
            "python3",
            str(self._input_dir / "git-remote-sso.py"),
        )

        info = self.sso._get_sso_info()
        self.assertDictEqual(
            info.headers,
            {
                "Authorization": "Basic REALLY_COOL_TOKEN",
            },
        )
        self.assertEqual(info.proxy_host, "localhost")
        self.assertEqual(info.proxy_port, 12345)
        c = info.cookies._cookies
        self.assertEqual(
            c["login.example.com"]["/"]["SSO"].value, "TUVFUE1PUlAK"
        )
        self.assertEqual(
            c[".example.com"]["/"]["__CoolProxy"].value, "QkxFRVBCTE9SUAo="
        )

    @unittest.skipUnless(RUN_SUBPROC_TESTS, "subprocess tests are flakey")
    def testLaunchHelperFailQuick(self):
        gerrit_util.SSOAuthenticator._sso_cmd = (
            "python3",
            str(self._input_dir / "git-remote-sso.py"),
        )

        with self.assertRaisesRegex(SystemExit, "SSO Failure Message!!!"):
            self.sso._get_sso_info()

    @unittest.skipUnless(RUN_SUBPROC_TESTS, "subprocess tests are flakey")
    def testLaunchHelperFailSlow(self):
        gerrit_util.SSOAuthenticator._timeout_secs = 0.2
        gerrit_util.SSOAuthenticator._sso_cmd = (
            "python3",
            str(self._input_dir / "git-remote-sso.py"),
        )

        with self.assertRaises(subprocess.TimeoutExpired):
            self.sso._get_sso_info()

    @mock.patch("gerrit_util.SSOAuthenticator.authenticate")
    def testAttemptAuthenticateWithReAuth(self, mockAuthenticate):
        conn = makeConn("chromium")
        reauth_context = auth.ReAuthContext(
            host="chromium", project="infra/infra"
        )
        out = self.sso.attempt_authenticate_with_reauth(conn, reauth_context)
        mockAuthenticate.assert_called()
        self.assertTrue(out)

    @staticmethod
    def _make_cookie(
        name: str, value: str, domain: str
    ) -> http.cookiejar.Cookie:
        return http.cookiejar.Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith("."),
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
        )

    @staticmethod
    def _make_http_response(
        url: str,
        status: int,
        headers: dict[str, str],
        body: bytes = b"",
    ) -> urllib.response.addinfourl:
        msg = email.message.Message()
        for k, v in headers.items():
            msg[k] = v
        resp = urllib.response.addinfourl(io.BytesIO(body), msg, url, status)
        resp.msg = resp.reason = "OK" if status == 200 else "Found"
        return resp

    def testAuthenticateSetsCookieJarAndUserAgent(self):
        jar = http.cookiejar.CookieJar()
        jar.set_cookie(self._make_cookie("sid", "val", ".google.com"))
        sso_info = gerrit_util.SSOAuthenticator.SSOInfo(
            proxy_host="127.0.0.1",
            proxy_port=8080,
            headers={"X-Custom": "1"},
            cookies=jar,
        )
        conn = gerrit_util.HttpConn(
            req_host="chromium-review.googlesource.com",
            req_uri="https://chromium-review.googlesource.com/a/changes/",
            req_method="GET",
            req_headers={},
            req_body=None,
        )
        with mock.patch.object(
            self.sso, "_get_sso_info", return_value=sso_info
        ):
            self.sso.authenticate(conn)

        self.assertEqual(conn.proxy, "http://127.0.0.1:8080")
        self.assertIs(conn.cookie_jar, jar)
        self.assertEqual(conn.req_headers["User-Agent"], "depot_tools")
        self.assertEqual(conn.req_headers["X-Custom"], "1")
        self.assertEqual(
            conn.req_uri,
            "http://chromium-review.git.corp.google.com/a/changes/",
        )

    def testRedirectSendsAndPersistsCookies(self):
        jar = http.cookiejar.CookieJar()
        jar.set_cookie(self._make_cookie("session", "old", ".example.com"))
        jar.set_cookie(
            self._make_cookie("login_token", "secret", "login.example.com")
        )
        sso_info = gerrit_util.SSOAuthenticator.SSOInfo(
            proxy_host="",
            proxy_port=0,
            headers={},
            cookies=jar,
        )
        conn = gerrit_util.HttpConn(
            req_host="review.example.com",
            req_uri="https://review.example.com/a/changes/",
            req_method="GET",
            req_headers={},
            req_body=None,
        )
        with mock.patch.object(
            self.sso, "_get_sso_info", return_value=sso_info
        ):
            self.sso.authenticate(conn)
        conn.proxy = None

        seen_requests: list[tuple[str, Optional[str], Optional[str]]] = []

        def fake_http_open(_handler, req: urllib.request.Request):
            seen_requests.append(
                (
                    req.full_url,
                    req.get_header("Cookie"),
                    req.get_header("User-agent"),
                )
            )
            step = len(seen_requests)
            if step == 1:
                return self._make_http_response(
                    req.full_url,
                    302,
                    {"Location": "http://login.example.com/auth"},
                )
            if step == 2:
                return self._make_http_response(
                    req.full_url,
                    302,
                    {
                        "Location": "http://review.example.com/a/changes/",
                        "Set-Cookie": (
                            "session=renewed; Domain=.example.com; Path=/"
                        ),
                    },
                )
            return self._make_http_response(req.full_url, 200, {}, b")]}'\n{}")

        with mock.patch.object(
            urllib.request.HTTPHandler, "http_open", autospec=True
        ) as mock_http_open:
            mock_http_open.side_effect = fake_http_open
            resp, body = conn.request()

        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b")]}'\n{}")
        self.assertEqual(
            seen_requests,
            [
                (
                    "http://review.example.com/a/changes/",
                    "session=old",
                    "depot_tools",
                ),
                (
                    "http://login.example.com/auth",
                    "session=old; login_token=secret",
                    "depot_tools",
                ),
                (
                    "http://review.example.com/a/changes/",
                    "session=renewed",
                    "depot_tools",
                ),
            ],
        )
        cookies_by_domain = {(c.domain, c.name): c.value for c in jar}
        self.assertEqual(
            cookies_by_domain[(".example.com", "session")], "renewed"
        )

    @mock.patch(
        "gerrit_util.ssoHelper.find_cmd", return_value="/fake/git-remote-sso"
    )
    @mock.patch("subprocess2.check_output")
    def testGetSsoInfoRefreshesCookies(
        self, mock_check_output: mock.MagicMock, _: mock.MagicMock
    ) -> None:
        empty_jar = http.cookiejar.CookieJar()
        populated_jar = http.cookiejar.CookieJar()
        populated_jar.set_cookie(self._make_cookie("sid", "val", ".google.com"))
        info_empty = gerrit_util.SSOAuthenticator.SSOInfo(
            proxy_host="localhost",
            proxy_port=1234,
            headers={"Authorization": "Basic token"},
            cookies=empty_jar,
        )
        info_populated = gerrit_util.SSOAuthenticator.SSOInfo(
            proxy_host="localhost",
            proxy_port=1234,
            headers={"Authorization": "Basic token"},
            cookies=populated_jar,
        )
        with mock.patch.object(
            gerrit_util.SSOAuthenticator,
            "_launch_sso_helper",
            side_effect=[info_empty, info_populated],
        ):
            info = self.sso._get_sso_info()
        self.assertIs(info, info_populated)
        mock_check_output.assert_called_once_with(
            [
                "/fake/git-remote-sso",
                "sso://chromium/All-Projects",
                "sso://chromium/All-Projects",
            ],
            stdin=b"list\n\n",
        )


class SSOHelperTest(unittest.TestCase):
    def _make_temp_dir(self) -> str:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return temp_dir.name

    def _chdir(self, path: str) -> None:
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(path)

    @staticmethod
    def _plant(path: str, mode: int = 0o755) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w"):
            pass
        os.chmod(path, mode)
        return path

    @staticmethod
    @contextlib.contextmanager
    def _simulate(
        platform: str, path_entries: list[str], pathext: str = ".EXE;.BAT"
    ) -> Iterator[None]:
        # Use the simulated platform's PATH separator, so that e.g. the drive
        # colon in "C:\" is not treated as a separator on POSIX hosts.
        pathsep = ";" if platform == "win32" else ":"
        with (
            mock.patch.object(sys, "platform", platform),
            mock.patch.object(os, "pathsep", pathsep),
            mock.patch.dict(
                os.environ,
                {"PATH": pathsep.join(path_entries), "PATHEXT": pathext},
            ),
        ):
            yield

    def testFindCmd(self) -> None:
        # Both a found helper and a missing one ("") are cached.
        for found, expected in (
            ("/fake/git-remote-sso", "/fake/git-remote-sso"),
            (None, ""),
        ):
            with (
                self.subTest(found=found),
                mock.patch(
                    "gclient_utils.FindExecutableInAbsolutePath",
                    return_value=found,
                ) as find,
            ):
                sso = gerrit_util.SSOHelper()
                self.assertEqual(sso.find_cmd(), expected)
                self.assertEqual(sso.find_cmd(), expected)
                find.assert_called_once_with("git-remote-sso")

    def testFindCmdOnWindowsIgnoresCwdAndRelativeEntries(self) -> None:
        cwd = self._make_temp_dir()
        self._chdir(cwd)
        relative_entries = ["", ".", "bin", os.path.join(".", "sub")]
        # Plant a helper in the current directory, which `shutil.which()`
        # searches first on Windows, and wherever each relative entry would
        # resolve to if it were searched.
        for entry in relative_entries:
            self._plant(os.path.join(cwd, entry, "git-remote-sso.BAT"))
        empty_dir = os.path.join(cwd, "empty")
        os.mkdir(empty_dir)
        abs_dir = os.path.join(cwd, "abs")
        abs_helper = self._plant(os.path.join(abs_dir, "git-remote-sso.EXE"))

        for path_entries, expected in (
            ([], ""),
            ([empty_dir], ""),
            (relative_entries, ""),
            (relative_entries + [empty_dir], ""),
            (relative_entries + [abs_dir], abs_helper),
        ):
            with (
                self.subTest(path_entries=path_entries),
                self._simulate("win32", path_entries),
            ):
                self.assertEqual(gerrit_util.SSOHelper().find_cmd(), expected)

    def testFindCmdOnWindowsRecognizesAbsoluteEntries(self) -> None:
        relative_entries = ["", ".", "bin", r".\sub", r"\bin", "C:", "C:bin"]
        cases = [(relative_entries, "")]
        for abs_dir in (r"C:\Tools", r"\\server\share\tools"):
            cases.append(
                (
                    relative_entries + [abs_dir],
                    os.path.join(abs_dir, "git-remote-sso.EXE"),
                )
            )
        for path_entries, expected in cases:
            with (
                self.subTest(path_entries=path_entries),
                self._simulate("win32", path_entries),
                # Pretend every candidate exists and is executable: a searched
                # relative entry would therefore be returned ahead of the
                # absolute one.
                mock.patch.object(os.path, "isfile", return_value=True),
                mock.patch.object(os, "access", return_value=True),
            ):
                self.assertEqual(gerrit_util.SSOHelper().find_cmd(), expected)

    def testFindCmdOnWindowsTriesPathext(self) -> None:
        temp_dir = self._make_temp_dir()
        first = os.path.join(temp_dir, "first")
        second = os.path.join(temp_dir, "second")
        self._plant(os.path.join(first, "git-remote-sso"))
        first_bat = self._plant(os.path.join(first, "git-remote-sso.BAT"))
        second_cmd = self._plant(os.path.join(second, "git-remote-sso.CMD"))
        second_exe = self._plant(os.path.join(second, "git-remote-sso.EXE"))

        for pathext, path_entries, expected in (
            # Each PATH entry is searched for every extension before the next.
            (".EXE;.BAT", [first, second], first_bat),
            # Within a PATH entry, extensions are tried in PATHEXT order.
            (".CMD;.EXE", [second], second_cmd),
            (".EXE;.CMD", [second], second_exe),
            # Extensions missing from PATHEXT are not tried.
            (".COM;.EXE", [first], ""),
            # An empty PATHEXT falls back to the `shutil.which()` default.
            ("", [first], first_bat),
        ):
            with (
                self.subTest(pathext=pathext, path_entries=path_entries),
                self._simulate("win32", path_entries, pathext),
            ):
                self.assertEqual(gerrit_util.SSOHelper().find_cmd(), expected)

    @unittest.skipIf(sys.platform == "win32", "needs POSIX file permissions")
    def testFindCmdOnPosix(self) -> None:
        cwd = self._make_temp_dir()
        self._chdir(cwd)
        # Executable helpers that are only reachable through the current
        # directory or a relative entry.
        self._plant(os.path.join(cwd, "git-remote-sso"))
        self._plant(os.path.join(cwd, "bin", "git-remote-sso"))
        non_exec_dir = os.path.join(cwd, "non_exec")
        self._plant(os.path.join(non_exec_dir, "git-remote-sso"), mode=0o644)
        dir_dir = os.path.join(cwd, "dir")
        os.makedirs(os.path.join(dir_dir, "git-remote-sso"))
        pathext_dir = os.path.join(cwd, "pathext")
        self._plant(os.path.join(pathext_dir, "git-remote-sso.EXE"))
        abs_dir = os.path.join(cwd, "abs")
        abs_helper = self._plant(os.path.join(abs_dir, "git-remote-sso"))

        unusable_abs_entries = [non_exec_dir, dir_dir, pathext_dir]
        for path_entries, expected in (
            # Like `shutil.which()`, only executable regular files match, and
            # PATHEXT is ignored.
            (unusable_abs_entries, ""),
            (unusable_abs_entries + [abs_dir], abs_helper),
            # The current directory and relative entries are never searched.
            (["", ".", "bin"], ""),
            (["", ".", "bin", abs_dir], abs_helper),
        ):
            with (
                self.subTest(path_entries=path_entries),
                self._simulate("linux", path_entries),
            ):
                self.assertEqual(gerrit_util.SSOHelper().find_cmd(), expected)


class ShouldUseSSOTest(unittest.TestCase):
    def setUp(self) -> None:
        self.newauth = mock.patch("newauth.Enabled", return_value=True)
        self.newauth.start()
        self.cwd = mock.patch("os.getcwd", return_value="/fake/cwd")
        self.cwd.start()
        self.sso = mock.patch(
            "gerrit_util.ssoHelper.find_cmd",
            return_value="/fake/git-remote-sso",
        )
        self.sso.start()
        scm_mock.GIT(self)
        self.addCleanup(mock.patch.stopall)

        gerrit_util.CheckShouldUseSSO.cache_clear()
        return super().setUp()

    def tearDown(self) -> None:
        super().tearDown()
        self.sso.stop()
        self.newauth.stop()

    @mock.patch("newauth.Enabled", return_value=False)
    def testDisabled(self, _):
        self.assertFalse(
            gerrit_util.ShouldUseSSO(
                "fake-host.googlesource.com", "firefly@google.com"
            )
        )

    @mock.patch("gerrit_util.ssoHelper.find_cmd", return_value="")
    def testMissingCommand(self, _):
        self.assertFalse(
            gerrit_util.ShouldUseSSO(
                "fake-host.googlesource.com", "firefly@google.com"
            )
        )

    def testBadHost(self):
        for bad_host in (
            "fake-host.coreboot.org",
            "googlesource.com",
            ".googlesource.com",
            "evil.com/.googlesource.com",
            "foo.googlesource.com@evil.com",
            "fake-host.googlesource.com:443",
        ):
            self.assertFalse(
                gerrit_util.ShouldUseSSO(bad_host, "firefly@google.com"),
                msg=bad_host,
            )

    def testEmptyEmail(self):
        self.assertTrue(
            gerrit_util.ShouldUseSSO("fake-host.googlesource.com", "")
        )

    def testGoogleEmail(self):
        self.assertTrue(
            gerrit_util.ShouldUseSSO(
                "fake-host.googlesource.com", "firefly@google.com"
            )
        )

    def testGmail(self):
        self.assertFalse(
            gerrit_util.ShouldUseSSO(
                "fake-host.googlesource.com", "firefly@gmail.com"
            )
        )

    @mock.patch(
        "gerrit_util.GetAccountEmails",
        return_value=[{"email": "firefly@chromium.org"}],
    )
    def testLinkedChromium(self, email):
        self.assertTrue(
            gerrit_util.ShouldUseSSO(
                "fake-host.googlesource.com", "firefly@chromium.org"
            )
        )
        email.assert_called_with(
            "fake-host.googlesource.com", "self", authenticator=mock.ANY
        )

    @mock.patch(
        "gerrit_util.GetAccountEmails",
        return_value=[{"email": "firefly@google.com"}],
    )
    def testUnlinkedChromium(self, email):
        self.assertFalse(
            gerrit_util.ShouldUseSSO(
                "fake-host.googlesource.com", "firefly@chromium.org"
            )
        )
        email.assert_called_with(
            "fake-host.googlesource.com", "self", authenticator=mock.ANY
        )


class EnsureAccountExistsTest(unittest.TestCase):
    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch(
        "scm.GIT.GetConfigList", return_value=["example.com:user@example.com"]
    )
    @mock.patch("gerrit_util.GetAccountDetails")
    def testEnsureAccountExists_CachedWithEmail(
        self, mock_get_account, mock_get_config_list, mock_get_config
    ):
        gerrit_util.EnsureAccountExists("example.com")
        mock_get_config_list.assert_called_once_with(
            mock.ANY, "depot-tools.hosthasaccount"
        )
        mock_get_account.assert_not_called()

    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch("scm.GIT.GetConfigList", return_value=["example.com"])
    @mock.patch("gerrit_util.GetAccountDetails")
    def testEnsureAccountExists_CachedLegacyHostOnly(
        self, mock_get_account, mock_get_config_list, mock_get_config
    ):
        gerrit_util.EnsureAccountExists("example.com")
        mock_get_config_list.assert_called_once_with(
            mock.ANY, "depot-tools.hosthasaccount"
        )
        mock_get_account.assert_not_called()

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", return_value="new_user@example.com")
    @mock.patch(
        "scm.GIT.GetConfigList",
        return_value=["example.com:old_user@example.com"],
    )
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        return_value={"email": "new_user@example.com"},
    )
    def testEnsureAccountExists_AccountSwitchMiss(
        self,
        mock_get_account,
        mock_get_config_list,
        mock_get_config,
        mock_set_config,
    ):
        gerrit_util.EnsureAccountExists("example.com")
        mock_get_account.assert_called_once_with(
            "example.com", authenticator=None
        )
        mock_set_config.assert_called_once_with(
            mock.ANY,
            "depot-tools.hosthasaccount",
            "example.com:new_user@example.com",
            append=True,
        )

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch("scm.GIT.GetConfigList", return_value=[])
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        return_value={"email": "user@example.com"},
    )
    def testEnsureAccountExists_UncachedSuccessWithEmail(
        self,
        mock_get_account,
        mock_get_config_list,
        mock_get_config,
        mock_set_config,
    ):
        gerrit_util.EnsureAccountExists("example.com")
        mock_get_account.assert_called_once_with(
            "example.com", authenticator=None
        )
        mock_set_config.assert_called_once_with(
            mock.ANY,
            "depot-tools.hosthasaccount",
            "example.com:user@example.com",
            append=True,
        )

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", return_value=None)
    @mock.patch("scm.GIT.GetConfigList", return_value=[])
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        return_value={"email": "user@example.com"},
    )
    def testEnsureAccountExists_UncachedSuccessNoLocalEmail(
        self,
        mock_get_account,
        mock_get_config_list,
        mock_get_config,
        mock_set_config,
    ):
        gerrit_util.EnsureAccountExists("example.com")
        mock_get_account.assert_called_once_with(
            "example.com", authenticator=None
        )
        mock_set_config.assert_called_once_with(
            mock.ANY,
            "depot-tools.hosthasaccount",
            "example.com",
            append=True,
        )

    @mock.patch("scm.GIT.GetConfig", return_value=None)
    @mock.patch("scm.GIT.GetConfigList", return_value=[])
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        side_effect=gerrit_util.GerritError(400, "Bad Request"),
    )
    def testEnsureAccountExists_NotFound400(
        self, mock_get_account, mock_get_config_list, mock_get_config
    ):
        with self.assertRaises(gerrit_util.GerritError) as ctx:
            gerrit_util.EnsureAccountExists("example.com")
        self.assertEqual(ctx.exception.http_status, 400)
        self.assertIn(
            "Account does not exist on Gerrit host 'example.com'",
            str(ctx.exception),
        )

    @mock.patch("scm.GIT.GetConfig", return_value=None)
    @mock.patch("scm.GIT.GetConfigList", return_value=[])
    @mock.patch("gerrit_util.GetAccountDetails", return_value={})
    def testEnsureAccountExists_NotFound404(
        self, mock_get_account, mock_get_config_list, mock_get_config
    ):
        with self.assertRaises(gerrit_util.GerritError) as ctx:
            gerrit_util.EnsureAccountExists("example.com")
        self.assertEqual(ctx.exception.http_status, 404)
        self.assertIn(
            "Account not found on Gerrit host 'example.com'", str(ctx.exception)
        )

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch(
        "scm.GIT.GetConfigList",
        return_value=["example.com:user@example.com", "other.com"],
    )
    @mock.patch("gerrit_util.GetAccountDetails", return_value=None)
    def testEnsureAccountExists_NotFound404InvalidatesCache(
        self,
        mock_get_account,
        mock_get_config_list,
        mock_get_config,
        mock_set_config,
    ):
        with self.assertRaises(gerrit_util.GerritError) as ctx:
            gerrit_util.EnsureAccountExists("example.com", force=True)
        self.assertEqual(ctx.exception.http_status, 404)
        self.assertIn(
            "Account not found on Gerrit host 'example.com'", str(ctx.exception)
        )
        mock_set_config.assert_has_calls(
            [
                mock.call(
                    mock.ANY,
                    "depot-tools.hosthasaccount",
                    None,
                    modify_all=True,
                ),
                mock.call(
                    mock.ANY,
                    "depot-tools.hosthasaccount",
                    "other.com",
                    append=True,
                ),
            ]
        )

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch(
        "scm.GIT.GetConfigList", return_value=["example.com:user@example.com"]
    )
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        return_value={"email": "user@example.com"},
    )
    def testEnsureAccountExists_ForceBypassesCache(
        self,
        mock_get_account,
        mock_get_config_list,
        mock_get_config,
        mock_set_config,
    ):
        gerrit_util.EnsureAccountExists("example.com", force=True)
        mock_get_account.assert_called_once_with(
            "example.com", authenticator=None
        )

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch(
        "scm.GIT.GetConfigList",
        return_value=["example.com:user@example.com", "other.com"],
    )
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        side_effect=gerrit_util.GerritError(400, "Bad Request"),
    )
    def testEnsureAccountExists_InvalidatesCacheOnError(
        self,
        mock_get_account,
        mock_get_config_list,
        mock_get_config,
        mock_set_config,
    ):
        with self.assertRaises(gerrit_util.GerritError):
            gerrit_util.EnsureAccountExists("example.com", force=True)
        mock_set_config.assert_has_calls(
            [
                mock.call(
                    mock.ANY,
                    "depot-tools.hosthasaccount",
                    None,
                    modify_all=True,
                ),
                mock.call(
                    mock.ANY,
                    "depot-tools.hosthasaccount",
                    "other.com",
                    append=True,
                ),
            ]
        )

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch(
        "scm.GIT.GetConfigList",
        return_value=[
            "example.com:user@example.com",
            "example.com:user@example.com.uk",
            "other.com",
        ],
    )
    def testInvalidateAccountCache(
        self, mock_get_config_list, mock_get_config, mock_set_config
    ):
        gerrit_util.InvalidateAccountCache("example.com")
        mock_set_config.assert_has_calls(
            [
                mock.call(
                    mock.ANY,
                    "depot-tools.hosthasaccount",
                    None,
                    modify_all=True,
                ),
                mock.call(
                    mock.ANY,
                    "depot-tools.hosthasaccount",
                    "example.com:user@example.com.uk",
                    append=True,
                ),
                mock.call(
                    mock.ANY,
                    "depot-tools.hosthasaccount",
                    "other.com",
                    append=True,
                ),
            ]
        )

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", return_value="User@Example.COM")
    @mock.patch(
        "scm.GIT.GetConfigList",
        return_value=["example.com:user@example.com"],
    )
    def testInvalidateAccountCache_CaseInsensitive(
        self, mock_get_config_list, mock_get_config, mock_set_config
    ):
        gerrit_util.InvalidateAccountCache("HTTPS://EXAMPLE.COM/")
        mock_set_config.assert_called_once_with(
            mock.ANY, "depot-tools.hosthasaccount", None, modify_all=True
        )

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch(
        "scm.GIT.GetConfigList",
        return_value=[
            "host1.com:user1@example.com",
            "host2.com:user2@example.com",
            "example.com:user@example.com",
        ],
    )
    def testInvalidateAccountCache_MultipleValuesUsesModifyAll(
        self, mock_get_config_list, mock_get_config, mock_set_config
    ):
        gerrit_util.InvalidateAccountCache("example.com")
        mock_set_config.assert_has_calls(
            [
                mock.call(
                    mock.ANY,
                    "depot-tools.hosthasaccount",
                    None,
                    modify_all=True,
                ),
                mock.call(
                    mock.ANY,
                    "depot-tools.hosthasaccount",
                    "host1.com:user1@example.com",
                    append=True,
                ),
                mock.call(
                    mock.ANY,
                    "depot-tools.hosthasaccount",
                    "host2.com:user2@example.com",
                    append=True,
                ),
            ]
        )

    @mock.patch("scm.GIT.GetConfig", return_value="User@Example.COM")
    @mock.patch(
        "scm.GIT.GetConfigList",
        return_value=["example.com:user@example.com"],
    )
    @mock.patch("gerrit_util.GetAccountDetails")
    def testEnsureAccountExists_CaseInsensitiveHit(
        self, mock_get_account, mock_get_config_list, mock_get_config
    ):
        gerrit_util.EnsureAccountExists("HTTPS://EXAMPLE.COM/")
        mock_get_account.assert_not_called()

    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch(
        "scm.GIT.GetConfigList",
        return_value=["example.com:8080:user@example.com"],
    )
    @mock.patch("gerrit_util.GetAccountDetails")
    def testEnsureAccountExists_BareHostWithPort(
        self, mock_get_account, mock_get_config_list, mock_get_config
    ):
        gerrit_util.EnsureAccountExists("example.com:8080")
        mock_get_account.assert_not_called()

    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch(
        "scm.GIT.GetConfigList",
        return_value=["example.com:user@example.com"],
    )
    @mock.patch("gerrit_util.GetAccountDetails")
    def testEnsureAccountExists_ExplicitCwdPassed(
        self, mock_get_account, mock_get_config_list, mock_get_config
    ):
        gerrit_util.EnsureAccountExists("example.com", cwd="/custom/repo/path")
        mock_get_config.assert_called_once_with(
            "/custom/repo/path", "user.email"
        )
        mock_get_config_list.assert_called_once_with(
            "/custom/repo/path", "depot-tools.hosthasaccount"
        )
        mock_get_account.assert_not_called()

    @mock.patch(
        "scm.GIT.SetConfig",
        side_effect=subprocess2.CalledProcessError(
            1, ["git", "config"], None, None, None
        ),
    )
    @mock.patch("scm.GIT.GetConfig", return_value=None)
    @mock.patch("scm.GIT.GetConfigList", return_value=[])
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        return_value={"_account_id": 12345},
    )
    def testEnsureAccountExists_SetConfigFailureGraceful(
        self,
        mock_get_account,
        mock_get_config_list,
        mock_get_config,
        mock_set_config,
    ):
        gerrit_util.EnsureAccountExists("example.com")
        mock_get_account.assert_called_once()

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfigList", return_value=[])
    def testInvalidateAccountCache_EmptyConfig(
        self, mock_get_config_list, mock_set_config
    ):
        gerrit_util.InvalidateAccountCache("example.com")
        mock_set_config.assert_not_called()

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", side_effect=Exception("git error"))
    @mock.patch("scm.GIT.GetConfigList", return_value=["other.com"])
    def testInvalidateAccountCache_NoMatchingHost(
        self, mock_get_config_list, mock_get_config, mock_set_config
    ):
        gerrit_util.InvalidateAccountCache("example.com")
        mock_set_config.assert_not_called()

    @mock.patch(
        "scm.GIT.GetConfigList", side_effect=Exception("fatal git error")
    )
    def testInvalidateAccountCache_ExceptionSwallowed(
        self, mock_get_config_list
    ):
        gerrit_util.InvalidateAccountCache("example.com")

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", side_effect=Exception("config error"))
    @mock.patch("scm.GIT.GetConfigList", side_effect=Exception("config error"))
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        return_value={"email": "user@example.com"},
    )
    def testEnsureAccountExists_GetConfigExceptionsSwallowed(
        self,
        mock_get_account,
        mock_get_config_list,
        mock_get_config,
        mock_set_config,
    ):
        gerrit_util.EnsureAccountExists("example.com")
        mock_get_account.assert_called_once()

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch(
        "scm.GIT.GetConfigList", return_value=["example.com:user@example.com"]
    )
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        side_effect=gerrit_util.GerritError(500, "Internal Server Error"),
    )
    def testEnsureAccountExists_Non400GerritErrorDoesNotInvalidate(
        self,
        mock_get_account,
        mock_get_config_list,
        mock_get_config,
        mock_set_config,
    ):
        with self.assertRaises(gerrit_util.GerritError) as ctx:
            gerrit_util.EnsureAccountExists("example.com", force=True)
        self.assertEqual(ctx.exception.http_status, 500)
        mock_set_config.assert_not_called()

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch(
        "scm.GIT.GetConfigList", return_value=["example.com:user@example.com"]
    )
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        side_effect=auth.GitLoginRequiredError(),
    )
    def testEnsureAccountExists_AuthExceptionDoesNotInvalidate(
        self,
        mock_get_account,
        mock_get_config_list,
        mock_get_config,
        mock_set_config,
    ):
        with self.assertRaises(auth.GitLoginRequiredError):
            gerrit_util.EnsureAccountExists("example.com", force=True)
        mock_set_config.assert_not_called()

    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch(
        "scm.GIT.GetConfigList", return_value=["example.com:user@example.com"]
    )
    @mock.patch("gerrit_util.GetAccountDetails")
    def testAsyncEnsureAccountExists_CachedFastReturn(
        self, mock_get_account, mock_get_config_list, mock_get_config
    ):
        join_fn = gerrit_util.AsyncEnsureAccountExists("example.com")
        join_fn()
        mock_get_account.assert_not_called()

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch("scm.GIT.GetConfigList", return_value=[])
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        return_value={"email": "user@example.com"},
    )
    def testAsyncEnsureAccountExists_UncachedSuccess(
        self,
        mock_get_account,
        mock_get_config_list,
        mock_get_config,
        mock_set_config,
    ):
        join_fn = gerrit_util.AsyncEnsureAccountExists("example.com")
        join_fn()
        mock_get_account.assert_called_once()

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch("scm.GIT.GetConfigList", return_value=[])
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        return_value={"email": "user@example.com"},
    )
    def testAsyncEnsureAccountExists_MultipleJoinCallsIdempotent(
        self,
        mock_get_account,
        mock_get_config_list,
        mock_get_config,
        mock_set_config,
    ):
        join_fn = gerrit_util.AsyncEnsureAccountExists("example.com")
        join_fn()
        join_fn()
        mock_get_account.assert_called_once()

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch("scm.GIT.GetConfigList", return_value=[])
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        return_value={"email": "user@example.com"},
    )
    def testAsyncEnsureAccountExists_ConcurrentJoinThreadSafe(
        self,
        mock_get_account,
        mock_get_config_list,
        mock_get_config,
        mock_set_config,
    ):
        join_fn = gerrit_util.AsyncEnsureAccountExists("example.com")
        barrier = threading.Barrier(4)
        errors = []

        def _caller():
            try:
                barrier.wait()
                join_fn()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_caller) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        mock_get_account.assert_called_once()

    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch("scm.GIT.GetConfigList", return_value=[])
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        side_effect=gerrit_util.GerritError(400, "Bad Request"),
    )
    def testAsyncEnsureAccountExists_RaisesExceptionOnJoin(
        self, mock_get_account, mock_get_config_list, mock_get_config
    ):
        join_fn = gerrit_util.AsyncEnsureAccountExists("example.com")
        with self.assertRaises(gerrit_util.GerritError):
            join_fn()

    @mock.patch("scm.GIT.GetConfig", return_value="user@example.com")
    @mock.patch("scm.GIT.GetConfigList", return_value=[])
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        side_effect=SystemExit(1),
    )
    def testAsyncEnsureAccountExists_SystemExitPropagates(
        self, mock_get_account, mock_get_config_list, mock_get_config
    ):
        join_fn = gerrit_util.AsyncEnsureAccountExists("example.com")
        with self.assertRaises(SystemExit) as ctx:
            join_fn()
        self.assertEqual(ctx.exception.code, 1)

    @mock.patch("scm.GIT.SetConfig")
    @mock.patch("scm.GIT.GetConfig", side_effect=Exception("config error"))
    @mock.patch("scm.GIT.GetConfigList", side_effect=Exception("config error"))
    @mock.patch(
        "gerrit_util.GetAccountDetails",
        return_value={"email": "user@example.com"},
    )
    def testAsyncEnsureAccountExists_GetConfigExceptionsSwallowed(
        self,
        mock_get_account,
        mock_get_config_list,
        mock_get_config,
        mock_set_config,
    ):
        join_fn = gerrit_util.AsyncEnsureAccountExists("example.com")
        join_fn()
        mock_get_account.assert_called_once()


class GerritFlowsTest(unittest.TestCase):
    def testComputeScheduleTime_DefaultJitter(self):
        # 123456 % 240 = 96 mins -> 19:00 + 1h36m = 20:36
        self.assertEqual(
            "20:36",
            gerrit_util.ComputeScheduleTime(123456),
        )
        self.assertEqual(
            "19:00",
            gerrit_util.ComputeScheduleTime(0),
        )
        self.assertEqual(
            "19:00",
            gerrit_util.ComputeScheduleTime(None),
        )
        # Boundary: issue 239 -> 22:59
        self.assertEqual(
            "22:59",
            gerrit_util.ComputeScheduleTime(239),
        )
        # Boundary: issue 240 -> 19:00
        self.assertEqual(
            "19:00",
            gerrit_util.ComputeScheduleTime(240),
        )

    def testComputeScheduleTime_CustomTime(self):
        self.assertEqual(
            "20:30",
            gerrit_util.ComputeScheduleTime(123456, custom_time="20:30"),
        )
        self.assertEqual(
            "00:00",
            gerrit_util.ComputeScheduleTime(123456, custom_time="00:00"),
        )
        self.assertEqual(
            "23:59",
            gerrit_util.ComputeScheduleTime(123456, custom_time="23:59"),
        )

    def testComputeScheduleTime_InvalidCustomTime(self):
        invalid_times = ["", "   ", "8pm", "25:00", "today at 20:30", 1234]
        for t in invalid_times:
            with self.assertRaises(ValueError):
                gerrit_util.ComputeScheduleTime(123456, custom_time=t)

    @mock.patch("gerrit_util.IsFlowsEnabled", return_value=True)
    @mock.patch("gerrit_util.CallGerritApi")
    def testCreateScheduledSubmitFlow_DefaultJitter(
        self, mockCallGerritApi, _mockIsFlowsEnabled
    ):
        mockCallGerritApi.return_value = {"uuid": "test-uuid"}
        result = gerrit_util.CreateScheduledSubmitFlow("example.com", 123456)
        self.assertEqual(result, {"uuid": "test-uuid"})
        mockCallGerritApi.assert_called_once_with(
            "example.com",
            "changes/123456/flows",
            reqtype="POST",
            body={
                "stage_expressions": [
                    {
                        "condition": (
                            "https://example.com/c/123456 is is:submittable, "
                            "wait until today at 20:36 in America/Los_Angeles"
                        ),
                        "action": {
                            "name": "vote",
                            "parameters": ["Commit-Queue+2"],
                        },
                    }
                ]
            },
            accept_statuses=(200, 201),
        )

    @mock.patch("gerrit_util.IsFlowsEnabled", return_value=True)
    @mock.patch("gerrit_util.CallGerritApi")
    def testCreateScheduledSubmitFlow_CustomTime(
        self, mockCallGerritApi, _mockIsFlowsEnabled
    ):
        mockCallGerritApi.return_value = {"uuid": "test-uuid"}
        result = gerrit_util.CreateScheduledSubmitFlow(
            "example.com", 123456, custom_time="20:30"
        )
        self.assertEqual(result, {"uuid": "test-uuid"})
        mockCallGerritApi.assert_called_once_with(
            "example.com",
            "changes/123456/flows",
            reqtype="POST",
            body={
                "stage_expressions": [
                    {
                        "condition": (
                            "https://example.com/c/123456 is is:submittable, "
                            "wait until today at 20:30 in America/Los_Angeles"
                        ),
                        "action": {
                            "name": "vote",
                            "parameters": ["Commit-Queue+2"],
                        },
                    }
                ]
            },
            accept_statuses=(200, 201),
        )

    @mock.patch("builtins.print")
    @mock.patch("gerrit_util.CallGerritApi", return_value={"enabled": False})
    def testCreateScheduledSubmitFlow_FlowsDisabledForNonGoogler(
        self, mockCallGerritApi, mockPrint
    ):
        result = gerrit_util.CreateScheduledSubmitFlow("example.com", 123456)
        self.assertIsNone(result)
        mockCallGerritApi.assert_called_once_with(
            "example.com",
            "changes/123456/is-flows-enabled",
            reqtype="GET",
            accept_statuses=(200,),
        )
        mockPrint.assert_called_once_with(
            "Scheduled submits are not currently supported for external "
            "Chromium contributors."
        )

    @mock.patch("gerrit_util.IsFlowsEnabled", return_value=True)
    @mock.patch("gerrit_util.CallGerritApi")
    def testCreateScheduledSubmitFlow_ApiFailure(
        self, mockCallGerritApi, _mockIsFlowsEnabled
    ):
        mockCallGerritApi.side_effect = gerrit_util.GerritError(
            500, "Internal Server Error"
        )
        with self.assertRaises(gerrit_util.GerritError):
            gerrit_util.CreateScheduledSubmitFlow("example.com", 123456)


class WithGitConfigDirTest(unittest.TestCase):
    def testNestingAndRestoreOnException(self):
        dir1 = os.path.abspath("/repo/one")
        dir2 = os.path.abspath("/repo/two")
        self.assertEqual(gerrit_util._GetGitConfigDir(), os.getcwd())

        with gerrit_util.WithGitConfigDir(dir1):
            self.assertEqual(gerrit_util._GetGitConfigDir(), dir1)
            with self.assertRaises(RuntimeError):
                with gerrit_util.WithGitConfigDir(dir2):
                    self.assertEqual(gerrit_util._GetGitConfigDir(), dir2)
                    raise RuntimeError("boom")
            self.assertEqual(gerrit_util._GetGitConfigDir(), dir1)

        self.assertEqual(gerrit_util._GetGitConfigDir(), os.getcwd())
        self.assertIsNone(gerrit_util._GIT_CONFIG_DIR)

    @mock.patch("scm.GIT.GetConfig")
    def testAuthenticatorsUseGitConfigDir(self, mock_get_config):
        mock_get_config.return_value = "user@google.com"
        repo_dir = os.path.abspath("/sub/repo")

        with mock.patch.dict(os.environ):
            os.environ.pop("GIT_COOKIES_PATH", None)
            with gerrit_util.WithGitConfigDir(repo_dir):
                with mock.patch.object(
                    gerrit_util.SSOAuthenticator,
                    "_resolve_sso_cmd",
                    return_value=("git-remote-sso",),
                ):
                    self.assertTrue(
                        gerrit_util.SSOAuthenticator.is_applicable()
                    )
                mock_get_config.assert_called_with(
                    repo_dir, "user.email", default=""
                )

                mock_get_config.return_value = "/custom/.gitcookies"
                self.assertEqual(
                    gerrit_util.CookiesAuthenticator.get_gitcookies_path(),
                    "/custom/.gitcookies",
                )
                mock_get_config.assert_called_with(
                    repo_dir,
                    "http.cookiefile",
                    os.path.join("~", ".gitcookies"),
                )

                with mock.patch(
                    "subprocess.check_output", return_value=b"true\n"
                ) as mock_check_output:
                    self.assertTrue(
                        gerrit_util.GitCredsAuthenticator._is_usehttppath_set(
                            "https://chromium.googlesource.com"
                        )
                    )
                    mock_check_output.assert_called_once_with(
                        [
                            "git",
                            "config",
                            "--get-urlmatch",
                            "credential.usehttppath",
                            "https://chromium.googlesource.com",
                        ],
                        cwd=repo_dir,
                    )


if __name__ == "__main__":
    unittest.main()
