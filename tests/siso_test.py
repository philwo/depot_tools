#!/usr/bin/env vpython3
# Copyright 2024 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import io
import os
import shlex
import sys
import json
import pytest
import subprocess
import itertools
from unittest.mock import DEFAULT
from pathlib import Path
from typing import Any, Dict, List, Tuple, Generator, Optional

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
import siso  # noqa: E402
import build_telemetry  # noqa: E402
import gclient_paths  # noqa: E402


# The functions are annotated with lru and the state is preserved between tests.
@pytest.fixture(autouse=True)
def clear_gclient_paths_caches():
    gclient_paths.FindGclientRoot.cache_clear()
    gclient_paths._GetPrimarySolutionPathInternal.cache_clear()
    gclient_paths._GetBuildtoolsPathInternal.cache_clear()
    gclient_paths._GetGClientSolutions.cache_clear()


@pytest.fixture(autouse=True)
def default_siso_mocks(mocker):
    # Mock GCE/Cloudtop by default to prevent general tests from disabling trace
    # on Windows. Explicitly overridden to False in physical gWindows tests.
    mocker.patch("siso._is_gce", return_value=True)


def _get_siso_config_dir(tmp_path: Path) -> Path:
    return tmp_path / "src" / "build" / "config" / "siso"


def _get_sisoenv_path(tmp_path: Path) -> Path:
    return _get_siso_config_dir(tmp_path) / ".sisoenv"


def _get_siso_bin_dir(tmp_path: Path) -> Path:
    return tmp_path / "src" / "third_party" / "siso" / "cipd"


def _get_siso_bin_path(tmp_path: Path) -> Path:
    return _get_siso_bin_dir(tmp_path) / ("siso" + gclient_paths.GetExeSuffix())


def create_telemetry_cfg(
    tmp_path: Path, mocker: Any, enabled: bool = True
) -> build_telemetry.Config:
    config_path = tmp_path / "build_telemetry.cfg"
    status = "opt-in" if enabled else "opt-out"
    config_data = {
        "user": "test@google.com",
        "status": status,
        "countdown": 0,
        "version": 1,
    }
    config_path.write_text(json.dumps(config_data))

    mocker.patch("build_telemetry.shutil.which", return_value="/bin/gcert")

    config = build_telemetry.Config(str(config_path), 0)
    mocker.patch("build_telemetry.load_config", return_value=config)
    return config


@pytest.fixture
def siso_test_fixture(
    tmp_path: Path, mocker: Any
) -> Generator[None, None, None]:
    # Replace trial dir functionality with tmp_parth.
    previous_dir = os.getcwd()
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    os.chdir(tmp_path / "src")
    mocker.patch("siso.getpass.getuser", return_value="testuser")
    yield
    os.chdir(previous_dir)


@pytest.fixture
def siso_project_setup(
    siso_test_fixture: None, tmp_path: Path, mocker: Any
) -> None:
    sisoenv_dir = _get_siso_config_dir(tmp_path)
    sisoenv_dir.mkdir(parents=True, exist_ok=True)
    (sisoenv_dir / ".sisoenv").write_text("SISO_PROJECT=test-project\n")

    siso_bin_dir = _get_siso_bin_dir(tmp_path)
    siso_bin_dir.mkdir(parents=True, exist_ok=True)
    siso_bin_path = _get_siso_bin_path(tmp_path)
    siso_bin_path.write_text("")
    os.chmod(siso_bin_path, 0o755)

    (tmp_path / ".gclient").write_text("")
    (tmp_path / ".gclient_entries").write_text("entries = {'src': '...'}")
    (tmp_path / "src" / "out" / "Default").mkdir(parents=True, exist_ok=True)

    mocker.patch(
        "siso._get_siso_subcmds", return_value={"ninja", "query", "other"}
    )
    mocker.patch("siso._supports_namespace", return_value=True)
    mocker.patch(
        "siso._handle_collector", side_effect=lambda _p, _a, e, subcmd="": e
    )

    # Create enabled telemetry config by default
    create_telemetry_cfg(tmp_path, mocker, enabled=True)


def test_load_sisorc_no_file(siso_test_fixture: Any) -> None:
    global_flags, subcmd_flags = siso.load_sisorc(
        os.path.join("build", "config", "siso", ".sisorc")
    )
    assert global_flags == []
    assert subcmd_flags == {}


def test_load_sisorc(siso_test_fixture: Any) -> None:
    sisorc = os.path.join("build", "config", "siso", ".sisorc")
    os.makedirs(os.path.dirname(sisorc))
    with open(sisorc, "w") as f:
        f.write("""
# comment
-credential_helper=gcloud
ninja --failure_verbose=false -k=0
        """)
    global_flags, subcmd_flags = siso.load_sisorc(sisorc)
    assert global_flags == ["-credential_helper=gcloud"]
    assert subcmd_flags == {"ninja": ["--failure_verbose=false", "-k=0"]}


@pytest.mark.parametrize(
    "args, subcmds, want",
    [
        pytest.param(
            [
                "-mutexprofile",
                "siso_mutex.prof",
                "ninja",
                "-project",
                "rbe-chrome-untrusted",
                "--enable_cloud_logging",
                "-C",
                "out/Default",
            ],
            {"ninja", "query"},
            (
                ["-mutexprofile", "siso_mutex.prof"],
                "ninja",
                [
                    "-project",
                    "rbe-chrome-untrusted",
                    "--enable_cloud_logging",
                    "-C",
                    "out/Default",
                ],
            ),
            id="complex_global_flags",
        ),
        pytest.param(
            ["ninja", "-C", "out/Default"],
            {"ninja", "query"},
            ([], "ninja", ["-C", "out/Default"]),
            id="simple_ninja",
        ),
        pytest.param(
            ["-v", "1", "ninja"],
            {"ninja", "query"},
            (["-v", "1"], "ninja", []),
            id="flag_with_value_before_subcmd",
        ),
        pytest.param(
            ["--version"],
            {"ninja", "query"},
            (["--version"], "", []),
            id="no_subcmd",
        ),
    ],
)
def test_split_args(
    args: List[str],
    subcmds: set[str],
    want: Tuple[List[str], str, List[str]],
    mocker: Any,
) -> None:
    mocker.patch("siso._get_siso_subcmds", return_value=subcmds)
    got = siso.split_args(args, "siso")
    assert got == want


@pytest.mark.parametrize(
    "args, want",
    [
        pytest.param(
            ["-C", "out/Debug", "ninja"],
            "out/Debug",
            id="C_before_subcmd",
        ),
        pytest.param(
            ["-Cout/Release", "ninja"],
            "out/Release",
            id="C_compact_before_subcmd",
        ),
        pytest.param(
            ["ninja", "-C", "out/Default"],
            "out/Default",
            id="simple_ninja",
        ),
        pytest.param(
            ["ninja", "-Cout/Release"],
            "out/Release",
            id="C_compact_after_subcmd",
        ),
        pytest.param(
            ["--version"],
            ".",
            id="no_outdir",
        ),
    ],
)
def test_fetch_out_dir(args: List[str], want: str) -> None:
    got = siso.fetch_out_dir(args)
    assert got == want


def test_get_siso_subcmds(mocker: Any) -> None:
    mock_run = mocker.patch("siso.subprocess.run")
    mock_run.return_value = mocker.Mock(
        returncode=0,
        stdout="""Usage: siso <flags> <subcommand> <subcommand args>

Subcommands:
        collector        OTEL collector daemon
        ninja            build the requests targets as ninja

Subcommands for auth:
        auth-check       prints current auth status
        login            login to siso system
        logout           logout from siso system
""",
    )
    got = siso._get_siso_subcmds("siso")
    assert got == {"collector", "ninja", "auth-check", "login", "logout"}


@pytest.mark.parametrize(
    "subcmd_args, env, want",
    [
        pytest.param(
            ["-C", "out/Default"],
            {},
            [
                "-C",
                "out/Default",
                "--metrics_labels",
                f"type=developer,tool=siso,host_os={siso._SYSTEM_DICT.get(sys.platform, sys.platform)}",
                "--namespace=developer",
            ],
            id="no_env_flags",
        ),
        pytest.param(
            [
                "-C",
                "out/Default",
                "--enable_cloud_monitoring",
                "--enable_cloud_profiler",
            ],
            {},
            [
                "-C",
                "out/Default",
                "--enable_cloud_monitoring",
                "--enable_cloud_profiler",
                "--metrics_labels",
                f"type=developer,tool=siso,host_os={siso._SYSTEM_DICT.get(sys.platform, sys.platform)}",
                "--namespace=developer",
            ],
            id="some_already_applied_no_env_flags",
        ),
        pytest.param(
            ["-C", "out/Default", "--metrics_project", "some_project"],
            {},
            [
                "-C",
                "out/Default",
                "--metrics_project",
                "some_project",
                "--metrics_labels",
                f"type=developer,tool=siso,host_os={siso._SYSTEM_DICT.get(sys.platform, sys.platform)}",
                "--namespace=developer",
                "--enable_cloud_monitoring",
                "--enable_cloud_profiler",
                "--enable_cloud_trace",
                "--enable_cloud_logging",
            ],
            id="metrics_project_set",
        ),
        pytest.param(
            ["-C", "out/Default"],
            {"RBE_metrics_project": "some_project"},
            [
                "-C",
                "out/Default",
                "--metrics_labels",
                f"type=developer,tool=siso,host_os={siso._SYSTEM_DICT.get(sys.platform, sys.platform)}",
                "--namespace=developer",
                "--enable_cloud_monitoring",
                "--enable_cloud_profiler",
                "--enable_cloud_trace",
                "--enable_cloud_logging",
            ],
            id="metrics_project_set_thru_env",
        ),
        pytest.param(
            ["-C", "out/Default", "--project", "some_project"],
            {},
            [
                "-C",
                "out/Default",
                "--project",
                "some_project",
                "--metrics_labels",
                f"type=developer,tool=siso,host_os={siso._SYSTEM_DICT.get(sys.platform, sys.platform)}",
                "--namespace=developer",
                "--enable_cloud_monitoring",
                "--enable_cloud_profiler",
                "--enable_cloud_trace",
                "--enable_cloud_logging",
                "--metrics_project=some_project",
            ],
            id="cloud_project_set",
        ),
        pytest.param(
            ["-C", "out/Default"],
            {"SISO_PROJECT": "some_project"},
            [
                "-C",
                "out/Default",
                "--metrics_labels",
                f"type=developer,tool=siso,host_os={siso._SYSTEM_DICT.get(sys.platform, sys.platform)}",
                "--namespace=developer",
                "--enable_cloud_monitoring",
                "--enable_cloud_profiler",
                "--enable_cloud_trace",
                "--enable_cloud_logging",
                "--metrics_project=some_project",
            ],
            id="cloud_project_set_thru_env",
        ),
        pytest.param(
            ["-C", "out/Default", "--enable_cloud_profiler=false"],
            {"SISO_PROJECT": "some_project"},
            [
                "-C",
                "out/Default",
                "--enable_cloud_profiler=false",
                "--metrics_labels",
                f"type=developer,tool=siso,host_os={siso._SYSTEM_DICT.get(sys.platform, sys.platform)}",
                "--namespace=developer",
                "--enable_cloud_monitoring",
                "--enable_cloud_trace",
                "--enable_cloud_logging",
                "--metrics_project=some_project",
            ],
            id="respects_set_flags",
        ),
        pytest.param(
            ["--help"],
            {},
            [
                "--help",
                "--metrics_labels",
                f"type=developer,tool=siso,host_os={siso._SYSTEM_DICT.get(sys.platform, sys.platform)}",
                "--namespace=developer",
            ],
            id="help_flag",
        ),
        pytest.param(
            ["-h"],
            {},
            [
                "-h",
                "--metrics_labels",
                f"type=developer,tool=siso,host_os={siso._SYSTEM_DICT.get(sys.platform, sys.platform)}",
                "--namespace=developer",
            ],
            id="short_help_flag",
        ),
        pytest.param(
            ["-C", "out/Default", "--metrics_labels=foo=bar"],
            {},
            [
                "-C",
                "out/Default",
                "--metrics_labels=foo=bar",
                "--namespace=developer",
            ],
            id="labels_exist",
        ),
    ],
)
def test_apply_telemetry_flags(
    subcmd_args: List[str], env: Dict[str, str], want: List[str]
) -> None:
    got = siso.apply_telemetry_flags(subcmd_args, env)
    assert got == want


def test_apply_telemetry_flags_no_namespace() -> None:
    subcmd_args = ["-C", "out/Default"]
    env = {}
    got = siso.apply_telemetry_flags(subcmd_args, env, supports_namespace=False)
    assert not any(arg.startswith("--namespace") for arg in got)


def test_apply_telemetry_flags_sets_expected_env_var(mocker: Any) -> None:
    mocker.patch.dict("os.environ", {})
    subcmd_args = [
        "-C",
        "out/Default",
    ]
    env = {}
    _ = siso.apply_telemetry_flags(subcmd_args, env)
    assert env.get("GOOGLE_API_USE_CLIENT_CERTIFICATE") == "false"


@pytest.mark.parametrize(
    "subcmd_args, env, want",
    [
        pytest.param(
            ["--metrics_project", "proj1"],
            {},
            "proj1",
            id="metrics_project_arg",
        ),
        pytest.param(["--project", "proj2"], {}, "proj2", id="project_arg"),
        pytest.param(
            ["--metrics_project", "proj1", "--project", "proj2"],
            {},
            "proj1",
            id="metrics_project_and_project_args",
        ),
        pytest.param(
            [],
            {"RBE_metrics_project": "proj3"},
            "proj3",
            id="rbe_metrics_project_env",
        ),
        pytest.param(
            [], {"SISO_PROJECT": "proj4"}, "proj4", id="siso_project_env"
        ),
        pytest.param(
            [],
            {"RBE_metrics_project": "proj3", "SISO_PROJECT": "proj4"},
            "proj3",
            id="rbe_and_siso_project_env",
        ),
        pytest.param(
            ["--project", "proj2"],
            {"RBE_metrics_project": "proj3"},
            "proj2",
            id="project_arg_and_rbe_env",
        ),
        pytest.param(
            ["--metrics_project", "proj1"],
            {"RBE_metrics_project": "proj3"},
            "proj1",
            id="metrics_project_arg_and_rbe_env",
        ),
        pytest.param([], {}, "", id="no_project"),
        pytest.param(
            ["-metrics_project", "proj1"],
            {},
            "proj1",
            id="short_metrics_project_arg",
        ),
        pytest.param(
            ["-project", "proj2"], {}, "proj2", id="short_project_arg"
        ),
    ],
)
def test_fetch_metrics_project(
    subcmd_args: List[str], env: Dict[str, str], want: str
) -> None:
    got = siso._fetch_metrics_project(subcmd_args, env)
    assert got == want


@pytest.mark.parametrize(
    "platform, env_vars, want_path_template",
    [
        (
            "Linux",
            {
                "XDG_RUNTIME_DIR": os.path.join(
                    "{root_dir}", "run", "user", "1000"
                )
            },
            os.path.join("{root_dir}", "run", "user", "1000", "{user}", "siso"),
        ),
        ("Linux", {}, os.path.join("/tmp", "{user}", "siso")),
        (
            "Darwin",
            {
                "TMPDIR": os.path.join(
                    "{root_dir}", "var", "folders", "12", "345..."
                ),
            },
            os.path.join(
                "{root_dir}", "var", "folders", "12", "345...", "{user}", "siso"
            ),
        ),
        ("Darwin", {}, os.path.join("/tmp", "{user}", "siso")),
        (
            "Linux",
            {"XDG_RUNTIME_DIR": "a" * 100},
            os.path.join("/tmp", "{user}", "siso"),
        ),
    ],
)
def test_resolve_sockets_folder(
    siso_test_fixture: Any,
    tmp_path: Path,
    platform: str,
    env_vars: Dict[str, str],
    want_path_template: str,
    mocker: Any,
) -> None:
    user = "testuser"
    # Replace placeholders in paths
    for key, value in env_vars.items():
        env_vars[key] = value.format(root_dir=str(tmp_path))
    want_path = want_path_template.format(root_dir=str(tmp_path), user=user)
    mocker.patch("sys.platform", new=platform.lower())
    path, length = siso._resolve_sockets_folder(env_vars)
    # If the desired path is too long, the function will fall back to /tmp/<user>/siso
    if (104 - len(want_path) - 6) < 1:
        expected_path = os.path.join("/tmp", user, "siso")
    else:
        expected_path = want_path
    expected_len = 104 - len(expected_path) - 6
    # Windows.
    assert path == expected_path
    assert length == expected_len
    assert os.path.isdir(path)


@pytest.mark.parametrize(
    "args, subcmd, project_val, telemetry_enabled",
    [
        (["-h"], "ninja", "test-project", True),
        (["--help"], "ninja", "test-project", True),
        (["-help"], "ninja", "test-project", True),
        ([], "other", "test-project", True),
        ([], "ninja", "", True),
        ([], "ninja", "test-project", False),
    ],
)
def test_main_handle_collector_skipped(
    siso_project_setup: None,
    tmp_path: Path,
    mocker: Any,
    args: List[str],
    subcmd: str,
    project_val: str,
    telemetry_enabled: bool,
) -> None:
    _get_sisoenv_path(tmp_path).write_text("")
    siso_bin_path = _get_siso_bin_path(tmp_path)

    mocker.patch("siso._fetch_metrics_project", return_value=project_val)

    runner = mocker.Mock(return_value=0)
    telemetry_cfg = create_telemetry_cfg(
        tmp_path, mocker, enabled=telemetry_enabled
    )

    siso.main(
        ["siso.py"] + args + ([subcmd] if subcmd else []),
        telemetry_cfg=telemetry_cfg,
        env={"SISO_PATH": str(siso_bin_path)},
        runner=runner,
    )

    # Verify that the runner was NOT passed a collector address in its env
    _, called_kwargs = runner.call_args
    called_env = called_kwargs.get("env", {})
    assert "SISO_COLLECTOR_ADDRESS" not in called_env


@pytest.fixture
def start_collector_mocks(mocker: Any) -> Dict[str, Any]:
    mocks = {
        "subprocess_run": mocker.patch("siso.subprocess.run"),
        "kill_collector": mocker.patch("siso._kill_collector"),
        "time_sleep": mocker.patch("siso.time.sleep"),
        "time_time": mocker.patch(
            "siso.time.time", side_effect=(1000 + i * 0.1 for i in range(100))
        ),
        "http_connection": mocker.patch("siso.http.client.HTTPConnection"),
        "subprocess_popen": mocker.patch("siso.subprocess.Popen"),
        "os_path_exists": mocker.patch("os.path.exists", return_value=True),
        "os_remove": mocker.patch("os.remove"),
    }
    mock_conn = mocker.Mock()
    mocks["http_connection"].return_value = mock_conn
    mocks["mock_conn"] = mock_conn
    return mocks


def _configure_http_responses(
    mocker: Any,
    mock_conn: Any,
    status_responses: List[Tuple[int, Any]],
    config_responses: Optional[List[Tuple[int, Any]]] = None,
) -> None:
    if config_responses is None:
        config_responses = []

    request_path_history = []

    def request_side_effect(method, path):
        request_path_history.append(path)

    def getresponse_side_effect():
        path = request_path_history[-1]
        if path == "/health/status":
            if not status_responses:
                return mocker.Mock(
                    status=404, read=mocker.Mock(return_value=b"")
                )
            status_code, _ = status_responses.pop(0)
            return mocker.Mock(
                status=status_code, read=mocker.Mock(return_value=b"")
            )
        if path == "/health/config":
            if not config_responses:
                return mocker.Mock(
                    status=200, read=mocker.Mock(return_value=b"{}")
                )
            status_code, _ = config_responses.pop(0)
            return mocker.Mock(
                status=status_code, read=mocker.Mock(return_value=b"")
            )
        return mocker.Mock(status=404)

    mock_conn.request.side_effect = request_side_effect
    mock_conn.getresponse.side_effect = getresponse_side_effect


def test_handle_collector_removes_existing_socket_file(
    siso_test_fixture: Any, start_collector_mocks: Dict[str, Any], mocker: Any
) -> None:
    mocker.patch("sys.platform", new="linux")
    mock_os_path_exists = mocker.patch("os.path.exists", return_value=True)
    mock_os_remove = mocker.patch("os.remove")
    mocker.patch("siso._fetch_metrics_project", return_value="test-project")
    siso_path = "siso_path"
    sockets_file = os.path.join("/tmp", "testuser", "siso", "test-project.sock")
    siso._handle_collector(siso_path, ["ninja"], {})
    mock_os_path_exists.assert_called_with(sockets_file)
    mock_os_remove.assert_called_with(sockets_file)


def test_handle_collector_remove_socket_file_fails(
    siso_test_fixture: Any, start_collector_mocks: Dict[str, Any], mocker: Any
) -> None:
    mocker.patch("sys.platform", new="linux")
    mock_os_path_exists = mocker.patch("os.path.exists", return_value=True)
    mock_os_remove = mocker.patch(
        "os.remove", side_effect=OSError("Permission denied")
    )
    mock_stderr = mocker.patch("sys.stderr", new_callable=io.StringIO)
    mocker.patch("siso._fetch_metrics_project", return_value="test-project")
    siso_path = "siso_path"
    sockets_file = os.path.join("/tmp", "testuser", "siso", "test-project.sock")
    siso._handle_collector(siso_path, ["ninja"], {})
    mock_os_path_exists.assert_called_with(sockets_file)
    mock_os_remove.assert_called_with(sockets_file)
    assert f"Failed to remove {sockets_file}" in mock_stderr.getvalue()


@pytest.mark.parametrize(
    "global_flags, subcmd_flags, args, should_collect_logs, env, want, want_stderr",
    [
        pytest.param(
            [],
            {},
            ["other", "-C", "out/Default"],
            True,
            {},
            ["other", "-C", "out/Default"],
            "",
            id="no_ninja",
        ),
        pytest.param(
            [],
            {},
            ["ninja", "-C", "out/Default"],
            False,
            {},
            [
                "ninja",
                "-C",
                "out/Default",
            ],
            "",
            id="ninja_no_logs",
        ),
        pytest.param(
            [],
            {},
            ["ninja", "-C", "out/Default"],
            True,
            {},
            [
                "ninja",
                "-C",
                "out/Default",
                "--metrics_labels",
                f"type=developer,tool=siso,host_os={siso._SYSTEM_DICT.get(sys.platform, sys.platform)}",
                "--namespace=developer",
            ],
            "",
            id="ninja_with_logs_no_project",
        ),
        pytest.param(
            [],
            {},
            ["ninja", "-C", "out/Default", "--project=test-project"],
            True,
            {},
            [
                "ninja",
                "-C",
                "out/Default",
                "--project=test-project",
                "--metrics_labels",
                f"type=developer,tool=siso,host_os={siso._SYSTEM_DICT.get(sys.platform, sys.platform)}",
                "--namespace=developer",
                "--enable_cloud_monitoring",
                "--enable_cloud_profiler",
                "--enable_cloud_trace",
                "--enable_cloud_logging",
                "--metrics_project=test-project",
            ],
            "",
            id="ninja_with_logs_with_project_in_args",
        ),
        pytest.param(
            [],
            {},
            ["ninja", "-C", "out/Default"],
            True,
            {"SISO_PROJECT": "test-project"},
            [
                "ninja",
                "-C",
                "out/Default",
                "--metrics_labels",
                f"type=developer,tool=siso,host_os={siso._SYSTEM_DICT.get(sys.platform, sys.platform)}",
                "--namespace=developer",
                "--enable_cloud_monitoring",
                "--enable_cloud_profiler",
                "--enable_cloud_trace",
                "--enable_cloud_logging",
                "--metrics_project=test-project",
            ],
            "",
            id="ninja_with_logs_with_project_in_env",
        ),
        pytest.param(
            ["-gflag"],
            {"ninja": ["-sflag"]},
            ["ninja", "-C", "out/Default"],
            False,
            {},
            [
                "-gflag",
                "ninja",
                "-sflag",
                "-C",
                "out/Default",
            ],
            "depot_tools/siso.py: %s\n"
            % shlex.join(["-gflag", "ninja", "-sflag", "-C", "out/Default"]),
            id="with_sisorc",
        ),
        pytest.param(
            ["-gflag_only"],
            {},
            ["ninja", "-C", "out/Default"],
            False,
            {},
            [
                "-gflag_only",
                "ninja",
                "-C",
                "out/Default",
            ],
            "depot_tools/siso.py: %s\n"
            % shlex.join(["-gflag_only", "ninja", "-C", "out/Default"]),
            id="with_sisorc_global_flags_only",
        ),
        pytest.param(
            [],
            {"ninja": ["-sflag_only"]},
            ["ninja", "-C", "out/Default"],
            False,
            {},
            [
                "ninja",
                "-sflag_only",
                "-C",
                "out/Default",
            ],
            "depot_tools/siso.py: %s\n"
            % shlex.join(["ninja", "-sflag_only", "-C", "out/Default"]),
            id="with_sisorc_subcmd_flags_only",
        ),
        pytest.param(
            ["-gflag_tel"],
            {"ninja": ["-sflag_tel"]},
            ["ninja", "-C", "out/Default"],
            True,
            {"SISO_PROJECT": "telemetry-project"},
            [
                "-gflag_tel",
                "ninja",
                "-sflag_tel",
                "-C",
                "out/Default",
                "--metrics_labels",
                f"type=developer,tool=siso,host_os={siso._SYSTEM_DICT.get(sys.platform, sys.platform)}",
                "--namespace=developer",
                "--enable_cloud_monitoring",
                "--enable_cloud_profiler",
                "--enable_cloud_trace",
                "--enable_cloud_logging",
                "--metrics_project=telemetry-project",
            ],
            "depot_tools/siso.py: %s\n"
            % shlex.join(
                [
                    "-gflag_tel",
                    "ninja",
                    "-sflag_tel",
                    "-C",
                    "out/Default",
                    "--metrics_labels",
                    f"type=developer,tool=siso,host_os={siso._SYSTEM_DICT.get(sys.platform, sys.platform)}",
                    "--namespace=developer",
                    "--enable_cloud_monitoring",
                    "--enable_cloud_profiler",
                    "--enable_cloud_trace",
                    "--enable_cloud_logging",
                    "--metrics_project=telemetry-project",
                ]
            ),
            id="with_sisorc_global_and_subcmd_flags_and_telemetry",
        ),
        pytest.param(
            ["-gflag_non_ninja"],
            {"query": ["-sflag_non_ninja"]},
            ["query", "-C", "out/Default"],
            True,
            {"SISO_PROJECT": "telemetry-project"},
            [
                "-gflag_non_ninja",
                "query",
                "-sflag_non_ninja",
                "-C",
                "out/Default",
            ],
            "depot_tools/siso.py: %s\n"
            % shlex.join(
                [
                    "-gflag_non_ninja",
                    "query",
                    "-sflag_non_ninja",
                    "-C",
                    "out/Default",
                ]
            ),
            id="with_sisorc_non_ninja_subcmd",
        ),
    ],
)
def test_main_process_args(
    global_flags: List[str],
    subcmd_flags: Dict[str, List[str]],
    args: List[str],
    should_collect_logs: bool,
    env: Dict[str, str],
    want: List[str],
    want_stderr: str,
    siso_project_setup: None,
    tmp_path: Path,
    mocker: Any,
) -> None:
    # Clear .sisoenv to avoid interference with test cases that set environment variables
    _get_sisoenv_path(tmp_path).write_text("")

    # Setup dummy project structure using real files to avoid mocking 'open'
    siso_bin_path = _get_siso_bin_path(tmp_path)

    # Create .sisorc if flags are provided
    sisorc_path = _get_siso_config_dir(tmp_path) / ".sisorc"
    if sisorc_path.exists():
        sisorc_path.unlink()
    if global_flags or subcmd_flags:
        with open(sisorc_path, "w") as f:
            for flag in global_flags:
                f.write(f"{flag}\n")
            for subcmd, flags in subcmd_flags.items():
                f.write(f"{subcmd} {' '.join(flags)}\n")

    mock_stderr = mocker.patch("sys.stderr", new_callable=io.StringIO)
    runner = mocker.Mock(return_value=0)
    telemetry_cfg = create_telemetry_cfg(
        tmp_path, mocker, enabled=should_collect_logs
    )

    # Provide a SISO_PATH to point to our dummy binary
    env_with_path = env.copy()
    env_with_path["SISO_PATH"] = str(siso_bin_path)

    siso.main(
        ["siso.py"] + args,
        telemetry_cfg=telemetry_cfg,
        env=env_with_path,
        runner=runner,
    )

    # Verify runner was called with the expected arguments
    assert runner.call_count == 1
    called_args = runner.call_args[0][0]
    # The first argument is the siso path, the rest are the processed args
    assert called_args[0] == str(siso_bin_path)
    assert called_args[1:] == want

    actual_stderr = mock_stderr.getvalue()
    expected_full_stderr = f"depot_tools/siso.py: Using Siso binary from SISO_PATH: {siso_bin_path}.\n"
    expected_full_stderr += want_stderr
    assert actual_stderr == expected_full_stderr


# Else it won"t even compile on Windows.
if sys.platform != "win32":
    SIGKILL = siso.signal.SIGKILL
else:
    SIGKILL = None


@pytest.mark.skipif(sys.platform == "win32", reason="Not applicable on Windows")
@pytest.mark.parametrize(
    "stdout, stderr, returncode, kill_side_effect, expected_result, expected_kill_args",
    [
        pytest.param(
            b"123\n", b"", 0, None, True, (123, SIGKILL), id="found_and_killed"
        ),
        pytest.param(
            b"",
            b"lsof: no process found\n",
            1,
            None,
            False,
            None,
            id="process_not_found",
        ),
        pytest.param(
            b"123\n",
            b"",
            0,
            OSError("Operation not permitted"),
            False,
            (123, SIGKILL),
            id="kill_fails",
        ),
        pytest.param(b"\n", b"", 0, None, False, None, id="no_pids_found"),
        pytest.param(
            b"0\n123\n456\n",
            b"",
            0,
            None,
            True,
            (123, SIGKILL),
            id="multiple_pids_found",
        ),
    ],
)
def test_kill_collector_posix(
    stdout: bytes,
    stderr: bytes,
    returncode: int,
    kill_side_effect: Optional[OSError],
    expected_result: bool,
    expected_kill_args: Optional[Tuple[int, Any]],
    mocker: Any,
) -> None:
    mocker.patch("sys.platform", new="linux")
    mock_os_kill = mocker.patch("siso.os.kill")
    mock_subprocess_run = mocker.patch("siso.subprocess.run")
    mock_subprocess_run.return_value = mocker.Mock(
        stdout=stdout, stderr=stderr, returncode=returncode
    )
    mock_os_kill.side_effect = kill_side_effect
    result = siso._kill_collector()
    assert result == expected_result
    mock_subprocess_run.assert_called_once_with(
        ["lsof", "-t", f"-i:{siso._OTLP_HEALTH_PORT}"], capture_output=True
    )
    if expected_kill_args:
        mock_os_kill.assert_called_once_with(*expected_kill_args)
    else:
        mock_os_kill.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="Only for Windows")
@pytest.mark.parametrize(
    "run_effects, expected_result, expected_calls",
    [
        pytest.param(
            [
                (
                    f"  TCP    127.0.0.1:{siso._OTLP_HEALTH_PORT}        [::]:0                 LISTENING       1234\r\n".encode(
                        "utf-8"
                    ),
                    b"",
                    0,
                ),
                (b"", b"", 0),
            ],
            True,
            [
                ["netstat", "-aon"],
                ["taskkill", "/F", "/T", "/PID", "1234"],
            ],
            id="found_and_killed",
        ),
        pytest.param(
            [
                (
                    b"  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       868\r\n",
                    b"",
                    0,
                ),
            ],
            False,
            [["netstat", "-aon"]],
            id="process_not_found",
        ),
        pytest.param(
            [
                (
                    f"  TCP    127.0.0.1:{siso._OTLP_HEALTH_PORT}        [::]:0                 LISTENING       1234\r\n  TCP    127.0.0.1:{siso._OTLP_HEALTH_PORT}        [::]:0                 LISTENING       5678\r\n".encode(
                        "utf-8"
                    ),
                    b"",
                    0,
                ),
                (b"", b"", 0),
            ],
            True,
            [
                ["netstat", "-aon"],
                ["taskkill", "/F", "/T", "/PID", "1234"],
            ],
            id="multiple_pids_found",
        ),
        pytest.param(
            [
                (b"", b"netstat error\n", 1),
            ],
            False,
            [["netstat", "-aon"]],
            id="netstat_fails",
        ),
        pytest.param(
            [
                (
                    f"  TCP    127.0.0.1:{siso._OTLP_HEALTH_PORT}        [::]:0                 LISTENING       1234\r\n".encode(
                        "utf-8"
                    ),
                    b"",
                    0,
                ),
                (b"", b"ERROR: Cannot terminate process.", 1),
            ],
            False,
            [
                ["netstat", "-aon"],
                ["taskkill", "/F", "/T", "/PID", "1234"],
            ],
            id="taskkill_fails",
        ),
    ],
)
def test_kill_collector_windows(
    run_effects: List[Tuple[bytes, bytes, int]],
    expected_result: bool,
    expected_calls: List[List[str]],
    mocker: Any,
) -> None:
    mock_subprocess_run = mocker.patch("siso.subprocess.run")
    mock_subprocess_run.side_effect = [
        mocker.Mock(stdout=stdout, stderr=stderr, returncode=returncode)
        for stdout, stderr, returncode in run_effects
    ]
    result = siso._kill_collector()
    assert result == expected_result
    calls = [mocker.call(c, capture_output=True) for c in expected_calls]
    mock_subprocess_run.assert_has_calls(calls)
    assert mock_subprocess_run.call_count == len(calls)


@pytest.mark.parametrize(
    "platform, creationflags",
    [
        ("linux", 0),
        ("win32", 134217728),  # subprocess.CREATE_NO_WINDOW
    ],
)
def test_handle_collector_dead_then_healthy(
    siso_test_fixture: Any,
    platform: str,
    creationflags: int,
    start_collector_mocks: Dict[str, Any],
    mocker: Any,
) -> None:
    mocker.patch("sys.platform", new=platform)
    mocker.patch("subprocess.CREATE_NO_WINDOW", creationflags, create=True)
    mock_json_loads = mocker.patch("siso.json.loads")
    m = start_collector_mocks
    siso_path = "siso_path"
    project = "test-project"
    _configure_http_responses(
        mocker,
        m["mock_conn"],
        status_responses=[(404, None), (200, None)],
        config_responses=[(200, None)],
    )
    status_healthy = {"healthy": True, "status": "StatusOK"}
    if platform == "linux":
        endpoint = os.path.join("/tmp", "testuser", "siso", f"{project}.sock")
    else:
        endpoint = siso._OTLP_DEFAULT_TCP_ENDPOINT
    config = {
        "receivers": {"otlp": {"protocols": {"grpc": {"endpoint": endpoint}}}}
    }
    mock_json_loads.side_effect = [status_healthy, config]
    env = {}
    args = ["ninja", "--project", project]
    res_env = siso._handle_collector(siso_path, args, env)
    assert res_env.get("SISO_COLLECTOR_ADDRESS")
    if platform == "linux":
        assert res_env["SISO_COLLECTOR_ADDRESS"] == f"unix://{endpoint}"
    else:
        assert res_env["SISO_COLLECTOR_ADDRESS"] == endpoint

    m["subprocess_popen"].assert_called_once_with(
        [siso_path, "collector", "--project", project],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=res_env,
        creationflags=creationflags,
    )
    m["kill_collector"].assert_not_called()


def test_handle_collector_unhealthy_then_healthy(
    siso_test_fixture: Any, start_collector_mocks: Dict[str, Any], mocker: Any
) -> None:
    mocker.patch("sys.platform", new="linux")
    mock_json_loads = mocker.patch("siso.json.loads")
    m = start_collector_mocks
    siso_path = "siso_path"
    project = "test-project"
    _configure_http_responses(
        mocker,
        m["mock_conn"],
        status_responses=[(200, None), (200, None)],
        config_responses=[(200, None), (200, None)],
    )
    status_unhealthy = {"healthy": False, "status": "NotOK"}
    status_healthy = {"healthy": True, "status": "StatusOK"}
    endpoint = os.path.join("/tmp", "testuser", "siso", f"{project}.sock")
    config_project_full = {
        "exporters": {"googlecloud": {"project": project}},
        "receivers": {"otlp": {"protocols": {"grpc": {"endpoint": endpoint}}}},
    }
    mock_json_loads.side_effect = [
        status_unhealthy,
        status_healthy,
        config_project_full,
        config_project_full,
    ]
    env = {}
    args = ["ninja", "--project", project]
    res_env = siso._handle_collector(siso_path, args, env)
    assert res_env.get("SISO_COLLECTOR_ADDRESS") == f"unix://{endpoint}"

    m["subprocess_popen"].assert_called_once_with(
        [siso_path, "collector", "--project", project],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=res_env,
        creationflags=0,
    )
    m["kill_collector"].assert_called_once()


def test_handle_collector_already_healthy(
    siso_test_fixture: Any, start_collector_mocks: Dict[str, Any], mocker: Any
) -> None:
    mocker.patch("sys.platform", new="linux")
    mock_json_loads = mocker.patch("siso.json.loads")
    m = start_collector_mocks
    siso_path = "siso_path"
    project = "test-project"
    _configure_http_responses(
        mocker,
        m["mock_conn"],
        status_responses=[(200, None)],
        config_responses=[(200, None), (200, None)],
    )
    status_healthy = {"healthy": True, "status": "StatusOK"}
    endpoint = os.path.join("/tmp", "testuser", "siso", f"{project}.sock")
    config_project_full = {
        "exporters": {"googlecloud": {"project": project}},
        "receivers": {"otlp": {"protocols": {"grpc": {"endpoint": endpoint}}}},
    }
    mock_json_loads.side_effect = [
        status_healthy,
        config_project_full,
        config_project_full,
    ]
    env = {}
    args = ["ninja", "--project", project]
    res_env = siso._handle_collector(siso_path, args, env)
    assert res_env.get("SISO_COLLECTOR_ADDRESS") == f"unix://{endpoint}"
    m["subprocess_popen"].assert_not_called()
    m["kill_collector"].assert_not_called()


def test_handle_collector_never_healthy(
    siso_test_fixture: Any, start_collector_mocks: Dict[str, Any], mocker: Any
) -> None:
    mocker.patch("sys.platform", new="linux")
    m = start_collector_mocks

    captured_env = {}

    def popen_side_effect(*args, **kwargs):
        nonlocal captured_env
        if "env" in kwargs:
            captured_env = kwargs["env"].copy()
        return mocker.Mock()

    m["subprocess_popen"].side_effect = popen_side_effect

    siso_path = "siso_path"
    project = "test-project"
    _configure_http_responses(
        mocker, m["mock_conn"], status_responses=[(404, None)]
    )
    env = {}
    args = ["ninja", "--project", project]
    res_env = siso._handle_collector(siso_path, args, env)
    # If never healthy, handle_collector removes the address from env
    assert "SISO_COLLECTOR_ADDRESS" not in res_env

    endpoint = os.path.join("/tmp", "testuser", "siso", f"{project}.sock")
    expected_env = env.copy()
    expected_env["SISO_COLLECTOR_ADDRESS"] = f"unix://{endpoint}"

    assert captured_env == expected_env

    m["subprocess_popen"].assert_called_once()
    m["kill_collector"].assert_not_called()


@pytest.mark.parametrize(
    "expected_result, http_status_responses, json_loads_side_effect_values",
    [
        (
            True,
            [(404, None), (200, None)],
            ["status_healthy", "config_with_socket"],
        ),
        (
            True,
            [(404, None)] + [(404, None)] * 5 + [(200, None)],
            ["status_healthy", "config_with_socket"],
        ),
        (False, [(404, None)] * 30, []),
    ],
    ids=["healthy_immediately", "healthy_later", "never_healthy"],
)
def test_handle_collector_lifecycle(
    siso_test_fixture: Any,
    start_collector_mocks: Dict[str, Any],
    mocker: Any,
    expected_result: bool,
    http_status_responses: List[Tuple[int, Any]],
    json_loads_side_effect_values: List[str],
) -> None:
    mocker.patch("sys.platform", new="linux")
    mock_json_loads = mocker.patch("siso.json.loads")
    siso_path = "siso_path"
    project = "test-project"
    endpoint = os.path.join("/tmp", "testuser", "siso", f"{project}.sock")
    status_healthy = {"healthy": True, "status": "StatusOK"}
    config_with_socket = {
        "receivers": {"otlp": {"protocols": {"grpc": {"endpoint": endpoint}}}}
    }
    json_loads_map = {
        "status_healthy": status_healthy,
        "config_with_socket": config_with_socket,
    }
    json_loads_side_effect = [
        json_loads_map[v] for v in json_loads_side_effect_values
    ]
    m = start_collector_mocks
    mock_json_loads.side_effect = json_loads_side_effect
    _configure_http_responses(
        mocker,
        m["mock_conn"],
        status_responses=list(http_status_responses),
        config_responses=[(200, None)] * 20,
    )

    captured_env = {}

    def popen_side_effect(*args, **kwargs):
        nonlocal captured_env
        if "env" in kwargs:
            captured_env = kwargs["env"].copy()
        return mocker.Mock()

    m["subprocess_popen"].side_effect = popen_side_effect

    env = {}
    args = ["ninja", "--project", project]
    res_env = siso._handle_collector(siso_path, args, env)

    if expected_result:
        assert res_env.get("SISO_COLLECTOR_ADDRESS") == f"unix://{endpoint}"
    else:
        assert "SISO_COLLECTOR_ADDRESS" not in res_env

    expected_env = env.copy()
    expected_env["SISO_COLLECTOR_ADDRESS"] = f"unix://{endpoint}"

    m["subprocess_popen"].assert_called_once()
    assert captured_env == expected_env

    if not expected_result:
        m["kill_collector"].assert_not_called()


@pytest.mark.skipif(sys.platform == "win32", reason="Not applicable on Windows")
def test_handle_collector_missing_sockets_file_appears_later(
    siso_test_fixture: Any,
    start_collector_mocks: Dict[str, Any],
    mocker: Any,
) -> None:
    mocker.patch("sys.platform", new="linux")

    socket_exists_vals = iter([False, False, True])

    def socket_file_sideeff(path: str) -> bool:
        if path.endswith(".sock"):
            return next(socket_exists_vals)
        return DEFAULT

    mocker.patch("os.path.exists", side_effect=socket_file_sideeff)

    m = start_collector_mocks
    siso_path = "siso_path"
    project = "test-project"
    endpoint = os.path.join("/tmp", "testuser", "siso", f"{project}.sock")

    # Status: DEAD -> (Start) -> Loop 1 (200) -> Loop 2 (200)
    _configure_http_responses(
        mocker,
        m["mock_conn"],
        status_responses=[(404, None), (200, None), (200, None)],
        config_responses=[(200, None), (200, None)],
    )

    status_healthy = {"healthy": True, "status": "StatusOK"}
    config_with_socket = {
        "receivers": {"otlp": {"protocols": {"grpc": {"endpoint": endpoint}}}}
    }

    mock_json_loads = mocker.patch("siso.json.loads")
    mock_json_loads.side_effect = [
        status_healthy,
        config_with_socket,
        status_healthy,
        config_with_socket,
    ]

    env = {}
    args = ["ninja", "--project", project]

    res_env = siso._handle_collector(siso_path, args, env)

    assert res_env.get("SISO_COLLECTOR_ADDRESS") == f"unix://{endpoint}"


@pytest.mark.skipif(sys.platform == "win32", reason="Not applicable on Windows")
def test_handle_collector_missing_sockets_file_never_appears(
    siso_test_fixture: Any,
    start_collector_mocks: Dict[str, Any],
    mocker: Any,
) -> None:
    mocker.patch("sys.platform", new="linux")

    def socket_file_sideeff(path: str) -> bool:
        if path.endswith(".sock"):
            return False
        return DEFAULT

    mocker.patch("os.path.exists", side_effect=socket_file_sideeff)

    m = start_collector_mocks
    siso_path = "siso_path"
    project = "test-project"
    endpoint = os.path.join("/tmp", "testuser", "siso", f"{project}.sock")

    # Status: DEAD -> (Start) -> Loop 1..N (200)
    status_responses = [(404, None)] + [(200, None)] * 20
    config_responses = [(200, None)] * 20

    _configure_http_responses(
        mocker,
        m["mock_conn"],
        status_responses=status_responses,
        config_responses=config_responses,
    )

    status_healthy = {"healthy": True, "status": "StatusOK"}
    config_with_socket = {
        "receivers": {"otlp": {"protocols": {"grpc": {"endpoint": endpoint}}}}
    }

    mock_json_loads = mocker.patch("siso.json.loads")
    mock_json_loads.side_effect = itertools.cycle(
        [status_healthy, config_with_socket]
    )

    env = {}
    args = ["ninja", "--project", project]

    res_env = siso._handle_collector(siso_path, args, env)

    # Should fail to find socket file, so no address in env.
    assert "SISO_COLLECTOR_ADDRESS" not in res_env


@pytest.mark.parametrize(
    "file_exists, expected_exit",
    [
        (True, True),
        (False, False),
    ],
)
def test_check_outdir(
    tmp_path: Path, mocker: Any, file_exists: bool, expected_exit: bool
) -> None:
    out_dir = tmp_path / "out" / "Default"
    out_dir.mkdir(parents=True)
    if file_exists:
        (out_dir / ".ninja_deps").touch()

    mock_exit = mocker.patch("sys.exit")
    mock_stderr = mocker.patch("sys.stderr", new_callable=io.StringIO)

    siso.check_outdir(str(out_dir))

    if expected_exit:
        mock_exit.assert_called_once_with(1)
        assert "contains Ninja state file" in mock_stderr.getvalue()
    else:
        mock_exit.assert_not_called()


@pytest.mark.parametrize(
    "is_corp, expected_msg_part",
    [
        (True, "gclient custom vars is correct"),
        (False, "backend_config/README.md"),
    ],
)
def test_main_backend_star_missing(
    siso_project_setup: None,
    tmp_path: Path,
    mocker: Any,
    is_corp: bool,
    expected_msg_part: str,
) -> None:
    # Setup project structure
    backend_config_dir = _get_siso_config_dir(tmp_path) / "backend_config"
    backend_config_dir.mkdir()
    # Ensure backend.star does NOT exist

    mocker.patch("siso._is_google_corp_machine", return_value=is_corp)
    mock_stderr = mocker.patch("sys.stderr", new_callable=io.StringIO)

    exit_code = siso.main(["siso.py", "ninja", "-C", "out/Default"])

    assert exit_code == 1
    assert expected_msg_part in mock_stderr.getvalue()


def test_main_siso_binary_missing(
    siso_project_setup: None, tmp_path: Path, mocker: Any
) -> None:
    # Remove created siso.
    siso_bin_path = _get_siso_bin_path(tmp_path)
    siso_bin_path.unlink()

    mock_stderr = mocker.patch("sys.stderr", new_callable=io.StringIO)

    exit_code = siso.main(["siso.py", "ninja", "-C", "out/Default"])

    assert exit_code == 1
    assert "Could not find siso in third_party/siso" in mock_stderr.getvalue()


def test_main_siso_override_path_missing(
    siso_project_setup: None, tmp_path: Path, mocker: Any
) -> None:
    # Setup project structure (minimal needed to reach the check)
    mock_stderr = mocker.patch("sys.stderr", new_callable=io.StringIO)

    # Set SISO_PATH to a non-existent file
    env = {"SISO_PATH": str(tmp_path / "non_existent_siso")}

    exit_code = siso.main(["siso.py", "ninja", "-C", "out/Default"], env=env)

    assert exit_code == 1
    assert "Could not find Siso at provided SISO_PATH" in mock_stderr.getvalue()


def test_main_sisoenv_missing(
    siso_project_setup: None, tmp_path: Path, mocker: Any
) -> None:
    # Setup project structure
    # Do NOT create .sisoenv
    sisoenv_file = _get_sisoenv_path(tmp_path)
    sisoenv_file.unlink()

    mock_stderr = mocker.patch("sys.stderr", new_callable=io.StringIO)

    exit_code = siso.main(["siso.py", "ninja", "-C", "out/Default"])

    assert exit_code == 1
    assert "Could not find .sisoenv" in mock_stderr.getvalue()


@pytest.mark.parametrize(
    "env, expected_val",
    [
        ({}, "1"),
        ({"PYTHONDONTWRITEBYTECODE": "0"}, "0"),
        ({"PYTHONPYCACHEPREFIX": "/tmp/pycache"}, None),
        (
            {
                "PYTHONPYCACHEPREFIX": "/tmp/pycache",
                "PYTHONDONTWRITEBYTECODE": "0",
            },
            "0",
        ),
    ],
)
def test_env_python_dont_write_bytecode(
    siso_project_setup: None,
    mocker: Any,
    env: Dict[str, str],
    expected_val: Optional[str],
) -> None:
    runner = mocker.Mock(return_value=0)

    siso.main(["siso.py", "ninja"], env=env, runner=runner)

    assert runner.called
    call_env = runner.call_args[1]["env"]
    if expected_val is None:
        assert "PYTHONDONTWRITEBYTECODE" not in call_env
    else:
        assert call_env.get("PYTHONDONTWRITEBYTECODE") == expected_val


def test_main_fallback_to_siso_path(
    siso_project_setup: None, tmp_path: Path, mocker: Any
) -> None:
    # Setup: valid solution path but NO .sisoenv
    sisoenv_file = _get_sisoenv_path(tmp_path)
    sisoenv_file.unlink()

    # Setup valid SISO_PATH
    siso_bin = tmp_path / "custom_siso"
    siso_bin.touch()
    os.chmod(siso_bin, 0o755)

    runner = mocker.Mock(return_value=0)

    env = {"SISO_PATH": str(siso_bin)}

    exit_code = siso.main(
        ["siso.py", "ninja", "-C", "out/Default"], env=env, runner=runner
    )

    assert exit_code == 0
    # Verify runner called with override path
    cmd = runner.call_args[0][0]
    assert cmd[0] == str(siso_bin)


@pytest.mark.skipif(sys.platform != "win32", reason="Only for Windows")
@pytest.mark.parametrize(
    "input_args, expected_cmd",
    [
        pytest.param(
            ["siso.py", "ninja -C out/Default"],
            ["C:\\siso.exe", "ninja", "-C", "out/Default"],
            id="standard_args",
        ),
        pytest.param(
            ["siso.py", 'ninja -C out/Default"'],
            ["C:\\siso.exe", "ninja", "-C", "out/Default"],
            id="trailing_quote_on_last_arg",
        ),
        pytest.param(
            ["siso.py", 'ninja --flag="value" -C out/Default'],
            ["C:\\siso.exe", "ninja", '--flag="value"', "-C", "out/Default"],
            id="quotes_preserved_in_middle_arg",
        ),
        pytest.param(
            ["siso.py", 'ninja --flag="value" -C out/Default"'],
            ["C:\\siso.exe", "ninja", '--flag="value"', "-C", "out/Default"],
            id="quotes_preserved_in_middle_and_stripped_from_last",
        ),
        pytest.param(
            ["siso.py", 'ninja -C out/Default --flag="value"'],
            ["C:\\siso.exe", "ninja", "-C", "out/Default", '--flag="value"'],
            id="quote_escaped_flag_at_the_end_preserved",
        ),
        pytest.param(
            ["siso.py", 'ninja -C out/Default --flag="value""'],
            ["C:\\siso.exe", "ninja", "-C", "out/Default", '--flag="value"'],
            id="quote_escaped_flag_with_trailing_backslash_stripped",
        ),
    ],
)
def test_main_windows_arg_splitting(
    input_args: List[str], expected_cmd: List[str], mocker: Any
) -> None:
    mocker.patch("siso.signal.signal")

    # Mock internals to bypass file checks
    mocker.patch("os.path.isfile", return_value=True)  # For SISO_PATH check

    runner = mocker.Mock(return_value=0)
    env = {"SISO_PATH": "C:\\siso.exe"}

    # As the env is incomplete on windows send a mocked false telemetry.
    telemetry_cfg = mocker.Mock()
    telemetry_cfg.enabled.return_value = False
    siso.main(input_args, env=env, runner=runner, telemetry_cfg=telemetry_cfg)

    # Verify args were split and stripped correctly
    cmd = runner.call_args[0][0]
    assert cmd == expected_cmd


def test_main_e2e(
    siso_project_setup: None, tmp_path: Path, mocker: Any
) -> None:
    # siso binary, already set up.
    siso_bin = _get_siso_bin_path(tmp_path)

    runner = mocker.Mock(return_value=0)

    args = ["siso.py", "ninja", "-C", str(tmp_path)]

    # run
    exit_code = siso.main(args, runner=runner)

    assert exit_code == 0
    # Verify runner was called with siso binary and processed args.
    called_args, _ = runner.call_args
    cmd = called_args[0]
    assert cmd[0] == str(siso_bin)
    assert "ninja" in cmd
    assert "-C" in cmd
    assert str(tmp_path) in cmd
    assert any(arg.startswith("--metrics_labels") for arg in cmd)


def test_main_dynamic_subcmds_e2e(
    siso_project_setup: None, tmp_path: Path, mocker: Any
) -> None:
    siso_bin = _get_siso_bin_path(tmp_path)
    runner = mocker.Mock(return_value=0)

    # Mock _get_siso_subcmds to return dynamic subcmds including a custom one
    mocker.patch(
        "siso._get_siso_subcmds", return_value={"custom_subcmd", "ninja"}
    )

    args = ["siso.py", "-mutexprofile", "prof", "custom_subcmd", "-flag"]

    exit_code = siso.main(args, runner=runner)

    assert exit_code == 0
    called_args, _ = runner.call_args
    cmd = called_args[0]
    assert cmd[0] == str(siso_bin)

    # Check that custom_subcmd was correctly identified
    assert "custom_subcmd" in cmd
    custom_idx = cmd.index("custom_subcmd")
    # -mutexprofile and prof should be before custom_subcmd
    assert cmd[custom_idx - 2] == "-mutexprofile"
    assert cmd[custom_idx - 1] == "prof"
    assert cmd[custom_idx + 1] == "-flag"


def test_main_complex_args_e2e(
    siso_project_setup: None, tmp_path: Path, mocker: Any
) -> None:
    siso_bin = _get_siso_bin_path(tmp_path)
    runner = mocker.Mock(return_value=0)

    # Complex command similar to the reported issue.
    args = [
        "siso.py",
        "-mutexprofile",
        "siso_mutex.prof",
        "ninja",
        "-project",
        "rbe-chrome-untrusted",
        "--enable_cloud_logging",
        "-C",
        str(tmp_path / "src" / "out" / "Default"),
    ]

    exit_code = siso.main(args, runner=runner)

    assert exit_code == 0
    called_args, _ = runner.call_args
    cmd = called_args[0]
    assert cmd[0] == str(siso_bin)

    # Subcommand should be "ninja" at index 3 (after -mutexprofile prof)
    assert cmd[3] == "ninja"
    # Pre-subcommand args should be preserved
    assert cmd[1] == "-mutexprofile"
    assert cmd[2] == "siso_mutex.prof"

    # Telemetry flags should be AFTER ninja
    ninja_idx = cmd.index("ninja")
    assert any(arg.startswith("--metrics_labels") for arg in cmd[ninja_idx:])
    assert any(
        arg.startswith("--metrics_project=rbe-chrome-untrusted")
        for arg in cmd[ninja_idx:]
    )


@pytest.mark.parametrize(
    "env_var",
    [
        "GEMINI_CLI",
        "CLAUDECODE",
        "CODEX_SANDBOX",
        "CURSOR_AGENT",
        "COPILOT_CLI",
        "AI_AGENT",
        None,
    ],
)
@pytest.mark.parametrize("subcmd", ["ninja", "query"])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_ai_agent_env_only_tags_telemetry(
    env_var: Optional[str],
    subcmd: str,
    exit_code: int,
    siso_project_setup: None,
    tmp_path: Path,
    mocker: Any,
) -> None:
    _get_sisoenv_path(tmp_path).write_text("")
    siso_bin_path = _get_siso_bin_path(tmp_path)
    mock_stdout = mocker.patch("sys.stdout", new_callable=io.StringIO)
    runner = mocker.Mock(return_value=exit_code)
    cfg = create_telemetry_cfg(tmp_path, mocker, enabled=True)
    env = {"SISO_PATH": str(siso_bin_path)}
    if env_var:
        env[env_var] = "1"

    siso.main(
        ["siso.py", subcmd, "-C", "out/Default"],
        telemetry_cfg=cfg,
        env=env,
        runner=runner,
    )

    args = runner.call_args.args[0]
    subcmd = args[1]
    subcmd_args = args[2:]
    stdout = mock_stdout.getvalue()
    for flag in ["--quiet", "--batch=false", "--heartbeat_period=30s"]:
        assert flag not in args
    assert stdout == ""
    tagged = "--namespace=developer:ai-agent" in subcmd_args
    assert tagged == bool(env_var and subcmd == "ninja")


def test_apply_telemetry_flags_physical_gwindows(mocker: Any) -> None:
    mocker.patch("sys.platform", "win32")
    mocker.patch("siso._is_gce", return_value=False)

    subcmd_args = ["-C", "out/Default"]
    env = {"SISO_PROJECT": "test-project"}
    got = siso.apply_telemetry_flags(subcmd_args, env)

    assert "--enable_cloud_trace" not in got
    # Other telemetry flags should still be enabled
    assert "--enable_cloud_logging" in got
    assert "--enable_cloud_monitoring" in got
    assert "--enable_cloud_profiler" in got


def test_apply_telemetry_flags_gwindows_cloudtop(mocker: Any) -> None:
    mocker.patch("sys.platform", "win32")
    mocker.patch("siso._is_gce", return_value=True)

    subcmd_args = ["-C", "out/Default"]
    env = {"SISO_PROJECT": "test-project"}
    got = siso.apply_telemetry_flags(subcmd_args, env)

    # Telemetry should be fully enabled (so positive flags are added)
    assert "--enable_cloud_trace" in got
    assert "--enable_cloud_logging" in got
    assert "--enable_cloud_monitoring" in got
    assert "--enable_cloud_profiler" in got


def test_apply_telemetry_flags_user_override_trace(mocker: Any) -> None:
    mocker.patch("sys.platform", "win32")
    mocker.patch("siso._is_gce", return_value=False)

    # User explicitly enables trace
    subcmd_args = ["-C", "out/Default", "--enable_cloud_trace"]
    env = {"SISO_PROJECT": "test-project"}
    got = siso.apply_telemetry_flags(subcmd_args, env)

    assert "--enable_cloud_trace" in got
    assert "--enable_cloud_logging" in got
    assert "--enable_cloud_monitoring" in got
    assert "--enable_cloud_profiler" in got


# Stanza to have pytest be executed.
if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
