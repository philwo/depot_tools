# Copyright 2013 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""
Utilities for requesting information for a Gerrit server via HTTPS.

https://gerrit-review.googlesource.com/Documentation/rest-api.html
"""

from __future__ import annotations

import base64
import contextlib
import functools
import http.client
import http.cookiejar
import json
import logging
import os
import queue
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from dataclasses import dataclass
from io import StringIO
from multiprocessing.pool import ThreadPool
from typing import Any, Callable, Container, Dict, Generator, List, NamedTuple
from typing import Optional, Tuple, TypedDict, Union, cast

import auth
import gclient_utils
import metrics
import metrics_utils
import newauth
import scm
import subprocess2


LOGGER = logging.getLogger()
# With a starting sleep time of 12.0 seconds, x <= [1.8-2.2]x backoff, and six
# total tries, the sleep time between the first and last tries will be ~6 min
# (excluding time for each try).
TRY_LIMIT = 6
SLEEP_TIME = 12.0
MAX_BACKOFF = 2.2
MIN_BACKOFF = 1.8

# Controls the transport protocol used to communicate with Gerrit.
# This is parameterized primarily to enable GerritTestCase.
GERRIT_PROTOCOL = "https"

# Controls how many concurrent Gerrit connections there can be.
MAX_CONCURRENT_CONNECTION = 20


_GIT_CONFIG_DIR = None


@contextlib.contextmanager
def WithGitConfigDir(cwd: Optional[str]):
    global _GIT_CONFIG_DIR
    old_cwd = _GIT_CONFIG_DIR
    _GIT_CONFIG_DIR = os.path.abspath(cwd) if cwd else None
    try:
        yield
    finally:
        _GIT_CONFIG_DIR = old_cwd


def _GetGitConfigDir() -> str:
    return _GIT_CONFIG_DIR or os.getcwd()


def time_sleep(seconds):
    # Use this so that it can be mocked in tests without interfering with python
    # system machinery.
    return time.sleep(seconds)


def time_time():
    # Use this so that it can be mocked in tests without interfering with python
    # system machinery.
    return time.time()


def log_retry_and_sleep(seconds, attempt, try_limit):
    LOGGER.info(
        "Will retry in %d seconds (%d more times)...",
        seconds,
        try_limit - attempt - 1,
    )
    time_sleep(seconds)
    return seconds * random.uniform(MIN_BACKOFF, MAX_BACKOFF)


class GerritError(Exception):
    """Exception class for errors commuicating with the gerrit-on-borg service."""

    def __init__(self, http_status, message, *args, **kwargs):
        super(GerritError, self).__init__(*args, **kwargs)
        self.http_status = http_status
        self.message = "(%d) %s" % (self.http_status, message)

    def __str__(self):
        return self.message


def _QueryString(params, first_param=None):
    """Encodes query parameters in the key:val[+key:val...] format specified here:

    https://gerrit-review.googlesource.com/Documentation/rest-api-changes.html#list-changes
    """
    q = [urllib.parse.quote(first_param)] if first_param else []
    q.extend(["%s:%s" % (key, val.replace(" ", "+")) for key, val in params])
    return "+".join(q)


class SSOHelper(object):
    """SSOHelper finds a Google-internal SSO helper."""

    _sso_cmd: Optional[str] = None

    def find_cmd(self) -> str:
        """Returns the cached command-line to invoke git-remote-sso.

        Only absolute `PATH` entries are searched, never the current directory.
        If git-remote-sso is not found, returns "".
        """
        if self._sso_cmd is not None:
            return self._sso_cmd
        # Do not use `shutil.which()`: on Windows it searches the current
        # directory first (always before Python 3.12, and on later versions
        # unless `NoDefaultCurrentDirectoryInExePath` is set), so a
        # `git-remote-sso.bat` at the root of a checkout would be run and its
        # output trusted.
        cmd = gclient_utils.FindExecutableInAbsolutePath("git-remote-sso")
        if cmd is None:
            cmd = ""
        self._sso_cmd = cmd
        return cmd


# Global instance
ssoHelper = SSOHelper()


_GOOGLESOURCE_HOST_RE = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+googlesource\.com",
    re.ASCII | re.IGNORECASE,
)


def _IsGooglesourceHost(host: Optional[str]) -> bool:
    """Returns True if `host` is a bare `*.googlesource.com` hostname."""
    return bool(host and _GOOGLESOURCE_HOST_RE.fullmatch(host))


def ShouldUseSSO(host: str, email: str) -> bool:
    """Return True if we should use SSO for the given Gerrit host and user."""
    LOGGER.debug("Determining whether we should use SSO...")
    result = CheckShouldUseSSO(host, email)
    LOGGER.debug("SSO=%r: %s", result.status, result.reason)
    return result.status


class SSOCheckResult(NamedTuple):
    status: bool
    reason: str


@functools.lru_cache(maxsize=None)
def CheckShouldUseSSO(host: str, email: str) -> SSOCheckResult:
    """Checks if we should use SSO for the given Gerrit host and user."""
    if not newauth.Enabled():
        return SSOCheckResult(False, "not opted in")
    if not _IsGooglesourceHost(host):
        return SSOCheckResult(False, f"non-googlesource host {host}")
    if not ssoHelper.find_cmd():
        return SSOCheckResult(False, "no SSO command")
    if gclient_utils.IsEnvCog():
        return SSOCheckResult(True, "in Cog")
    if not email:
        return SSOCheckResult(
            True, "email is empty or missing (and SSO command available)"
        )
    if email.endswith("@google.com"):
        return SSOCheckResult(True, "email is @google.com")
    if not (email.endswith("@chromium.org") or email.endswith("@webrtc.org")):
        return SSOCheckResult(
            False, "email is not @chromium.org or @webrtc.org"
        )
    authenticator = SSOAuthenticator()
    records: list[EmailRecord] = []
    try:
        records = (
            GetAccountEmails(host, "self", authenticator=authenticator) or []
        )
    except GerritError as e:
        # Temporary message for issue
        if e.message == "Unexpected json output: <!--googleoff: all-->":
            LOGGER.warning(
                "See https://issues.chromium.org/541179825 for investigation and workarounds for this issue"
            )
            raise
        if e.http_status == 400:
            # This is likely because the user doesn't have an account on the Gerrit host.
            return SSOCheckResult(False, "account missing on Gerrit host")
        raise
    if any(email == r["email"] for r in records):
        return SSOCheckResult(True, "email is linked to @google.com email")
    return SSOCheckResult(False, "email is not linked to @google.com email")


class HttpResponse(NamedTuple):
    status: int
    reason: str
    headers: Any

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        if hasattr(self.headers, "get"):
            return self.headers.get(key, default)
        if isinstance(self.headers, dict):
            key_lower = key.lower()
            for k, v in self.headers.items():
                if k.lower() == key_lower:
                    return v
        return default


class _Authenticator(object):
    """Base authenticator class for authenticator implementations to subclass."""

    # Cached _Authenticator subclass instance, resolved via get().
    _resolved: Optional[_Authenticator] = None
    _resolved_lock = threading.Lock()

    def authenticate(self, conn: HttpConn):
        """Adds authentication information to the HttpConn."""
        raise NotImplementedError()

    def attempt_authenticate_with_reauth(
        self, conn: HttpConn, context: auth.ReAuthContext
    ) -> bool:
        """Adds authentication information to satisfy the ReAuth requirement.

        If the Gerrit operation doesn't need to satisfy ReAuth requirement
        (e.g. querying CLs), use `authenticate()` method instead.

        Returns `True` if an authorization token was added. This includes
        two scenarios:

        - A ReAuth token for `context` is available.
        - A ReAuth token for `context` isn't available, and `context` doesn't
          require ReAuth. In this case, the authentication information doesn't
          satisfy ReAuth requirements, but still authenticates the user.

        When this method returns `False`, the caller should decide if they want
        to proceed without ReAuth (i.e. call `authenticate()` method to attach
        non-ReAuth credential), or fail the operation (e.g. ask the user to
        complete ReAuth).
        """
        return False

    def debug_summary_state(self) -> str:
        """If this _Authenticator has any debugging information about its state,
        _WriteGitPushTraces will call this to include in the git push traces.

        Return value is any relevant debugging information with all PII/secrets
        redacted.
        """
        raise NotImplementedError()

    @classmethod
    def is_applicable(cls, *, gerrit_host: Optional[str] = None) -> bool:
        """Must return True if this Authenticator is available in the current
        environment.

        If `gerrit_host` isn't None, it should be the hostname of a Gerrit
        server, such as "chromium-review.googlesource.com".
        """
        raise NotImplementedError()

    def ensure_authenticated(
        self,
        *,
        gerrit_host: str,
        git_host: str,
        reauth_context: Optional[auth.ReAuthContext] = None,
    ) -> Tuple[bool, str]:
        """Returns (bypassable, error message).

        If the error message is empty, there is no error to report.
        If bypassable is true, the caller will allow the user to continue past the
        error.

        If `reauth_context` is provided, additionally checks whether the
        authentication information satisfies the ReAuth requirement.

        For the ReAuth check, Gerrit `host` attribute in `reauth_context` takes
        precedence over `gerrit_host` argument.
        """
        return (True, "")

    @classmethod
    def get(cls):
        """Returns: (_Authenticator) The identified _Authenticator to use.

        Probes the local system and its environment and identifies the
        _Authenticator instance to use.

        The resolved _Authenticator instance is cached as a class variable.
        """
        with cls._resolved_lock:
            if ret := cls._resolved:
                return ret

            use_new_auth = newauth.Enabled()

            if use_new_auth:
                LOGGER.debug("_Authenticator.get: using new auth stack")
                if LuciContextAuthenticator.is_applicable():
                    LOGGER.debug(
                        "_Authenticator.get: using LUCI context authenticator"
                    )
                    ret = LuciContextAuthenticator()
                else:
                    LOGGER.debug(
                        "_Authenticator.get: using chained authenticator"
                    )
                    a = [
                        GitcookiesHostAuthenticator(),
                        SSOAuthenticator(),
                        # GCE detection can't distinguish cloud workstations.
                        GceAuthenticator(),
                        GitCredsAuthenticator(),
                        NoAuthenticator(),
                    ]
                    ret = ChainedAuthenticator(a)
                cls._resolved = ret
                return ret
            authenticators = [
                LuciContextAuthenticator,
                GceAuthenticator,
                CookiesAuthenticator,
            ]

            for candidate in authenticators:
                if candidate.is_applicable():
                    LOGGER.debug(
                        "_Authenticator.get: Selected %s.", candidate.__name__
                    )
                    ret = candidate()
                    cls._resolved = ret
                    return ret

            auth_names = ", ".join(a.__name__ for a in authenticators)
            raise ValueError(
                f"Could not find suitable authenticator, tried: [{auth_names}]."
            )


def debug_auth() -> Tuple[str, str]:
    """Returns the name of the chosen auth scheme and any additional debugging
    information for that authentication scheme."""
    authn = _Authenticator.get()
    return authn.__class__.__name__, authn.debug_summary_state()


def ensure_authenticated(
    *,
    gerrit_host: str,
    git_host: str,
    reauth_context: Optional[auth.ReAuthContext] = None,
) -> Tuple[bool, str]:
    """Returns (bypassable, error message).

    If the error message is empty, there is no error to report. If bypassable is
    true, the caller will allow the user to continue past the error.
    """
    return _Authenticator.get().ensure_authenticated(
        gerrit_host=gerrit_host,
        git_host=git_host,
        reauth_context=reauth_context,
    )


class SSOAuthenticator(_Authenticator):
    """SSOAuthenticator implements a Google-internal authentication scheme."""

    # This is set to true in tests, allows _parse_config to consume expired
    # cookies.
    _testing_load_expired_cookies = False

    # How long we should wait for the sso helper to write and close stdout.
    # Overridden in tests.
    _timeout_secs = 5

    # The required fields for the sso config, expected from the sso helper's
    # output.
    _required_fields = frozenset(
        ["http.cookiefile", "http.proxy", "include.path"]
    )

    @dataclass
    class SSOInfo:
        proxy_host: str
        proxy_port: int
        cookies: http.cookiejar.CookieJar
        headers: Dict[str, str]

        @property
        def proxy_url(self) -> str:
            return f"http://{self.proxy_host}:{self.proxy_port}"

    # SSOInfo is a cached blob of information used by the `authenticate` method.
    _sso_info: Optional[SSOInfo] = None
    _sso_info_lock = threading.Lock()

    @classmethod
    def _resolve_sso_cmd(cls) -> Tuple[str, ...]:
        """Returns the cached command-line to invoke git-remote-sso.

        If git-remote-sso is not in $PATH, returns ().
        """
        cmd = ssoHelper.find_cmd()
        if not cmd:
            return ()
        return (
            cmd,
            "-print_config",
            "sso://*.git.corp.google.com",
        )

    @classmethod
    def is_applicable(cls, *, gerrit_host: Optional[str] = None) -> bool:
        if not cls._resolve_sso_cmd():
            return False
        email: str = scm.GIT.GetConfig(
            _GetGitConfigDir(), "user.email", default=""
        )
        if gerrit_host is not None:
            return ShouldUseSSO(gerrit_host, email)
        return email.endswith("@google.com")

    @classmethod
    def _parse_config(cls, config: Dict[str, str]) -> SSOInfo:
        """Parses the sso info from the given config.

        Note: update cls._required_fields appropriately when making
        changes to this method, to ensure the field values are captured
        from the sso helper.
        """
        fullAuthHeader = cast(
            str,
            scm.GIT.Capture(
                [
                    "config",
                    "-f",
                    config["include.path"],
                    "http.extraHeader",
                ]
            ),
        )
        headerKey, headerValue = fullAuthHeader.split(":", 1)
        headers = {headerKey.strip(): headerValue.strip()}

        proxy_host, proxy_port = config["http.proxy"].split(":", 1)

        cfpath = config["http.cookiefile"]
        cj = http.cookiejar.MozillaCookieJar(cfpath)
        # NOTE: python3.8 doesn't support httponly cookie lines, so we parse
        # this manually. Once we move to python3.10+, this hack can be removed.
        with open(cfpath) as cf:
            cookiedata = cf.read().replace("#HttpOnly_", "")
        # _really_load is the way that MozillaCookieJar subclasses
        # FileCookieJar. Calling this directly is better than reimplementing the
        # entire _really_load function manually.
        cj._really_load(
            StringIO(cookiedata),
            cfpath,
            ignore_discard=False,
            ignore_expires=cls._testing_load_expired_cookies,
        )

        return cls.SSOInfo(
            proxy_host=proxy_host,
            proxy_port=int(proxy_port),
            cookies=cj,
            headers=headers,
        )

    @classmethod
    def _launch_sso_helper(cls) -> SSOInfo:
        """Launches the git-remote-sso process and extracts the parsed SSOInfo.

        Raises an exception if something goes wrong.
        """
        cmd = cls._resolve_sso_cmd()

        with tempdir() as tdir:
            tf = os.path.join(tdir, "git-remote-sso.stderr")

            with open(tf, mode="w") as stderr_file:
                # NOTE: The git-remote-sso helper does the following:
                #
                # 1. writes files to disk.
                # 2. writes config to stdout, referencing those files.
                # 3. closes stdout (sending EOF to us, ending the stdout data).
                #    - Note: on Windows, stdout.read() times out anyway, so
                #      instead we process stdout line by line.
                # 4. waits for stdin to close.
                # 5. deletes files on disk (which is why we make sys.stdin a PIPE
                #    instead of closing it outright).
                #
                # NOTE: the http.proxy value in the emitted config points to
                # a socket which is owned by a system service, not `proc` itself.
                with subprocess2.Popen(
                    cmd,
                    stdout=subprocess2.PIPE,
                    stderr=stderr_file,
                    stdin=subprocess2.PIPE,
                    encoding="utf-8",
                ) as proc:
                    stderr_file.close()  # we can close after process starts.
                    timedout = False

                    def _fire_timeout():
                        nonlocal timedout
                        timedout = True
                        proc.kill()

                    timer = threading.Timer(cls._timeout_secs, _fire_timeout)
                    timer.start()
                    try:
                        config = {}
                        for line in proc.stdout:
                            if not line:
                                break
                            field, value = line.strip().split("=", 1)
                            config[field] = value
                            # Stop reading if we have all the required fields.
                            if cls._required_fields.issubset(config.keys()):
                                break
                    finally:
                        timer.cancel()

                    missing_fields = cls._required_fields - config.keys()
                    if timedout:
                        # Within the time limit, at least one required field was
                        # still missing. Killing the process has been triggered,
                        # even if we now have all fields.
                        details = ""
                        if missing_fields:
                            details = ": missing fields [{names}]".format(
                                names=", ".join(missing_fields)
                            )
                        LOGGER.error(
                            "SSOAuthenticator: Timeout: %r: reading config%s.",
                            cmd,
                            details,
                        )
                        raise subprocess.TimeoutExpired(
                            cmd=cmd, timeout=cls._timeout_secs
                        )
                    # if the process already ended, then something is wrong.
                    retcode = proc.poll()
                    # if stdout was closed without all required data, we need to
                    # wait for end-of-process here and hope for an error message
                    # - the poll above is racy in this case (we could see stdout
                    # EOF but the process may not have quit yet).
                    if not retcode and missing_fields:
                        retcode = proc.wait(timeout=cls._timeout_secs)
                        # We timed out while doing `wait` - we can't safely open
                        # stderr on windows, so just emit a generic timeout
                        # exception.
                        if retcode is None:
                            LOGGER.error(
                                "SSOAuthenticator: Timeout: %r: waiting error output.",
                                cmd,
                            )
                            raise subprocess.TimeoutExpired(
                                cmd=cmd, timeout=cls._timeout_secs
                            )

                    # Finally, if the poll or wait ended up getting the retcode,
                    # it means the process failed, so we can read the stderr
                    # file and reflect it back to the user.
                    if retcode is not None:
                        # process failed - we should be able to read the tempfile.
                        with open(tf, encoding="utf-8") as stderr:
                            sys.exit(
                                f"SSOAuthenticator: exit {retcode}: {stderr.read().strip()}"
                            )

                    return cls._parse_config(config)

    @classmethod
    def _get_sso_info(cls) -> SSOInfo:
        with cls._sso_info_lock:
            info = cls._sso_info
            if not info:
                info = cls._launch_sso_helper()
                # HACK: `git-remote-sso` doesn't always properly warm up the
                # cookies - in this case, invoke `git-remote-sso` directly
                # against a public repo with two URL arguments so neither git
                # nor git-remote-http rewrites the URL via url.*.insteadOf,
                # and then try pulling config again.
                #
                # BUG: b/342644760
                if not any(c.domain == ".google.com" for c in info.cookies):
                    LOGGER.debug("SSO: Refreshing .google.com cookies.")
                    url = "sso://chromium/All-Projects"
                    subprocess2.check_output(
                        [ssoHelper.find_cmd(), url, url],
                        stdin=b"list\n\n",
                    )
                    info = cls._launch_sso_helper()
                    if not any(c.domain == ".google.com" for c in info.cookies):
                        raise ValueError(
                            "Unable to extract .google.com cookie."
                        )
                cls._sso_info = info
            return info

    def authenticate(self, conn: HttpConn):
        sso_info = self._get_sso_info()
        conn.proxy = sso_info.proxy_url
        conn.req_headers.update(sso_info.headers)
        conn.req_headers.setdefault("User-Agent", "depot_tools")
        conn.cookie_jar = sso_info.cookies

        # Now we must rewrite:
        #   https://xxx.googlesource.com ->
        #   http://xxx.git.corp.google.com
        parsed = urllib.parse.urlparse(conn.req_uri)
        parsed = parsed._replace(scheme="http")
        if (hostname := parsed.hostname) and hostname.endswith(
            ".googlesource.com"
        ):
            assert not parsed.port, (
                "SSOAuthenticator: netloc: port not supported"
            )
            assert not parsed.username, (
                "SSOAuthenticator: netloc: username not supported"
            )
            assert not parsed.password, (
                "SSOAuthenticator: netloc: password not supported"
            )

            hostname_parts = hostname.rsplit(".", 2)  # X, googlesource, com
            conn.req_host = hostname_parts[0] + ".git.corp.google.com"
            parsed = parsed._replace(netloc=conn.req_host)
        conn.req_uri = parsed.geturl()

        # Finally, add cookies
        sso_info.cookies.add_cookie_header(conn)
        assert conn.has_header("Cookie"), (
            "sso_info.cookies.add_cookie_header failed to add Cookie."
        )

    def attempt_authenticate_with_reauth(
        self, conn: HttpConn, context: auth.ReAuthContext
    ) -> bool:
        self.authenticate(conn)
        return True  # SSO credential satisfies ReAuth requirement.

    def debug_summary_state(self) -> str:
        return ""


class CookiesAuthenticator(_Authenticator):
    """_Authenticator implementation that uses ".gitcookies" for token.

    Expected case for developer workstations.
    """

    _EMPTY = object()

    def __init__(self):
        # Credentials will be loaded lazily on first use. This ensures
        # _Authenticator get() can always construct an authenticator, even if
        # something is broken. This allows 'creds-check' to proceed to actually
        # checking creds later, rigorously (instead of blowing up with a cryptic
        # error if they are wrong).
        self._gitcookies = self._EMPTY

    @classmethod
    def is_applicable(cls, *, gerrit_host: Optional[str] = None) -> bool:
        # We consider CookiesAuthenticator always applicable for now.
        return True

    @property
    def gitcookies(self):
        if self._gitcookies is self._EMPTY:
            self._gitcookies = self._get_gitcookies()
        return self._gitcookies

    @classmethod
    def get_new_password_url(cls, host):
        assert not host.startswith("http")
        # Assume *.googlesource.com pattern.
        parts = host.split(".")

        # remove -review suffix if present.
        if parts[0].endswith("-review"):
            parts[0] = parts[0][: -len("-review")]

        return "https://%s/new-password" % (".".join(parts))

    @classmethod
    def _get_new_password_message(cls, host):
        if host is None:
            return (
                "Git host for Gerrit upload is unknown. Check your remote "
                "and the branch your branch is tracking. This tool assumes "
                "that you are using a git server at *.googlesource.com."
            )
        url = cls.get_new_password_url(host)
        return "You can (re)generate your credentials by visiting %s" % url

    @classmethod
    def get_gitcookies_path(cls) -> str:
        if envVal := os.getenv("GIT_COOKIES_PATH"):
            return envVal

        return os.path.expanduser(
            scm.GIT.GetConfig(
                _GetGitConfigDir(),
                "http.cookiefile",
                os.path.join("~", ".gitcookies"),
            )
        )

    @classmethod
    def _get_gitcookies(cls):
        gitcookies = {}
        path = cls.get_gitcookies_path()
        if not os.path.exists(path):
            return gitcookies

        try:
            f = gclient_utils.FileRead(path, "rb").splitlines()
        except IOError:
            return gitcookies

        for line in f:
            try:
                fields = line.strip().split("\t")
                if line.strip().startswith("#") or len(fields) != 7:
                    continue
                domain, xpath, key, value = (
                    fields[0],
                    fields[2],
                    fields[5],
                    fields[6],
                )
                if xpath == "/" and key == "o":
                    if value.startswith("git-"):
                        login, secret_token = value.split("=", 1)
                        gitcookies[domain] = (login, secret_token)
                    else:
                        gitcookies[domain] = ("", value)
            except (IndexError, ValueError, TypeError) as exc:
                LOGGER.warning(exc)
        return gitcookies

    def _get_auth_for_host(self, host):
        # Don't use http.cookiejar.domain_match: it uses an unanchored
        # substring search, so ".googlesource.com" would match
        # "chromium.googlesource.com.evil.com" and leak credentials.
        bare_host = host.split(":", 1)[0].lower()
        for domain, creds in self.gitcookies.items():
            domain_low = domain.lower()
            if bare_host == domain_low or (
                domain_low.startswith(".")
                and (
                    bare_host == domain_low[1:]
                    or bare_host.endswith(domain_low)
                )
            ):
                return (creds[0], creds[1])
        return None

    def authenticate(self, conn: HttpConn):
        a = self._get_auth_for_host(conn.req_host)
        if a:
            login, cred = a
            if login:
                secret = base64.b64encode(f"{login}:{cred}".encode("utf-8"))
                conn.req_headers["Authorization"] = (
                    f"Basic {secret.decode('utf-8')}"
                )
            else:
                conn.req_headers["Authorization"] = f"Bearer {cred}"

    def ensure_authenticated(
        self,
        *,
        gerrit_host: str,
        git_host: str,
        reauth_context: Optional[auth.ReAuthContext] = None,
    ) -> Tuple[bool, str]:
        """Returns (bypassable, error message).

        If the error message is empty, there is no error to report.
        If bypassable is true, the caller will allow the user to continue past the
        error.

        Some bots are still using .gitcookies. They're marked as trusted robots,
        and therefore already satisfies ReAuth requirement. So we don't need to
        perform extra ReAuth checks here for bots.

        Human users shouldn't be using .gitcookies by now.
        """
        # Lazy-loader to identify Gerrit and Git hosts.
        gerrit_auth = self._get_auth_for_host(gerrit_host)
        git_auth = self._get_auth_for_host(git_host)
        if gerrit_auth and git_auth:
            if gerrit_auth == git_auth:
                return True, ""
            all_gsrc = self._get_auth_for_host("d0esN0tEx1st.googlesource.com")
            print(
                "WARNING: You have different credentials for Gerrit and git hosts:\n"
                "           %s\n"
                "           %s\n"
                "        Consider running the following command:\n"
                "          git cl creds-check\n"
                "        %s\n"
                "        %s"
                % (
                    git_host,
                    gerrit_host,
                    (
                        "Hint: delete creds for .googlesource.com"
                        if all_gsrc
                        else ""
                    ),
                    self._get_new_password_message(git_host),
                )
            )
            return True, "If you know what you are doing"

        missing = ([] if gerrit_auth else [gerrit_host]) + (
            [] if git_auth else [git_host]
        )
        return False, (
            "Credentials for the following hosts are required:\n"
            "  %s\n"
            "These are read from %s\n"
            "%s"
            % (
                "\n  ".join(missing),
                self.get_gitcookies_path(),
                self._get_new_password_message(git_host),
            )
        )

    @classmethod
    def _redact_cookie_value(cls, value: str) -> str:
        if value.startswith("git-") and "=" in value:
            login, _ = value.split("=", 1)
            return f"{login}=REDACTED"
        return "REDACTED"

    @classmethod
    def _redact_gitcookies_line(cls, line: str) -> str:
        if not line.strip():
            return line
        fields = line.split("\t")
        if len(fields) == 7:
            fields[6] = cls._redact_cookie_value(fields[6])
            return "\t".join(fields)
        return "REDACTED"

    def debug_summary_state(self) -> str:
        gitcookies_path = self.get_gitcookies_path()
        if not os.path.isfile(gitcookies_path):
            return ""
        try:
            gitcookies = gclient_utils.FileRead(gitcookies_path)
        except OSError:
            return ""
        out = []
        for raw_line in gitcookies.splitlines(keepends=True):
            body = raw_line.rstrip("\r\n")
            ending = raw_line[len(body) :]
            out.append(f"{self._redact_gitcookies_line(body)}{ending}")
        return "".join(out)

    def get_auth_email(self, host):
        """Best effort parsing of email to be used for auth for the given host."""
        a = self._get_auth_for_host(host)
        if not a:
            return None
        login = a[0]
        # login typically looks like 'git-xxx.example.com'
        if not login.startswith("git-") or "." not in login:
            return None
        username, domain = login[len("git-") :].split(".", 1)
        return "%s@%s" % (username, domain)


class GitcookiesHostAuthenticator(CookiesAuthenticator):
    """CookiesAuthenticator for the hosts that .gitcookies has credentials for.

    Lets the new auth stack keep using .gitcookies for those hosts.
    """

    def is_applicable(self, *, gerrit_host: Optional[str] = None) -> bool:
        return bool(gerrit_host and self._get_auth_for_host(gerrit_host))


class GceAuthenticator(_Authenticator):
    """_Authenticator implementation that uses GCE metadata service for token."""

    _INFO_URL = "http://metadata.google.internal"
    _ACQUIRE_URL = (
        "%s/computeMetadata/v1/instance/"
        "service-accounts/default/token" % _INFO_URL
    )
    _ACQUIRE_HEADERS = {"Metadata-Flavor": "Google"}

    _cache_is_gce = None
    _token_cache = None
    _token_expiration = None

    @classmethod
    def is_applicable(cls, *, gerrit_host: Optional[str] = None):
        if gerrit_host is not None and not _IsGooglesourceHost(gerrit_host):
            return False
        if os.getenv("SKIP_GCE_AUTH_FOR_GIT"):
            return False
        if cls._cache_is_gce is None:
            cls._cache_is_gce = cls._test_is_gce()
        return cls._cache_is_gce

    @classmethod
    def _test_is_gce(cls):
        # Based on https://cloud.google.com/compute/docs/metadata#runninggce
        resp, _ = cls._get(cls._INFO_URL)
        if resp is None:
            return False
        return resp.get("metadata-flavor") == "Google"

    @staticmethod
    def _get(url, **kwargs):
        next_delay_sec = 1.0
        headers = kwargs.get("headers", {})
        for i in range(TRY_LIMIT):
            p = urllib.parse.urlparse(url)
            if p.scheme not in ("http", "https"):
                raise RuntimeError(
                    "Don't know how to work with protocol '%s'" % p.scheme
                )
            req = urllib.request.Request(url, headers=headers, method="GET")
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({})
            )
            try:
                with opener.open(req, timeout=30) as resp:
                    response = HttpResponse(
                        status=resp.status,
                        reason=resp.reason,
                        headers=resp.headers,
                    )
                    contents = resp.read()
            except urllib.error.HTTPError as e:
                response = HttpResponse(
                    status=e.code,
                    reason=e.reason,
                    headers=e.headers,
                )
                contents = e.read()
            except (
                urllib.error.URLError,
                socket.error,
                TimeoutError,
                OSError,
            ) as e:
                LOGGER.debug("GET [%s] raised %s", url, e)
                return None, None
            LOGGER.debug(
                "GET [%s] #%d/%d (%d)", url, i + 1, TRY_LIMIT, response.status
            )
            if response.status < 500:
                return (response, contents)

            # Retry server error status codes.
            LOGGER.warning("Encountered server error")
            if TRY_LIMIT - i > 1:
                next_delay_sec = log_retry_and_sleep(
                    next_delay_sec, i, TRY_LIMIT
                )
        return None, None

    @classmethod
    def _get_token_dict(cls):
        # If cached token is valid for at least 25 seconds, return it.
        if cls._token_cache and time_time() + 25 < cls._token_expiration:
            return cls._token_cache

        resp, contents = cls._get(
            cls._ACQUIRE_URL, headers=cls._ACQUIRE_HEADERS
        )
        if resp is None or resp.status != 200:
            return None
        cls._token_cache = json.loads(contents)
        cls._token_expiration = cls._token_cache["expires_in"] + time_time()
        return cls._token_cache

    def authenticate(self, conn: HttpConn):
        if not _IsGooglesourceHost(conn.req_host):
            return
        token_dict = self._get_token_dict()
        if not token_dict:
            return
        conn.req_headers["Authorization"] = (
            "%(token_type)s %(access_token)s" % token_dict
        )

    def attempt_authenticate_with_reauth(
        self, conn: HttpConn, context: auth.ReAuthContext
    ) -> bool:
        if not _IsGooglesourceHost(conn.req_host):
            return False
        # GCE bots do not need ReAuth. They implicitly satisfy the requirement if otherwise
        # trusted.
        self.authenticate(conn)
        return True

    def debug_summary_state(self) -> str:
        # TODO(b/343230702) - report ambient account name.
        return ""


class LuciContextAuthenticator(_Authenticator):
    """_Authenticator implementation that uses LUCI_CONTEXT ambient local auth."""

    @staticmethod
    def is_applicable(*, gerrit_host: Optional[str] = None):
        if gerrit_host is not None and not _IsGooglesourceHost(gerrit_host):
            return False
        return auth.has_luci_context_local_auth()

    def __init__(self):
        self._authenticator = auth.Authenticator(
            " ".join([auth.OAUTH_SCOPE_EMAIL, auth.OAUTH_SCOPE_GERRIT])
        )

    @property
    def luci_auth(self) -> auth.Authenticator:
        return self._authenticator

    def authenticate(self, conn: HttpConn):
        if not _IsGooglesourceHost(conn.req_host):
            return
        conn.req_headers["Authorization"] = (
            f"Bearer {self._authenticator.get_access_token().token}"
        )

    def attempt_authenticate_with_reauth(
        self, conn: HttpConn, context: auth.ReAuthContext
    ) -> bool:
        if not _IsGooglesourceHost(conn.req_host):
            return False
        # LUCI context credential satisfies ReAuth requirement.
        self.authenticate(conn)
        return True

    def debug_summary_state(self) -> str:
        # TODO(b/343230702) - report ambient account name.
        return ""


class GitCredsAuthenticator(_Authenticator):
    """_Authenticator implementation that uses `git-credential-luci` with OAuth.

    This is similar to LuciContextAuthenticator, except that it is for
    local non-google.com developer credentials.
    """

    def __init__(self):
        self._authenticator = auth.GerritAuthenticator()

    @property
    def luci_auth(self) -> auth.Authenticator:
        return self._authenticator

    def authenticate(self, conn: HttpConn):
        if not _IsGooglesourceHost(conn.req_host):
            return
        conn.req_headers["Authorization"] = (
            f"Bearer {self._authenticator.get_access_token()}"
        )

    def attempt_authenticate_with_reauth(
        self, conn: HttpConn, context: auth.ReAuthContext
    ) -> bool:
        if not _IsGooglesourceHost(conn.req_host):
            return False
        try:
            header = self._authenticator.get_authorization_header(context)
            conn.req_headers["Authorization"] = header
            return True
        except auth.GitReAuthRequiredError:
            return False

    def debug_summary_state(self) -> str:
        # TODO(b/343230702) - report ambient account name.
        return ""

    @classmethod
    def gerrit_account_exists(cls, host: str) -> bool:
        """Return True if the Gerrit account exists.

        This checks the user currently logged in with git-credential-luci.
        If the user is not logged in with git-credential-luci, returns False.

        This method caches positive results in the user's Git config.
        """
        if not _IsGooglesourceHost(host):
            return False
        try:
            EnsureAccountExists(host, authenticator=cls())
            return True
        except auth.GitLoginRequiredError:
            return False
        except GerritError as e:
            if e.http_status in (400, 404):
                return False
            raise

    @functools.cache
    def is_applicable(self, *, gerrit_host: Optional[str] = None):
        if not gerrit_host or not _IsGooglesourceHost(gerrit_host):
            return False
        return self.gerrit_account_exists(gerrit_host)

    def ensure_authenticated(
        self,
        *,
        gerrit_host: str,
        git_host: str,
        reauth_context: Optional[auth.ReAuthContext] = None,
    ) -> Tuple[bool, str]:
        try:
            if not reauth_context:
                self._authenticator.get_access_token()
            else:
                self._authenticator.get_authorization_header(reauth_context)
                # Also check that the user's Git config has useHttpPath set.
                if not self._is_usehttppath_set(f"https://{git_host}"):
                    return (
                        False,
                        "You have not set credential.useHttpPath in your Git config."
                        " You can set this by running:\n"
                        "  git config credential.useHttpPath true",
                    )
            return (True, "")
        except (
            auth.GitLoginRequiredError,
            auth.GitReAuthRequiredError,
            auth.GitUnknownError,
        ) as e:
            return (False, str(e))

    @staticmethod
    def _is_usehttppath_set(url: str) -> bool:
        try:
            # scm.GIT doesn't support --get-urlmatch
            output = subprocess.check_output(
                [
                    "git",
                    "config",
                    "--get-urlmatch",
                    "credential.usehttppath",
                    url,
                ],
                cwd=_GetGitConfigDir(),
            )
            return output.decode().rstrip() in ["true", "yes", "on"]
        except subprocess.CalledProcessError as e:
            LOGGER.warning(
                "credential.useHttpPath check failed (exit: %r, output: %r)",
                e.returncode,
                e.output,
            )
            return False


class NoAuthenticator(_Authenticator):
    """_Authenticator implementation that does no auth."""

    @staticmethod
    def is_applicable(*, gerrit_host: Optional[str] = None):
        return True

    def authenticate(self, conn: HttpConn):
        pass

    def debug_summary_state(self) -> str:
        return ""


class ChainedAuthenticator(_Authenticator):
    """_Authenticator that delegates to others in sequence.

    Authenticators should implement the method `is_applicable_for`.
    """

    def __init__(self, authenticators: List[_Authenticator]):
        self._authenticators = list(authenticators)

    def is_applicable(self, *, gerrit_host: Optional[str] = None) -> bool:
        return bool(
            next(self.applicable_authenticators(gerrit_host=gerrit_host), None)
        )

    def applicable_authenticators(
        self, *, gerrit_host: Optional[str] = None
    ) -> Generator[_Authenticator, None, None]:
        """Returns a generator that yields applicable authenticators."""
        for a in self._authenticators:
            if a.is_applicable(gerrit_host=gerrit_host):
                yield a

    def authenticate(self, conn: HttpConn):
        for a in self.applicable_authenticators(gerrit_host=conn.host):
            a.authenticate(conn)
            break
        else:
            raise ValueError(
                f"{self!r} has no applicable authenticator for {conn!r}"
            )

    def attempt_authenticate_with_reauth(
        self, conn: HttpConn, context: auth.ReAuthContext
    ) -> bool:
        for a in self.applicable_authenticators(gerrit_host=conn.host):
            if a.attempt_authenticate_with_reauth(conn, context):
                return True
        return False

    def debug_summary_state(self) -> str:
        return ""

    def ensure_authenticated(
        self,
        *,
        gerrit_host: str,
        git_host: str,
        reauth_context: Optional[auth.ReAuthContext] = None,
    ) -> Tuple[bool, str]:
        for a in self.applicable_authenticators(gerrit_host=gerrit_host):
            bypassable, err_msg = a.ensure_authenticated(
                gerrit_host=gerrit_host,
                git_host=git_host,
                reauth_context=reauth_context,
            )
            if err_msg:
                LOGGER.info(
                    "%s.ensure_authenticated() returns an %s error: %s",
                    a.__class__.__name__,
                    "a bypassable" if bypassable else "an",
                    err_msg,
                )
            return bypassable, err_msg
        return (
            False,
            f"{self!r} has no applicable authenticator for {gerrit_host}",
        )


class ReqParams(TypedDict):
    uri: str
    method: str
    headers: Dict[str, str]
    body: Optional[str]


class HttpConn:
    """HttpConn holds request parameters and performs HTTP requests."""

    def __init__(
        self,
        *,
        req_host: str,
        req_uri: str,
        req_method: str,
        req_headers: Dict[str, str],
        req_body: Optional[str],
        timeout: int = 300,
        proxy: Optional[str] = None,
        cookie_jar: Optional[http.cookiejar.CookieJar] = None,
    ) -> None:
        self.req_host = req_host
        self.req_uri = req_uri
        self.req_method = req_method
        self.req_headers = req_headers
        self.unredirected_headers: Dict[str, str] = {}
        self.req_body = req_body
        self.timeout = timeout
        self.proxy = proxy
        self.cookie_jar = cookie_jar

    @property
    def req_params(self) -> ReqParams:
        return {
            "uri": self.req_uri,
            "method": self.req_method,
            "headers": self.req_headers,
            "body": self.req_body,
        }

    def request(
        self,
        uri: Optional[str] = None,
        method: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[Union[str, bytes]] = None,
    ) -> Tuple[HttpResponse, bytes]:
        uri = uri or self.req_uri
        method = method or self.req_method
        req_headers = headers if headers is not None else self.req_headers
        body_data = body if body is not None else self.req_body
        if isinstance(body_data, str):
            data: Optional[bytes] = body_data.encode("utf-8")
        else:
            data = body_data

        req = urllib.request.Request(
            url=uri,
            data=data,
            headers=req_headers,
            method=method,
        )
        for k, v in self.unredirected_headers.items():
            req.add_unredirected_header(k, v)

        handlers: list[urllib.request.BaseHandler] = []
        if self.cookie_jar is not None:
            handlers.append(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        if self.proxy:
            handlers.append(
                urllib.request.ProxyHandler(
                    {"http": self.proxy, "https": self.proxy}
                )
            )
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                return (
                    HttpResponse(
                        status=resp.status,
                        reason=resp.reason,
                        headers=resp.headers,
                    ),
                    resp.read(),
                )
        except urllib.error.HTTPError as e:
            return (
                HttpResponse(
                    status=e.code,
                    reason=e.reason,
                    headers=e.headers,
                ),
                e.read(),
            )
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise socket.timeout(str(e.reason)) from e
            raise

    # Interface for CookieJar.add_cookie_header
    def has_header(self, header: str) -> bool:
        return header in self.req_headers or header in self.unredirected_headers

    def get_full_url(self) -> str:
        return self.req_uri

    def get_header(
        self, header: str, default: Optional[str] = None
    ) -> Optional[str]:
        return self.req_headers.get(
            header, self.unredirected_headers.get(header, default)
        )

    def add_unredirected_header(self, header: str, value: str):
        self.unredirected_headers[header] = value

    @property
    def unverifiable(self) -> bool:
        return False

    @property
    def origin_req_host(self) -> str:
        return self.req_host

    @property
    def type(self) -> str:
        return urllib.parse.urlparse(self.req_uri).scheme

    @property
    def host(self) -> str:
        return self.req_host


def CreateHttpConn(
    host: str,
    path: str,
    reqtype="GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Dict] = None,
    timeout: Optional[Union[int, float]] = 300,
    *,
    authenticator: Optional[_Authenticator] = None,
    reauth_context: Optional[auth.ReAuthContext] = None,
    reauth_is_optional: bool = False,
) -> HttpConn:
    """Opens an HTTPS connection to a Gerrit service, and sends a request.

    If `reauth_context` is provided, creates an HttpConn with authentication
    information that satisfies ReAuth requirements.

    When ReAuth failed, this method raises an exception to indicate why
    ReAuth failed. If `reauth_is_optional` is True, this method fallbacks to
    creating an authenticated, non-ReAuth HttpConn (e.g. using access
    tokens).
    """
    headers = headers or {}
    bare_host = host.partition(":")[0]

    url = path
    if not url.startswith("/"):
        url = "/" + url
    if not url.startswith("/a/"):
        url = "/a%s" % url

    rendered_body: Optional[str] = None
    if body:
        rendered_body = json.dumps(body, sort_keys=True)
        headers.setdefault("Content-Type", "application/json")

    uri = urllib.parse.urljoin(f"{GERRIT_PROTOCOL}://{host}", url)
    conn = HttpConn(
        timeout=timeout,
        req_host=host,
        req_uri=uri,
        req_method=reqtype,
        req_headers=headers,
        req_body=rendered_body,
    )

    if authenticator is None:
        authenticator = _Authenticator.get()
    # TODO(crbug.com/1059384): Automatically detect when running on cloudtop.
    if isinstance(authenticator, GceAuthenticator):
        print(
            "If you're on a cloudtop instance, export "
            "SKIP_GCE_AUTH_FOR_GIT=1 in your env."
        )

    if reauth_context:
        reauth_succeed = authenticator.attempt_authenticate_with_reauth(
            conn, reauth_context
        )
        if not reauth_succeed:
            if reauth_is_optional:
                LOGGER.debug(
                    "ReAuth requested, but the authenticator didn't return a token "
                    "that satisfies ReAuth requirement."
                )
                authenticator.authenticate(conn)
            else:
                raise auth.GitReAuthRequiredError()
    else:
        # ReAuth wasn't requested.
        authenticator.authenticate(conn)

    if "Authorization" not in conn.req_headers:
        LOGGER.debug("No authorization found for %s." % bare_host)

    if LOGGER.isEnabledFor(logging.DEBUG):
        LOGGER.debug("%s %s", conn.req_method, conn.req_uri)
        LOGGER.debug("conn.proxy=%r", conn.proxy)
        for key, val in conn.req_headers.items():
            if key in ("Authorization", "Cookie"):
                val = "HIDDEN"
            LOGGER.debug("%s: %s", key, val)
        if conn.req_body:
            LOGGER.debug(conn.req_body)

    return conn


def ReadHttpResponse(
    conn: HttpConn,
    accept_statuses: Container[int] = frozenset([200]),
    max_tries=TRY_LIMIT,
):
    """Reads an HTTP response from a connection into a string buffer.

    Args:
        conn: An Http object created by CreateHttpConn above.
        accept_statuses: Treat any of these statuses as success. Default: [200]
            Common additions include 204, 400, and 404.
        max_tries: The maximum number of times the request should be attempted.
    Returns:
        A string buffer containing the connection's reply.
    """
    response = contents = None
    sleep_time = SLEEP_TIME
    for idx in range(max_tries):
        before_response = time.time()
        try:
            response, contents = conn.request(**conn.req_params)
        except socket.timeout:
            if idx < max_tries - 1:
                sleep_time = log_retry_and_sleep(sleep_time, idx, max_tries)
                continue
            raise
        contents = contents.decode("utf-8", "replace")

        response_time = time.time() - before_response
        metrics.collector.add_repeated(
            "http_requests",
            metrics_utils.extract_http_metrics(
                conn.req_params["uri"],
                conn.req_params["method"],
                response.status,
                response_time,
            ),
        )

        # If response.status is an accepted status,
        # or response.status < 500 then the result is final; break retry loop.
        # If the response is 404/409 it might be because of replication lag,
        # so keep trying anyway. If it is 429, it is generally ok to retry after
        # a backoff.
        if (
            response.status in accept_statuses
            or response.status < 500
            and response.status not in [404, 409, 429]
        ):
            LOGGER.debug(
                "got response %d for %s %s",
                response.status,
                conn.req_params["method"],
                conn.req_params["uri"],
            )
            # If 404 was in accept_statuses, then it's expected that the file
            # might not exist, so don't return the gitiles error page because
            # that's not the "content" that was actually requested.
            if response.status == 404:
                contents = ""
            break

        # A status >=500 is assumed to be a possible transient error; retry.
        LOGGER.warning(
            "A transient error occurred while querying %s:\n%s %s\n%d %s\n%s",
            conn.req_host,
            conn.req_params["method"],
            conn.req_params["uri"],
            response.status,
            response.reason,
            contents,
        )

        if idx < max_tries - 1:
            sleep_time = log_retry_and_sleep(sleep_time, idx, max_tries)
    # end of retries loop

    # Help the type checker a bit here - it can't figure out the `except` logic
    # in the loop above.
    assert response, (
        "Impossible: End of retry loop without response or exception."
    )

    if response.status in accept_statuses:
        return StringIO(contents)

    if response.status in (302, 401, 403):
        www_authenticate = response.get("www-authenticate")
        if not www_authenticate:
            print("Your Gerrit credentials might be misconfigured.")
        elif not newauth.Enabled():
            auth_match = re.search('realm="([^"]+)"', www_authenticate, re.I)
            host = auth_match.group(1) if auth_match else conn.req_host
            new_password_url = CookiesAuthenticator.get_new_password_url(host)
            print(
                "Authentication failed. Please make sure your .gitcookies "
                f"file has credentials for {host}."
            )
            print(f"(Re)generate credentials here: {new_password_url}")
        print("Try:\n  git cl creds-check")

    reason = "%s: %s" % (response.reason, contents)
    raise GerritError(response.status, reason)


def ReadHttpJsonResponse(
    conn,
    accept_statuses: Container[int] = frozenset([200]),
    max_tries=TRY_LIMIT,
) -> dict:
    """Parses an https response as json."""
    fh = ReadHttpResponse(conn, accept_statuses, max_tries)
    # The first line of the response should always be: )]}'
    s = fh.readline()
    if s and s.rstrip() != ")]}'":
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug(
                "ReadHttpJsonResponse: Error parsing, first line: %r", s
            )
            LOGGER.debug(
                "ReadHttpJsonResponse: Error parsing, rest: %r", fh.read()
            )
        raise GerritError(200, "Unexpected json output: %s" % s[:100])
    s = fh.read()
    if not s:
        return {}
    return json.loads(s)


# Callers such as `gerrit_client.py rawapi` invoke Gerrit REST endpoints
# directly via CallGerritApi without going through CreateDraft or SetReview.
# Match draft and review endpoints here too so AI agent comments are still
# tagged with "is_ai": True.
_GERRIT_DRAFTS_PATH_RE = re.compile(
    r"^(?:a/)?changes/.+/revisions/[^/]+/drafts(?:/[^/]+)?$"
)
_GERRIT_REVIEW_PATH_RE = re.compile(
    r"^(?:a/)?changes/.+/revisions/[^/]+/review$"
)


def _SetAiReviewComments(comments):
    if not isinstance(comments, dict):
        return
    for comment_list in comments.values():
        if isinstance(comment_list, list):
            for comment in comment_list:
                if isinstance(comment, dict):
                    comment.setdefault("is_ai", True)


def CallGerritApi(host, path, **kwargs):
    """Helper for calling a Gerrit API that returns a JSON response."""
    reqtype = (kwargs.get("reqtype") or "GET").upper()
    body = kwargs.get("body")
    if isinstance(body, dict) and gclient_utils.IsEnvAi():
        norm_path = urllib.parse.urlsplit(path).path.strip("/")
        if reqtype == "PUT" and _GERRIT_DRAFTS_PATH_RE.match(norm_path):
            body.setdefault("is_ai", True)
        elif reqtype == "POST" and _GERRIT_REVIEW_PATH_RE.match(norm_path):
            _SetAiReviewComments(body.get("comments"))

    conn_kwargs = {}
    conn_kwargs.update(
        (k, kwargs[k]) for k in ["reqtype", "headers", "body"] if k in kwargs
    )
    conn = CreateHttpConn(host, path, **conn_kwargs)
    read_kwargs = {}
    read_kwargs.update(
        (k, kwargs[k]) for k in ["accept_statuses"] if k in kwargs
    )
    return ReadHttpJsonResponse(conn, **read_kwargs)


def QueryChanges(
    host, params, first_param=None, limit=None, o_params=None, start=None
):
    """
    Queries a gerrit-on-borg server for changes matching query terms.

    Args:
        params: A list of key:value pairs for search parameters, as documented
            here (e.g. ('is', 'owner') for a parameter 'is:owner'):
            https://gerrit-review.googlesource.com/Documentation/user-search.html#search-operators
        first_param: A change identifier
        limit: Maximum number of results to return.
        start: how many changes to skip (starting with the most recent)
        o_params: A list of additional output specifiers, as documented here:
            https://gerrit-review.googlesource.com/Documentation/rest-api-changes.html#list-changes

    Returns:
        A list of json-decoded query results.
    """
    # Note that no attempt is made to escape special characters; YMMV.
    if not params and not first_param:
        raise RuntimeError("QueryChanges requires search parameters")
    path = "changes/?q=%s" % _QueryString(params, first_param)
    if start:
        path = "%s&start=%s" % (path, start)
    if limit:
        path = "%s&n=%d" % (path, limit)
    if o_params:
        path = "%s&%s" % (path, "&".join(["o=%s" % p for p in o_params]))
    return ReadHttpJsonResponse(CreateHttpConn(host, path, timeout=30))


def GenerateAllChanges(
    host, params, first_param=None, limit=500, o_params=None, start=None
):
    """Queries a gerrit-on-borg server for all the changes matching the query
    terms.

    WARNING: this is unreliable if a change matching the query is modified while
    this function is being called.

    A single query to gerrit-on-borg is limited on the number of results by the
    limit parameter on the request (see QueryChanges) and the server maximum
    limit.

    Args:
        params, first_param: Refer to QueryChanges().
        limit: Maximum number of requested changes per query.
        o_params: Refer to QueryChanges().
        start: Refer to QueryChanges().

    Returns:
        A generator object to the list of returned changes.
    """
    already_returned = set()

    def at_most_once(cls):
        for cl in cls:
            if cl["_number"] not in already_returned:
                already_returned.add(cl["_number"])
                yield cl

    start = start or 0
    cur_start = start
    more_changes = True

    while more_changes:
        # This will fetch changes[start..start+limit] sorted by most recently
        # updated. Since the rank of any change in this list can be changed any
        # time (say user posting comment), subsequent calls may overalp like
        # this: > initial order ABCDEFGH query[0..3]  => ABC > E gets updated.
        # New order: EABCDFGH query[3..6] => CDF   # C is a dup query[6..9] =>
        # GH    # E is missed.
        page = QueryChanges(
            host, params, first_param, limit, o_params, cur_start
        )
        for cl in at_most_once(page):
            yield cl

        more_changes = [cl for cl in page if "_more_changes" in cl]
        if len(more_changes) > 1:
            raise GerritError(
                200,
                "Received %d changes with a _more_changes attribute set but should "
                "receive at most one." % len(more_changes),
            )
        if more_changes:
            cur_start += len(page)

    # If we paged through, query again the first page which in most
    # circumstances will fetch all changes that were modified while this
    # function was run.
    if start != cur_start:
        page = QueryChanges(host, params, first_param, limit, o_params, start)
        for cl in at_most_once(page):
            yield cl


def MultiQueryChanges(
    host, params, change_list, limit=None, o_params=None, start=None
):
    """Initiate a query composed of multiple sets of query parameters."""
    if not change_list:
        raise RuntimeError(
            "MultiQueryChanges requires a list of change numbers/id's"
        )
    q = [
        "q=%s" % "+OR+".join([urllib.parse.quote(str(x)) for x in change_list])
    ]
    if params:
        q.append(_QueryString(params))
    if limit:
        q.append("n=%d" % limit)
    if start:
        q.append("S=%s" % start)
    if o_params:
        q.extend(["o=%s" % p for p in o_params])
    path = "changes/?%s" % "&".join(q)
    try:
        result = ReadHttpJsonResponse(CreateHttpConn(host, path))
    except GerritError as e:
        msg = "%s:\n%s" % (e.message, path)
        raise GerritError(e.http_status, msg)
    return result


def GetGerritFetchUrl(host):
    """Given a Gerrit host name returns URL of a Gerrit instance to fetch from."""
    return "%s://%s/" % (GERRIT_PROTOCOL, host)


def GetChangePageUrl(host, change_number):
    """Given a Gerrit host name and change number, returns change page URL."""
    return "%s://%s/#/c/%d/" % (GERRIT_PROTOCOL, host, change_number)


def GetChangeUrl(host, change):
    """Given a Gerrit host name and change ID, returns a URL for the change."""
    return "%s://%s/a/changes/%s" % (GERRIT_PROTOCOL, host, change)


def GetChange(host, change, accept_statuses: Container[int] = frozenset([200])):
    """Queries a Gerrit server for information about a single change."""
    path = "changes/%s" % change
    return ReadHttpJsonResponse(
        CreateHttpConn(host, path), accept_statuses=accept_statuses
    )


def GetChangeDetail(host, change, o_params=None):
    """Queries a Gerrit server for extended information about a single change."""
    path = "changes/%s/detail" % change
    if o_params:
        path += "?%s" % "&".join(["o=%s" % p for p in o_params])
    return ReadHttpJsonResponse(CreateHttpConn(host, path))


def _GetReAuthContext(
    host: str, change: Union[str, int], project: Optional[str] = None
) -> auth.ReAuthContext:
    """Returns a ReAuthContext for `change`, fetching `project` if omitted."""
    return auth.ReAuthContext(
        host=host,
        project=project or GetChangeDetail(host, change)["project"],
    )


def GetChangeCommit(host: str, change: str, revision: str = "current") -> dict:
    """Query a Gerrit server for a revision associated with a change."""
    path = "changes/%s/revisions/%s/commit?links" % (change, revision)
    return ReadHttpJsonResponse(CreateHttpConn(host, path))


def GetChangeCurrentRevision(host, change):
    """Get information about the latest revision for a given change."""
    return QueryChanges(host, [], change, o_params=("CURRENT_REVISION",))


def GetChangeRevisions(host, change):
    """Gets information about all revisions associated with a change."""
    return QueryChanges(host, [], change, o_params=("ALL_REVISIONS",))


def GetChangeReview(host, change, revision=None):
    """Gets the current review information for a change."""
    if not revision:
        jmsg = GetChangeRevisions(host, change)
        if not jmsg:
            return None

        if len(jmsg) > 1:
            raise GerritError(
                200, "Multiple changes found for ChangeId %s." % change
            )
        revision = jmsg[0]["current_revision"]
    path = "changes/%s/revisions/%s/review"
    return ReadHttpJsonResponse(CreateHttpConn(host, path))


def GetChangeComments(host, change):
    """Get the line- and file-level comments on a change."""
    path = "changes/%s/comments" % change
    return ReadHttpJsonResponse(CreateHttpConn(host, path))


def GetChangeDrafts(host, change):
    """Get the draft comments on a change across all revisions.

    https://gerrit-review.googlesource.com/Documentation/rest-api-changes.html#list-change-drafts
    """
    path = "changes/%s/drafts" % change
    return ReadHttpJsonResponse(CreateHttpConn(host, path))


def GetChangeChecks(host, change, revision="current"):
    """Get automated findings from the Gerrit Findings plugin."""
    path = "changes/%s/revisions/%s/list_checks" % (change, revision)
    return ReadHttpJsonResponse(CreateHttpConn(host, path))


def CreateDraft(host, change, revision="current", body=None):
    """Creates a draft comment on a revision.

    https://gerrit-review.googlesource.com/Documentation/rest-api-changes.html#create-draft
    """
    if isinstance(body, dict) and gclient_utils.IsEnvAi():
        body.setdefault("is_ai", True)
    path = f"changes/{change}/revisions/{revision}/drafts"
    conn = CreateHttpConn(host, path, reqtype="PUT", body=body)
    return ReadHttpJsonResponse(conn, accept_statuses=(200, 201))


def GetRelatedChanges(host, change, revision="current"):
    """Gets the related changes for a given change and revision."""
    path = "changes/%s/revisions/%s/related" % (change, revision)
    return ReadHttpJsonResponse(CreateHttpConn(host, path))


def AbandonChange(host, change, msg=""):
    """Abandons a Gerrit change."""
    path = "changes/%s/abandon" % change
    body = {"message": msg} if msg else {}
    conn = CreateHttpConn(host, path, reqtype="POST", body=body)
    return ReadHttpJsonResponse(conn, accept_statuses=(200, 204, 409))


def MoveChange(host, change, destination_branch):
    """Move a Gerrit change to different destination branch."""
    path = "changes/%s/move" % change
    body = {"destination_branch": destination_branch, "keep_all_votes": True}
    conn = CreateHttpConn(host, path, reqtype="POST", body=body)
    return ReadHttpJsonResponse(conn)


def RestoreChange(host, change, msg=""):
    """Restores a previously abandoned change."""
    path = "changes/%s/restore" % change
    body = {"message": msg} if msg else {}
    conn = CreateHttpConn(host, path, reqtype="POST", body=body)
    return ReadHttpJsonResponse(conn)


def RebaseChange(host, change, base=None, project: Optional[str] = None):
    """Rebases a change."""
    path = f"changes/{change}/rebase"
    body = {"base": base} if base else {}
    reauth_context = _GetReAuthContext(host, change, project)
    conn = CreateHttpConn(
        host, path, reqtype="POST", body=body, reauth_context=reauth_context
    )
    # If a rebase fails due to a merge conflict, Gerrit returns 409. Retrying
    # more than once probably won't help since the merge conflict will still
    # exist.
    return ReadHttpJsonResponse(conn, max_tries=2)


def SubmitChange(host, change):
    """Submits a Gerrit change via Gerrit."""
    path = "changes/%s/submit" % change
    conn = CreateHttpConn(host, path, reqtype="POST")
    # If a submit fails due to a merge conflict, Gerrit returns 409. Retrying
    # more than once probably won't help since the merge conflict will still
    # exist.
    return ReadHttpJsonResponse(conn, max_tries=2)


def GetChangesSubmittedTogether(host, change):
    """Get all changes submitted with the given one."""
    path = "changes/%s/submitted_together?o=NON_VISIBLE_CHANGES" % change
    conn = CreateHttpConn(host, path, reqtype="GET")
    return ReadHttpJsonResponse(conn)


def PublishChangeEdit(host, change, notify=True, project: Optional[str] = None):
    """Publish a Gerrit change edit."""
    path = "changes/%s/edit:publish" % change
    body = {"notify": "ALL" if notify else "NONE"}
    reauth_context = _GetReAuthContext(host, change, project)
    conn = CreateHttpConn(
        host, path, reqtype="POST", body=body, reauth_context=reauth_context
    )
    return ReadHttpJsonResponse(conn, accept_statuses=(204,))


def ChangeEdit(host, change, path, data):
    """Puts content of a file into a change edit."""
    path = "changes/%s/edit/%s" % (change, urllib.parse.quote(path, ""))
    body = {
        "binary_content": "data:text/plain;base64,%s"
        % base64.b64encode(data.encode("utf-8")).decode("utf-8")
    }
    conn = CreateHttpConn(host, path, reqtype="PUT", body=body)
    return ReadHttpJsonResponse(conn, accept_statuses=(204, 409))


def SetChangeEditMessage(host, change, message):
    """Sets the commit message of a change edit."""
    path = "changes/%s/edit:message" % change
    body = {"message": message}
    conn = CreateHttpConn(host, path, reqtype="PUT", body=body)
    return ReadHttpJsonResponse(conn, accept_statuses=(204, 409))


def HasPendingChangeEdit(host, change):
    conn = CreateHttpConn(host, "changes/%s/edit" % change)
    try:
        ReadHttpResponse(conn)
    except GerritError as e:
        # 204 No Content means no pending change.
        if e.http_status == 204:
            return False
        raise
    return True


def DeletePendingChangeEdit(host, change):
    conn = CreateHttpConn(host, "changes/%s/edit" % change, reqtype="DELETE")
    # On success, Gerrit returns status 204; if the edit was already deleted it
    # returns 404.  Anything else is an error.
    ReadHttpResponse(conn, accept_statuses=[204, 404])


def CherryPick(
    host,
    change,
    destination,
    revision="current",
    message=None,
    base=None,
    allow_conflicts=False,
    project: Optional[str] = None,
):
    """Create a cherry-pick commit from the given change, onto the given
    destination.
    """
    path = "changes/%s/revisions/%s/cherrypick" % (change, revision)
    body = {"destination": destination}
    if message:
        body["message"] = message
    if base:
        body["base"] = base
    if allow_conflicts:
        body["allow_conflicts"] = True
    reauth_context = _GetReAuthContext(host, change, project)
    conn = CreateHttpConn(
        host, path, reqtype="POST", body=body, reauth_context=reauth_context
    )

    # If a cherry pick fails due to a merge conflict, Gerrit returns 409.
    # Retrying more than once probably won't help since the merge conflict will
    # still exist.
    return ReadHttpJsonResponse(conn, max_tries=2)


def CherryPickCommit(host, project, commit, destination):
    """Cherry-pick a commit from a project to a destination branch."""
    reauth_context = auth.ReAuthContext(host=host, project=project)
    project = urllib.parse.quote(project, "")
    path = f"projects/{project}/commits/{commit}/cherrypick"
    body = {"destination": destination}
    conn = CreateHttpConn(
        host, path, reqtype="POST", body=body, reauth_context=reauth_context
    )
    return ReadHttpJsonResponse(conn)


def GetPatch(
    host: str,
    change: str,
    revision: str = "current",
    path: Optional[str] = None,
    parent: Optional[int] = None,
    context: Optional[int] = None,
) -> bytes:
    """Get a formatted patch for a change revision."""
    endpoint = f"changes/{change}/revisions/{revision}/patch"
    params = []
    if path is not None:
        params.append(("path", path))
    if parent is not None:
        params.append(("parent", parent))
    if context is not None:
        params.append(("context", context))
    if params:
        endpoint += "?" + urllib.parse.urlencode(params)

    conn = CreateHttpConn(host, endpoint, reqtype="GET")
    return base64.b64decode(ReadHttpResponse(conn).read())


def GetFileContents(host, change, path, revision="current"):
    """Get the contents of a file with the given path in the given revision.

    Returns:
        A bytes object with the file's contents.
    """
    path = "changes/%s/revisions/%s/files/%s/content" % (
        change,
        revision,
        urllib.parse.quote(path, ""),
    )
    conn = CreateHttpConn(host, path, reqtype="GET")
    return base64.b64decode(ReadHttpResponse(conn).read())


def SetCommitMessage(
    host, change, description, notify="ALL", project: Optional[str] = None
):
    """Updates a commit message."""
    assert notify in ("ALL", "NONE")
    path = "changes/%s/message" % change
    body = {"message": description, "notify": notify}
    reauth_context = _GetReAuthContext(host, change, project)
    conn = CreateHttpConn(
        host, path, reqtype="PUT", body=body, reauth_context=reauth_context
    )
    try:
        ReadHttpResponse(conn, accept_statuses=[200, 204])
    except GerritError as e:
        raise GerritError(
            e.http_status,
            "Received unexpected http status while editing message "
            "in change %s" % change,
        )


def GetCommitIncludedIn(host, project, commit):
    """Retrieves the branches and tags for a given commit.

    https://gerrit-review.googlesource.com/Documentation/rest-api-projects.html#get-included-in

    Returns:
        A JSON object with keys of 'branches' and 'tags'.
    """
    path = "projects/%s/commits/%s/in" % (
        urllib.parse.quote(project, ""),
        commit,
    )
    conn = CreateHttpConn(host, path, reqtype="GET")
    return ReadHttpJsonResponse(conn, accept_statuses=[200])


def IsCodeOwnersEnabledOnHost(host):
    """Check if the code-owners plugin is enabled for the host."""
    path = "config/server/capabilities"
    capabilities = ReadHttpJsonResponse(CreateHttpConn(host, path))
    return "code-owners-checkCodeOwner" in capabilities


def IsCodeOwnersEnabledOnRepo(host, repo):
    """Check if the code-owners plugin is enabled for the repo."""
    repo = PercentEncodeForGitRef(repo)
    path = "/projects/%s/code_owners.project_config" % repo
    config = ReadHttpJsonResponse(CreateHttpConn(host, path))
    return not config["status"].get("disabled", False)


def GetOwnersForFile(
    host,
    project,
    branch,
    path,
    limit=100,
    resolve_all_users=True,
    highest_score_only=False,
    seed=None,
    o_params=("DETAILS",),
):
    """Gets information about owners attached to a file."""
    path = "projects/%s/branches/%s/code_owners/%s" % (
        urllib.parse.quote(project, ""),
        urllib.parse.quote(branch, ""),
        urllib.parse.quote(path, ""),
    )
    q = ["resolve-all-users=%s" % json.dumps(resolve_all_users)]
    if highest_score_only:
        q.append("highest-score-only=%s" % json.dumps(highest_score_only))
    if seed:
        q.append("seed=%d" % seed)
    if limit:
        q.append("n=%d" % limit)
    if o_params:
        q.extend(["o=%s" % p for p in o_params])
    if q:
        path = "%s?%s" % (path, "&".join(q))
    return ReadHttpJsonResponse(CreateHttpConn(host, path))


def GetReviewers(host, change):
    """Gets information about all reviewers attached to a change."""
    path = "changes/%s/reviewers" % change
    return ReadHttpJsonResponse(CreateHttpConn(host, path))


def GetReview(host, change, revision):
    """Gets review information about a specific revision of a change."""
    path = "changes/%s/revisions/%s/review" % (change, revision)
    return ReadHttpJsonResponse(CreateHttpConn(host, path))


def AddReviewers(
    host,
    change,
    reviewers=None,
    ccs=None,
    notify=True,
    revision="current",
    accept_statuses: Container[int] = frozenset([200, 400, 422]),
):
    """Add reviewers to a change."""
    if not reviewers and not ccs:
        return None
    if not change:
        return None
    reviewers = frozenset(reviewers or [])
    ccs = frozenset(ccs or [])
    path = "changes/%s/revisions/%s/review" % (change, revision)

    body = {
        "drafts": "KEEP",
        "reviewers": [],
        "notify": "ALL" if notify else "NONE",
    }
    for r in sorted(reviewers | ccs):
        state = "REVIEWER" if r in reviewers else "CC"
        body["reviewers"].append(
            {
                "reviewer": r,
                "state": state,
                "notify": "NONE",  # We handled `notify` argument above.
            }
        )

    conn = CreateHttpConn(host, path, reqtype="POST", body=body)
    # Gerrit will return 400 if one or more of the requested reviewers are
    # unprocessable. We read the response object to see which were rejected,
    # warn about them, and retry with the remainder.
    resp = ReadHttpJsonResponse(conn, accept_statuses=accept_statuses)

    errored = set()
    for result in resp.get("reviewers", {}).values():
        r = result.get("input")
        state = "REVIEWER" if r in reviewers else "CC"
        if result.get("error"):
            errored.add(r)
            LOGGER.warning('Note: "%s" not added as a %s' % (r, state.lower()))
    if errored:
        # Try again, adding only those that didn't fail, and only accepting 200.
        AddReviewers(
            host,
            change,
            reviewers=(reviewers - errored),
            ccs=(ccs - errored),
            notify=notify,
            revision=revision,
            accept_statuses=[200],
        )


# Gerrit labels that are subject to review enforcement.
_REVIEW_ENFORCEMENT_LABELS = set(x.lower() for x in ["Code-Review"])


def _contains_review_enforcement_label(
    labels: Optional[Dict[str, str]],
) -> bool:
    """Returns if `labels` are subject to review enforcement."""
    return bool(labels) and any(
        label.lower() in _REVIEW_ENFORCEMENT_LABELS for label in labels
    )


def SetReview(
    host,
    change,
    revision="current",
    msg=None,
    labels=None,
    notify=None,
    ready=None,
    automatic_attention_set_update: Optional[bool] = None,
    project: Optional[str] = None,
    comments: Optional[Dict[str, Any]] = None,
):
    """Sets labels and/or adds a message to a code review.

    https://gerrit-review.googlesource.com/Documentation/rest-api-changes.html#set-review

    We need to check if this change's `project` needs to satisfy ReAuth
    requirements, and attach an ReAuth credential to the request. `project`
    can be optionally be provided to save one Gerrit server round-trip.
    """
    if not any([msg, labels, comments]):
        return
    path = f"changes/{change}/revisions/{revision}/review"
    body: Dict[str, Any] = {"drafts": "KEEP"}
    if msg:
        body["message"] = msg
    if labels:
        body["labels"] = labels
    if comments:
        if gclient_utils.IsEnvAi():
            _SetAiReviewComments(comments)
        body["comments"] = comments
    if notify is not None:
        body["notify"] = "ALL" if notify else "NONE"
    if ready:
        body["ready"] = True
    if automatic_attention_set_update is not None:
        body[
            "ignore_automatic_attention_set_rules"
        ] = not automatic_attention_set_update

    # ReAuth needed if we set labels (notably Code-Review).
    reauth_context = None
    if _contains_review_enforcement_label(labels):
        reauth_context = _GetReAuthContext(host, change, project)

    conn = CreateHttpConn(
        host, path, reqtype="POST", body=body, reauth_context=reauth_context
    )
    response = ReadHttpJsonResponse(conn)
    if labels:
        for key, val in labels.items():
            if (
                "labels" not in response
                or key not in response["labels"]
                or int(response["labels"][key] != int(val))
            ):
                raise GerritError(
                    200,
                    'Unable to set "%s" label on change %s.' % (key, change),
                )
    return response


def ResetReviewLabels(
    host, change, label, value="0", message=None, notify=None
):
    """Resets the value of a given label for all reviewers on a change."""
    # This is tricky, because we want to work on the "current revision", but
    # there's always the risk that "current revision" will change in between
    # API calls.  So, we check "current revision" at the beginning and end; if
    # it has changed, raise an exception.
    jmsg = GetChangeCurrentRevision(host, change)
    if not jmsg:
        raise GerritError(
            200, 'Could not get review information for change "%s"' % change
        )
    value = str(value)
    revision = jmsg[0]["current_revision"]
    path = "changes/%s/revisions/%s/review" % (change, revision)
    message = message or (
        "%s label set to %s programmatically." % (label, value)
    )
    jmsg = GetReview(host, change, revision)
    if not jmsg:
        raise GerritError(
            200,
            "Could not get review information for revision %s "
            "of change %s" % (revision, change),
        )
    for review in jmsg.get("labels", {}).get(label, {}).get("all", []):
        if str(review.get("value", value)) != value:
            body = {
                "drafts": "KEEP",
                "message": message,
                "labels": {label: value},
                "on_behalf_of": review["_account_id"],
            }
            if notify:
                body["notify"] = notify
            conn = CreateHttpConn(host, path, reqtype="POST", body=body)
            response = ReadHttpJsonResponse(conn)
            if str(response["labels"][label]) != value:
                username = review.get("email", jmsg.get("name", ""))
                raise GerritError(
                    200,
                    'Unable to set %s label for user "%s"'
                    " on change %s." % (label, username, change),
                )
    jmsg = GetChangeCurrentRevision(host, change)
    if not jmsg:
        raise GerritError(
            200, 'Could not get review information for change "%s"' % change
        )

    if jmsg[0]["current_revision"] != revision:
        raise GerritError(
            200,
            'While resetting labels on change "%s", '
            "a new patchset was uploaded." % change,
        )


def CreateChange(host, project, branch="main", subject="", params=()):
    """
    Creates a new change.

    Args:
        params: A list of additional ChangeInput specifiers, as documented here:
            (e.g. ('is_private', 'true') to mark the change private.
            https://gerrit-review.googlesource.com/Documentation/rest-api-changes.html#change-input

    Returns:
        ChangeInfo for the new change.
    """
    path = "changes/"
    body = {"project": project, "branch": branch, "subject": subject}
    body.update(dict(params))
    for key in "project", "branch", "subject":
        if not body[key]:
            raise GerritError(200, "%s is required" % key.title())

    reauth_context = auth.ReAuthContext(host=host, project=body["project"])
    conn = CreateHttpConn(
        host, path, reqtype="POST", body=body, reauth_context=reauth_context
    )
    return ReadHttpJsonResponse(conn, accept_statuses=[201])


def CreateGerritBranch(host, project, branch, commit):
    """Creates a new branch from given project and commit

    https://gerrit-review.googlesource.com/Documentation/rest-api-projects.html#create-branch

    Returns:
        A JSON object with 'ref' key.
    """
    path = "projects/%s/branches/%s" % (project, branch)
    body = {"revision": commit}
    conn = CreateHttpConn(host, path, reqtype="PUT", body=body)
    response = ReadHttpJsonResponse(conn, accept_statuses=[201, 409])
    if response:
        return response
    raise GerritError(200, "Unable to create gerrit branch")


def CreateGerritTag(host, project, tag, commit):
    """Creates a new tag at the given commit.

    https://gerrit-review.googlesource.com/Documentation/rest-api-projects.html#create-tag

    Returns:
        A JSON object with 'ref' key.
    """
    path = "projects/%s/tags/%s" % (project, tag)
    body = {"revision": commit}
    conn = CreateHttpConn(host, path, reqtype="PUT", body=body)
    response = ReadHttpJsonResponse(conn, accept_statuses=[201, 409])
    if response:
        return response
    raise GerritError(200, "Unable to create gerrit tag")


def GetHead(host, project):
    """Retrieves current HEAD of Gerrit project

    https://gerrit-review.googlesource.com/Documentation/rest-api-projects.html#get-head

    Returns:
        A JSON object with 'ref' key.
    """
    path = "projects/%s/HEAD" % (project)
    conn = CreateHttpConn(host, path, reqtype="GET")
    response = ReadHttpJsonResponse(conn, accept_statuses=[200])
    if response:
        return response
    raise GerritError(200, "Unable to update gerrit HEAD")


def UpdateHead(host, project, branch):
    """Updates Gerrit HEAD to point to branch

    https://gerrit-review.googlesource.com/Documentation/rest-api-projects.html#set-head

    Returns:
        A JSON object with 'ref' key.
    """
    path = "projects/%s/HEAD" % (project)
    body = {"ref": branch}
    conn = CreateHttpConn(host, path, reqtype="PUT", body=body)
    response = ReadHttpJsonResponse(conn, accept_statuses=[200])
    if response:
        return response
    raise GerritError(200, "Unable to update gerrit HEAD")


def GetGerritBranch(host, project, branch):
    """Gets a branch info from given project and branch name.

    See:
    https://gerrit-review.googlesource.com/Documentation/rest-api-projects.html#get-branch

    Returns:
        A JSON object with 'revision' key if the branch exists, otherwise None.
    """
    path = "projects/%s/branches/%s" % (project, branch)
    conn = CreateHttpConn(host, path, reqtype="GET")
    return ReadHttpJsonResponse(conn, accept_statuses=[200, 404])


def GetGerritTag(host, project, tag):
    """Gets a tag info from given project and tag name.

    See:
    https://gerrit-review.googlesource.com/Documentation/rest-api-projects.html#get-tag

    Returns:
        A JSON object with 'revision' key if the tag exists, otherwise None.
    """
    path = f"projects/{project}/tags/{tag}"
    conn = CreateHttpConn(host, path, reqtype="GET")
    return ReadHttpJsonResponse(conn, accept_statuses=[200, 404])


def GetProjectHead(host, project):
    conn = CreateHttpConn(
        host, "/projects/%s/HEAD" % urllib.parse.quote(project, "")
    )
    return ReadHttpJsonResponse(conn, accept_statuses=[200])


def GetAccountDetails(
    host, account_id="self", *, authenticator: Optional[_Authenticator] = None
):
    """Returns details of the account.

    If account_id is not given, uses magic value 'self' which corresponds to
    whichever account user is authenticating as.

    Documentation:
    https://gerrit-review.googlesource.com/Documentation/rest-api-accounts.html#get-account

    Returns None if account is not found (i.e., Gerrit returned 404).
    """
    conn = CreateHttpConn(
        host, "/accounts/%s" % account_id, authenticator=authenticator
    )
    return ReadHttpJsonResponse(conn, accept_statuses=[200, 404])


_HOST_HAS_ACCOUNT_CONFIG_KEY = "depot-tools.hosthasaccount"


def _NormalizeHostAndEmail(
    host: str, email: Optional[str]
) -> Tuple[str, Optional[str]]:
    """Normalizes host and email to lowercase stripped strings for caching."""
    if "://" not in host:
        host = "https://" + host
    parsed = urllib.parse.urlparse(host)
    norm_host = (parsed.netloc or parsed.path).rstrip("/").lower()
    norm_email = email.strip().lower() if email else None
    return norm_host, norm_email


def InvalidateAccountCache(
    host: str, email: Optional[str] = None, *, cwd: Optional[str] = None
) -> None:
    """Evicts cached account existence entry for host/email from Git config."""
    cwd = cwd or _GetGitConfigDir()
    try:
        hosts = scm.GIT.GetConfigList(cwd, _HOST_HAS_ACCOUNT_CONFIG_KEY)
        if not hosts:
            return
        if not email:
            try:
                email = scm.GIT.GetConfig(cwd, "user.email")
            except Exception:
                pass

        norm_host, norm_email = _NormalizeHostAndEmail(host, email)
        target = f"{norm_host}:{norm_email}" if norm_email else norm_host
        new_hosts = [
            h
            for h in hosts
            if h.strip().lower() != target and h.strip().lower() != norm_host
        ]
        if len(new_hosts) == len(hosts):
            return

        scm.GIT.SetConfig(
            cwd, _HOST_HAS_ACCOUNT_CONFIG_KEY, None, modify_all=True
        )
        for h in new_hosts:
            scm.GIT.SetConfig(cwd, _HOST_HAS_ACCOUNT_CONFIG_KEY, h, append=True)
    except Exception as e:
        LOGGER.debug("Could not invalidate account cache: %s", e)


@dataclass(frozen=True)
class _AccountCacheInfo:
    cwd: str
    norm_host: str
    norm_email: Optional[str]
    cache_entry: str
    is_cached: bool


def _GetAccountCacheInfo(
    host: str,
    email: Optional[str] = None,
    *,
    cwd: Optional[str] = None,
) -> _AccountCacheInfo:
    """Resolves normalized host/email, cache entry key, and checks cache existence."""
    cwd = cwd or _GetGitConfigDir()
    if not email:
        try:
            email = scm.GIT.GetConfig(cwd, "user.email")
        except Exception:
            pass

    norm_host, norm_email = _NormalizeHostAndEmail(host, email)
    cache_entry = f"{norm_host}:{norm_email}" if norm_email else norm_host
    is_cached = False
    try:
        hosts = scm.GIT.GetConfigList(cwd, _HOST_HAS_ACCOUNT_CONFIG_KEY)
        norm_hosts = [h.strip().lower() for h in hosts]
        is_cached = cache_entry in norm_hosts or norm_host in norm_hosts
    except Exception:
        pass

    return _AccountCacheInfo(
        cwd=cwd,
        norm_host=norm_host,
        norm_email=norm_email,
        cache_entry=cache_entry,
        is_cached=is_cached,
    )


def EnsureAccountExists(
    host: str,
    *,
    cwd: Optional[str] = None,
    force: bool = False,
    authenticator: Optional[_Authenticator] = None,
) -> None:
    """Verifies that the user has an active account on the given Gerrit host.

    Caches positive results in the local Git config under 'depot-tools.hosthasaccount'
    (keyed by 'host:email') to avoid redundant ~500ms network RPCs on subsequent
    operations while safely supporting multiple accounts.

    Raises:
        GerritError: If the account does not exist (400/404) or on HTTP failure.
        auth.GitLoginRequiredError: If user is not authenticated.
    """
    cache_info = _GetAccountCacheInfo(host, cwd=cwd)
    if not force and cache_info.is_cached:
        LOGGER.debug(
            "Using cached account existence for Gerrit host %r",
            cache_info.cache_entry,
        )
        return

    try:
        info = GetAccountDetails(
            cache_info.norm_host, authenticator=authenticator
        )
    except GerritError as e:
        if e.http_status == 400:
            InvalidateAccountCache(
                cache_info.norm_host, cache_info.norm_email, cwd=cache_info.cwd
            )
            raise GerritError(
                400,
                f"Account does not exist on Gerrit host '{cache_info.norm_host}'. "
                f"Please log into https://{cache_info.norm_host} in your browser first to activate your account.",
            ) from e
        raise

    if not info or ("email" not in info and "_account_id" not in info):
        InvalidateAccountCache(
            cache_info.norm_host, cache_info.norm_email, cwd=cache_info.cwd
        )
        raise GerritError(
            404,
            f"Account not found on Gerrit host '{cache_info.norm_host}'. "
            f"Please log into https://{cache_info.norm_host} in your browser first to activate your account.",
        )

    LOGGER.debug("Gerrit account exists on %r", cache_info.cache_entry)
    try:
        hosts = scm.GIT.GetConfigList(
            cache_info.cwd, _HOST_HAS_ACCOUNT_CONFIG_KEY
        )
        norm_hosts = [h.strip().lower() for h in hosts]
        if cache_info.cache_entry not in norm_hosts:
            scm.GIT.SetConfig(
                cache_info.cwd,
                _HOST_HAS_ACCOUNT_CONFIG_KEY,
                cache_info.cache_entry,
                append=True,
            )
    except (subprocess2.CalledProcessError, OSError, Exception) as e:
        LOGGER.debug(
            "Could not cache '%s': %s", _HOST_HAS_ACCOUNT_CONFIG_KEY, e
        )


def AsyncEnsureAccountExists(
    host: str,
    *,
    cwd: Optional[str] = None,
    force: bool = False,
    authenticator: Optional[_Authenticator] = None,
) -> Callable[[], None]:
    """Starts account existence verification in a background worker thread.

    Returns a callable that blocks until verification finishes and raises any
    exception encountered during verification.
    """
    cache_info = _GetAccountCacheInfo(host, cwd=cwd)
    if not force and cache_info.is_cached:
        return lambda: None

    result_queue: queue.Queue = queue.Queue(maxsize=1)

    def _worker():
        try:
            EnsureAccountExists(
                cache_info.norm_host,
                cwd=cache_info.cwd,
                force=True,
                authenticator=authenticator,
            )
            result_queue.put(None)
        except BaseException as e:
            result_queue.put(e)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    lock = threading.Lock()
    joined = False
    exc: Optional[BaseException] = None

    def _join():
        nonlocal joined, exc
        with lock:
            if not joined:
                thread.join()
                exc = result_queue.get()
                joined = True
        if exc:
            raise exc

    return _join


class EmailRecord(TypedDict):
    email: str
    preferred: bool  # This should be NotRequired[bool] in 3.11+


def GetAccountEmails(
    host, account_id="self", *, authenticator: Optional[_Authenticator] = None
) -> Optional[List[EmailRecord]]:
    """Returns all emails for this account, and an indication of which of these
    is preferred.

    If account_id is not given, uses magic value 'self' which corresponds to
    whichever account user is authenticating as.

    Requires Modify Account permission to view emails other than 'self'.

    Documentation:
    https://gerrit-review.googlesource.com/Documentation/rest-api-accounts.html#list-account-emails

    Returns None if account is not found (i.e. Gerrit returned 404).
    """
    conn = CreateHttpConn(
        host, "/accounts/%s/emails" % account_id, authenticator=authenticator
    )
    resp = ReadHttpJsonResponse(conn, accept_statuses=[200, 404])
    if resp is None:
        return None
    return cast(List[EmailRecord], resp)


def ValidAccounts(host, accounts, max_threads=10):
    """Returns a mapping from valid account to its details.

    Invalid accounts, either not existing or without unique match,
    are not present as returned dictionary keys.
    """
    assert not isinstance(accounts, str), type(accounts)
    accounts = list(set(accounts))
    if not accounts:
        return {}

    def get_one(account):
        try:
            return account, GetAccountDetails(host, account)
        except GerritError:
            return None, None

    valid = {}
    with contextlib.closing(
        ThreadPool(min(max_threads, len(accounts)))
    ) as pool:
        for account, details in pool.map(get_one, accounts):
            if account and details:
                valid[account] = details
    return valid


def PercentEncodeForGitRef(original):
    """Applies percent-encoding for strings sent to Gerrit via git ref metadata.

    The encoding used is based on but stricter than URL encoding (Section 2.1 of
    RFC 3986). The only non-escaped characters are alphanumerics, and 'SPACE'
    (U+0020) can be represented as 'LOW LINE' (U+005F) or 'PLUS SIGN' (U+002B).

    For more information, see the Gerrit docs here:

    https://gerrit-review.googlesource.com/Documentation/user-upload.html#message
    """
    safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "
    encoded = "".join(c if c in safe else "%%%02X" % ord(c) for c in original)

    # Spaces are not allowed in git refs; gerrit will interpret either '_' or
    # '+' (or '%20') as space. Use '_' since that has been supported the
    # longest.
    return encoded.replace(" ", "_")


@contextlib.contextmanager
def tempdir():
    tdir = None
    try:
        tdir = tempfile.mkdtemp(suffix="gerrit_util")
        yield tdir
    finally:
        if tdir:
            gclient_utils.rmtree(tdir)


def ChangeIdentifier(project, change_number):
    """Returns change identifier "project~number" suitable for |change| arg of
    this module API.

    Such format is allows for more efficient Gerrit routing of HTTP requests,
    comparing to specifying just change_number.
    """
    assert int(change_number)
    return "%s~%s" % (urllib.parse.quote(project, ""), change_number)


def ComputeScheduleTime(
    issue: Optional[Union[int, str]] = None,
    custom_time: Optional[str] = None,
) -> str:
    """Computes target time string for submission flow.

    If custom_time is provided, validates that it is in 24-hour "HH:MM" format
    in PT / MTV time (e.g. "19:45"). Otherwise, computes a deterministic time
    between 19:00 - 23:00 PT / MTV time based on the issue number.
    """
    if custom_time is not None:
        if not isinstance(custom_time, str):
            raise ValueError(
                f'Invalid time "{custom_time}". Expected 24-hour format '
                '"HH:MM" in PT / MTV time (e.g. "19:45").'
            )
        custom_time = custom_time.strip()
        # Validate 24-hour HH:MM format (00:00 to 23:59)
        if not re.match(r"^([01]?[0-9]|2[0-3]):[0-5][0-9]$", custom_time):
            raise ValueError(
                f'Invalid time "{custom_time}". Expected 24-hour format '
                '"HH:MM" in PT / MTV time (e.g. "19:45").'
            )
        return custom_time

    # Deterministic jitter across 240-minute window (19:00 to 23:00)
    issue_num = int(issue) if issue else 0
    offset_minutes = issue_num % 240
    target_hour = 19 + (offset_minutes // 60)
    target_min = offset_minutes % 60
    return f"{target_hour:02d}:{target_min:02d}"


def IsFlowsEnabled(host: str, issue: Union[int, str]) -> bool:
    """Checks if Gerrit Flows are enabled for the current user on the change."""
    resp = CallGerritApi(
        host,
        f"changes/{issue}/is-flows-enabled",
        reqtype="GET",
        accept_statuses=(200,),
    )
    return bool(isinstance(resp, dict) and resp.get("enabled"))


def CreateScheduledSubmitFlow(
    host: str,
    issue: Union[int, str],
    custom_time: Optional[str] = None,
    tz: str = "America/Los_Angeles",
) -> Optional[Dict[str, Any]]:
    """Creates a Gerrit Flow to trigger CQ+2 at scheduled time once
    submittable.
    """
    target_time = ComputeScheduleTime(issue, custom_time)
    if not IsFlowsEnabled(host, issue):
        print(
            "Scheduled submits are not currently supported for external "
            "Chromium contributors."
        )
        return None

    cl_url = f"https://{host}/c/{issue}"
    condition = (
        f"{cl_url} is is:submittable, wait until today at {target_time} in {tz}"
    )
    body = {
        "stage_expressions": [
            {
                "condition": condition,
                "action": {
                    "name": "vote",
                    "parameters": ["Commit-Queue+2"],
                },
            }
        ]
    }
    try:
        resp = CallGerritApi(
            host,
            f"changes/{issue}/flows",
            reqtype="POST",
            body=body,
            accept_statuses=(200, 201),
        )
        print(
            f"Created submit flow for CL {issue} "
            f"(scheduled for today at {target_time} in {tz})."
        )
        return resp
    except Exception as e:
        print(f"WARNING: Failed to create submit flow for CL {issue}: {e}")
        raise
