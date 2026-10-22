"""Isolated tests for the macOS Podman login startup and retry configuration."""

import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict

import pytest

STARTUP = Path(__file__).resolve().parents[2] / 'scripts/macos/kiro-gateway-start.sh'
PLIST = STARTUP.with_name('com.user.kiro-gateway.plist')


@pytest.fixture
def startup_harness(tmp_path: Path) -> Dict[str, Any]:
    """Create fake CLIs that record commands without accessing real containers or networks."""
    stub = '''
import json, os, sys, time
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
state_file = Path(os.environ['STUB_STATE'])
state = json.loads(state_file.read_text())
with open(os.environ['STUB_CALLS'], 'a') as stream:
    stream.write(json.dumps([name] + args) + '\\n')
def save():
    state_file.write_text(json.dumps(state))
if name == 'curl':
    if os.environ.get('STUB_INITIAL_HEALTH') == 'true':
        print('{"status":"healthy"}')
    elif not state.get('ready') or not (state.get('started') or os.environ.get('STUB_RUNNING') == 'true'):
        sys.exit(7)
    elif os.environ.get('STUB_FINAL_HEALTH') == 'malformed':
        print('not-json')
    elif os.environ.get('STUB_FINAL_HEALTH') == 'unhealthy':
        print('{"status":"starting"}')
    else:
        print('{"status":"healthy"}')
    sys.exit(0)
if args[:2] == ['machine', 'inspect']:
    if os.environ.get('STUB_INSPECT_FAIL') == 'true': sys.exit(1)
    print(os.environ.get('STUB_MACHINE_STATE', 'stopped'))
elif args[:2] == ['machine', 'start']:
    if os.environ.get('STUB_START_FAIL') == 'true': sys.exit(1)
elif args[:1] == ['--connection']:
    args = args[2:]
    if args[0] == 'info':
        time.sleep(float(os.environ.get('STUB_INFO_SLEEP', '0')))
        if os.environ.get('STUB_INFO_FAIL') == 'true': sys.exit(1)
        state['ready'] = True
        save()
        print('synthetic-host')
    elif args[:2] == ['container', 'exists']:
        if os.environ.get('STUB_MISSING_CONTAINER') == 'true': sys.exit(1)
    elif args[0] == 'inspect':
        print(os.environ.get('STUB_RUNNING', 'false'))
    elif args[0] == 'start':
        if os.environ.get('STUB_CONTAINER_START_FAIL') == 'true': sys.exit(1)
        state['started'] = True
        save()
    else:
        raise AssertionError(args)
else:
    raise AssertionError(args)
'''
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(('KIRO_GATEWAY_', 'STUB_')):
            del env[key]
    for name in ('podman', 'curl'):
        executable = tmp_path / name
        executable.write_text(f'#!{sys.executable}\n' + stub)
        executable.chmod(0o755)
    state = tmp_path / 'state.json'
    state.write_text('{}')
    calls = tmp_path / 'calls.jsonl'
    calls.touch()
    env.update({
        'HOME': str(tmp_path), 'STUB_STATE': str(state), 'STUB_CALLS': str(calls),
        'KIRO_GATEWAY_PODMAN_BIN': str(tmp_path / 'podman'),
        'KIRO_GATEWAY_CURL_BIN': str(tmp_path / 'curl'),
        'KIRO_GATEWAY_PYTHON_BIN': sys.executable,
        'KIRO_GATEWAY_START_LOG': str(tmp_path / 'startup.log'),
        'KIRO_GATEWAY_START_LOCK': str(tmp_path / 'start.lock'),
        'KIRO_GATEWAY_READY_ATTEMPTS': '1', 'KIRO_GATEWAY_HEALTH_ATTEMPTS': '1',
        'KIRO_GATEWAY_POLL_INTERVAL': '0', 'KIRO_GATEWAY_COMMAND_TIMEOUT': '2',
    })
    return {'env': env, 'calls': calls, 'lock': tmp_path / 'start.lock',
            'log': tmp_path / 'startup.log'}


def run_startup(harness: Dict[str, Any], **overrides: str) -> subprocess.CompletedProcess[str]:
    """Run the actual Bash entry point with fake external dependencies and a hard test timeout.

    Args:
        harness: Paths and environment for fake Podman and curl commands.
        overrides: Per-test startup or CLI behavior overrides.

    Returns:
        Completed shell process with captured diagnostics.
    """
    env = {**harness['env'], **overrides}
    return subprocess.run(['/bin/bash', str(STARTUP)], env=env, capture_output=True,
                          text=True, timeout=10, check=False)


def recorded_calls(harness: Dict[str, Any]) -> list[list[str]]:
    """Return the commands recorded by the fake external dependencies.

    Args:
        harness: Fixture containing the command log path.

    Returns:
        One argument list for each CLI invocation.
    """
    return [json.loads(line) for line in harness['calls'].read_text().splitlines()]


def test_stopped_machine_and_container_start_until_health_succeeds(startup_harness: Dict[str, Any]) -> None:
    """Start the named machine and existing container through the explicit root connection."""
    result = run_startup(startup_harness)
    assert result.returncode == 0, result.stderr
    calls = recorded_calls(startup_harness)
    assert ['podman', 'machine', 'start', 'podman-machine-default'] in calls
    assert ['podman', '--connection', 'podman-machine-default-root', 'start', 'kiro-gateway'] in calls
    assert 'Gateway is healthy' in startup_harness['log'].read_text()
    assert not startup_harness['lock'].exists()
    assert all('run' not in call and 'pull' not in call and 'rm' not in call for call in calls)
    assert all('--noproxy' in call and '--max-time' in call for call in calls if call[0] == 'curl')


def test_healthy_periodic_check_leaves_workloads_untouched(startup_harness: Dict[str, Any]) -> None:
    """A healthy service requires no Podman calls or restarts."""
    result = run_startup(startup_harness, STUB_INITIAL_HEALTH='true')
    assert result.returncode == 0
    assert all(call[0] == 'curl' for call in recorded_calls(startup_harness))


@pytest.mark.parametrize('state', ['running', 'starting'])
def test_existing_or_concurrently_starting_machine_is_not_started_again(
    startup_harness: Dict[str, Any], state: str,
) -> None:
    """Wait for an existing VM instead of racing Podman Desktop's startup."""
    result = run_startup(startup_harness, STUB_MACHINE_STATE=state, STUB_RUNNING='true')
    assert result.returncode == 0
    calls = recorded_calls(startup_harness)
    assert ['podman', 'machine', 'start', 'podman-machine-default'] not in calls
    assert not any(call[-2:] == ['start', 'kiro-gateway'] for call in calls)


def test_failed_machine_start_can_still_succeed_if_other_launcher_finished(startup_harness: Dict[str, Any]) -> None:
    """Treat connection readiness as authoritative when two launchers race."""
    assert run_startup(startup_harness, STUB_START_FAIL='true').returncode == 0


@pytest.mark.parametrize('behavior', [
    {'STUB_INSPECT_FAIL': 'true'}, {'STUB_MACHINE_STATE': 'unknown'},
    {'STUB_START_FAIL': 'true', 'STUB_INFO_FAIL': 'true'},
    {'STUB_MISSING_CONTAINER': 'true'}, {'STUB_CONTAINER_START_FAIL': 'true'},
    {'STUB_FINAL_HEALTH': 'unhealthy'}, {'STUB_FINAL_HEALTH': 'malformed'},
    {'KIRO_GATEWAY_PODMAN_BIN': '/nonexistent/podman'},
    {'KIRO_GATEWAY_READY_ATTEMPTS': '0'}, {'KIRO_GATEWAY_COMMAND_TIMEOUT': 'invalid'},
])
def test_startup_failures_exit_nonzero_for_launchd_retry(
    startup_harness: Dict[str, Any], behavior: Dict[str, str],
) -> None:
    """Do not report successful startup when a dependency or health check fails."""
    result = run_startup(startup_harness, **behavior)
    assert result.returncode != 0
    assert 'ERROR:' in startup_harness['log'].read_text()
    assert not startup_harness['lock'].exists()


def test_hung_podman_command_is_bounded(startup_harness: Dict[str, Any]) -> None:
    """A hung remote connection must not block launchd indefinitely."""
    before = time.monotonic()
    result = run_startup(startup_harness, STUB_INFO_SLEEP='3', KIRO_GATEWAY_COMMAND_TIMEOUT='1')
    assert result.returncode != 0
    assert time.monotonic() - before < 3


def test_active_lock_prevents_overlapping_launches(startup_harness: Dict[str, Any]) -> None:
    """Concurrent invocations must not start the same machine twice."""
    lock = startup_harness['lock']
    lock.mkdir()
    (lock / 'pid').write_text(str(os.getpid()))
    assert run_startup(startup_harness).returncode == 0
    assert recorded_calls(startup_harness) == []
    assert (lock / 'pid').read_text() == str(os.getpid())


def test_new_ownerless_lock_is_not_deleted_during_pid_publication(startup_harness: Dict[str, Any]) -> None:
    """Do not steal a lock between mkdir and PID publication."""
    startup_harness['lock'].mkdir()
    assert run_startup(startup_harness).returncode == 0
    assert startup_harness['lock'].exists()
    assert recorded_calls(startup_harness) == []


@pytest.mark.parametrize('owner', [None, '99999999', '0', 'invalid'])
def test_stale_lock_is_recovered(startup_harness: Dict[str, Any], owner: str | None) -> None:
    """Recover both abandoned PID files and old ownerless locks."""
    lock = startup_harness['lock']
    lock.mkdir()
    if owner is not None:
        (lock / 'pid').write_text(owner)
    os.utime(lock, (time.time() - 1000, time.time() - 1000))
    assert run_startup(startup_harness).returncode == 0
    assert not lock.exists()


def test_stale_lock_with_unexpected_files_is_not_recursively_deleted(startup_harness: Dict[str, Any]) -> None:
    """Leave unexpected files intact instead of deleting a directory to recover a lock."""
    lock = startup_harness['lock']
    lock.mkdir()
    (lock / 'pid').write_text('99999999')
    unexpected = lock / 'unexpected'
    unexpected.write_text('preserve this file')
    assert run_startup(startup_harness).returncode != 0
    assert unexpected.read_text() == 'preserve this file'
    assert recorded_calls(startup_harness) == []


def test_overall_deadline_caps_an_individual_command_timeout(startup_harness: Dict[str, Any]) -> None:
    """Enforce the overall startup deadline even when a command allows a longer wait."""
    before = time.monotonic()
    result = run_startup(startup_harness, STUB_INFO_SLEEP='5',
                         KIRO_GATEWAY_STARTUP_TIMEOUT='1', KIRO_GATEWAY_COMMAND_TIMEOUT='5')
    assert result.returncode != 0
    assert time.monotonic() - before < 3


def test_credentials_are_not_made_world_writable(startup_harness: Dict[str, Any], tmp_path: Path) -> None:
    """Startup must preserve private credential permissions."""
    database = tmp_path / 'Library/Application Support/kiro-cli/data.sqlite3'
    database.parent.mkdir(parents=True)
    database.write_text('synthetic credentials')
    database.chmod(0o600)
    assert run_startup(startup_harness).returncode == 0
    assert database.stat().st_mode & 0o777 == 0o600
    assert database.read_text() == 'synthetic credentials'


def test_launch_agent_preserves_background_vm_and_retries_failures() -> None:
    """Protect Podman descendants after exit and retry failed or unhealthy startup."""
    configuration = plistlib.loads(PLIST.read_bytes())
    assert configuration['AbandonProcessGroup'] is True
    assert configuration['RunAtLoad'] is True
    assert configuration['KeepAlive'] == {'SuccessfulExit': False}
    assert configuration['StartInterval'] == 60
    assert configuration['ThrottleInterval'] == 30
    assert configuration['ProgramArguments'] == ['/bin/bash', '__HOME__/workspace/kiro-gateway-start.sh']
