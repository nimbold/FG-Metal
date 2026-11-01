#!/usr/bin/env python3
"""Real Darwin adversarial tests for the Host BuildLease v3 detach boundary."""
from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import build_retention as retention


DARWIN_V3_AVAILABLE = (
    sys.platform == "darwin"
    and Path("/usr/bin/hdiutil").is_file()
    and Path("/usr/sbin/diskutil").is_file()
    and Path("/usr/bin/sandbox-exec").is_file()
    and Path("/usr/bin/cc").is_file()
)


def _cleanup_adversarial_fixture(temp: tempfile.TemporaryDirectory,
                                 workspaces: list[tuple[Path, dict | None]]) -> None:
    """Keep fixture backing images until every normal detach is independently verified."""
    try:
        for root, identity in workspaces:
            if not root.exists():
                raise RuntimeError(f"fixture workspace root disappeared; preserving {temp.name}")
            detached = retention._detach_session_workspace(root, identity)
            if not isinstance(detached, dict) or detached.get("attached") is not False:
                raise RuntimeError(f"fixture detach was not confirmed; preserving {temp.name}")
            if retention._session_image_row(root) is not None:
                raise RuntimeError(f"fixture image remains attached; preserving {temp.name}")
    except BaseException:
        # TemporaryDirectory's finalizer would otherwise remove backing image
        # pathnames after a teardown error, including KeyboardInterrupt.
        temp._finalizer.detach()
        raise
    try:
        temp.cleanup()
    except BaseException:
        temp._finalizer.detach()
        raise


class RecordingTestResult(unittest.TestResult):
    def __init__(self) -> None:
        super().__init__()
        self.successes: list[unittest.case.TestCase] = []

    def addSuccess(self, test: unittest.case.TestCase) -> None:
        self.successes.append(test)
        super().addSuccess(test)


class AdversarialFixtureCleanupTests(unittest.TestCase):
    _SYNTHETIC_IMAGE_CONTENT = b"synthetic non-mounted fixture image"

    def _fixture(self):
        temp = tempfile.TemporaryDirectory(prefix="fgmetal-fixture-cleanup-")
        root = Path(temp.name).resolve(strict=True)
        image = root / "workspace.sparseimage"
        image.write_bytes(self._SYNTHETIC_IMAGE_CONTENT)
        return temp, root, image, {"attached": True, "mountpoint": str(root / "mount")}

    def _remove_synthetic_fixture(self, temp, root: Path, image: Path) -> None:
        # These tests mock the detach/image-row behavior, so this is an ordinary
        # temporary directory, never a mounted image. Remove only its unique,
        # exact TemporaryDirectory root after verifying the marker file.
        self.assertEqual(root, Path(temp.name).resolve(strict=True))
        self.assertTrue(root.is_dir())
        self.assertFalse(root.is_symlink())
        self.assertTrue(image.is_file())
        self.assertFalse(image.is_symlink())
        self.assertEqual(image.read_bytes(), self._SYNTHETIC_IMAGE_CONTENT)
        shutil.rmtree(root)

    def test_unmount_failure_preserves_temp_root_and_image(self) -> None:
        temp, root, image, identity = self._fixture()
        try:
            with mock.patch.object(retention, "_detach_session_workspace",
                                   side_effect=retention.RetentionError("normal unmount failed")):
                with self.assertRaisesRegex(retention.RetentionError, "normal unmount failed"):
                    _cleanup_adversarial_fixture(temp, [(root, identity)])
            self.assertTrue(root.exists())
            self.assertTrue(image.exists())
        finally:
            self._remove_synthetic_fixture(temp, root, image)

    def test_detach_failure_preserves_temp_root_and_image(self) -> None:
        temp, root, image, identity = self._fixture()
        try:
            with mock.patch.object(retention, "_detach_session_workspace",
                                   side_effect=retention.RetentionError("normal detach failed")):
                with self.assertRaisesRegex(retention.RetentionError, "normal detach failed"):
                    _cleanup_adversarial_fixture(temp, [(root, identity)])
            self.assertTrue(root.exists())
            self.assertTrue(image.exists())
        finally:
            self._remove_synthetic_fixture(temp, root, image)

    def test_attached_image_row_prevents_temp_cleanup(self) -> None:
        temp, root, image, identity = self._fixture()
        try:
            with (mock.patch.object(retention, "_detach_session_workspace", return_value={"attached": False}),
                  mock.patch.object(retention, "_session_image_row", return_value={"image-path": str(image)})):
                with self.assertRaisesRegex(RuntimeError, "fixture image remains attached"):
                    _cleanup_adversarial_fixture(temp, [(root, identity)])
            self.assertTrue(root.exists())
            self.assertTrue(image.exists())
        finally:
            self._remove_synthetic_fixture(temp, root, image)

    def test_nonzero_attach_with_exact_partial_attachment_runs_normal_cleanup(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="fgmetal-partial-attach-")
        root = Path(temp.name).resolve(strict=True)
        image = root / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME
        workspace = root / retention.BUILD_SESSION_WORKSPACE_NAME
        attached = False
        mounted = False
        commands: list[list[str]] = []
        entity = {"dev-entry": "/dev/disk-test1", "mount-point": str(workspace),
                  "image-path": str(image), "image-writeable": True}
        row_mounted = {"image-path": str(image), "writeable": True,
                       "system-entities": [
                           {"dev-entry": "/dev/disk-test", "content-hint": "GUID_partition_scheme"},
                           {"dev-entry": "/dev/disk-test1", "mount-point": str(workspace)},
                       ]}
        row_unmounted = {"image-path": str(image), "writeable": True,
                         "system-entities": [
                             {"dev-entry": "/dev/disk-test", "content-hint": "GUID_partition_scheme"},
                             {"dev-entry": "/dev/disk-test1", "mount-point": None},
                         ]}

        def image_row(_root: Path):
            if not attached:
                return None
            return row_mounted if mounted else row_unmounted

        def fake_run(command, *args, **kwargs):
            nonlocal attached, mounted
            argv = [str(item) for item in command]
            commands.append(argv)
            if argv[1] == "create":
                image.write_bytes(self._SYNTHETIC_IMAGE_CONTENT)
                return subprocess.CompletedProcess(command, 0, b"", b"")
            if argv[1] == "attach":
                attached = True
                mounted = True
                return subprocess.CompletedProcess(command, 1, b"", b"injected partial attach")
            if argv[1:] == ["unmount", str(workspace)]:
                mounted = False
                return subprocess.CompletedProcess(command, 0, b"", b"")
            if argv[1:] == ["eject", "/dev/disk-test"]:
                attached = False
                mounted = False
                return subprocess.CompletedProcess(command, 0, b"", b"")
            self.fail(f"unexpected subprocess in mocked partial attach test: {argv}")

        try:
            with (mock.patch.object(retention, "_session_hdiutil",
                                    return_value=(Path("/usr/bin/hdiutil"), "hdiutil-digest")),
                  mock.patch.object(retention, "_session_diskutil",
                                    return_value=(Path("/usr/sbin/diskutil"), "diskutil-digest")),
                  mock.patch.object(retention, "_session_image_row",
                                    side_effect=image_row),
                  mock.patch.object(retention, "_session_image_entity",
                                    side_effect=lambda _root: entity if mounted else None),
                  mock.patch.object(retention, "_session_image_device",
                                    side_effect=lambda _root: "/dev/disk-test" if attached else None),
                  mock.patch.object(retention, "_session_diskutil_info", side_effect=lambda device: {
                      "DeviceIdentifier": "disk-test1" if device.endswith("1") else "disk-test",
                      "ParentWholeDisk": "disk-test", "VolumeUUID": "R85-SYNTHETIC-PARTIAL-UUID",
                      "FilesystemType": "hfs", "Writable": True,
                      "MountPoint": str(workspace) if mounted else None,
                      "Whole": device == "/dev/disk-test",
                  }),
                  mock.patch.object(retention, "_session_image_content_identity", side_effect=lambda path: {
                      "path": str(path), "device": image.stat(follow_symlinks=False).st_dev,
                      "inode": image.stat(follow_symlinks=False).st_ino,
                      "size_bytes": image.stat(follow_symlinks=False).st_size,
                      "sha256": "c" * 64,
                  }),
                  mock.patch.object(retention.subprocess, "run", side_effect=fake_run)):
                with self.assertRaisesRegex(retention.RetentionError, "could not attach"):
                    retention._attach_session_workspace(root, 32 * 1024 * 1024)
            self.assertIn(["/usr/sbin/diskutil", "unmount", str(workspace)], commands)
            self.assertIn(["/usr/sbin/diskutil", "eject", "/dev/disk-test"], commands)
            self.assertFalse(attached)
            self.assertFalse(mounted)
            self.assertTrue(image.exists())
        finally:
            temp.cleanup()

    def test_host_fixture_setup_registers_cleanup_before_attach_can_fail(self) -> None:
        case = HostBuildLeaseV3AdversarialTests("test_01_normal_child_exit_allows_normal_unmount_and_detach")
        real_temporary_directory = tempfile.TemporaryDirectory
        cleanup_error = retention.RetentionError("synthetic attach failed after fixture setup began")
        cleanup = mock.Mock()
        try:
            with (mock.patch.object(tempfile, "TemporaryDirectory",
                                    side_effect=lambda **kwargs: real_temporary_directory(
                                        prefix=kwargs.get("prefix", "fgmetal-setup-cleanup-"))),
                  mock.patch.object(retention, "_attach_session_workspace", side_effect=cleanup_error),
                  mock.patch(__name__ + "._cleanup_adversarial_fixture", cleanup)):
                with self.assertRaises(retention.RetentionError) as caught:
                    case.setUp()
                self.assertIs(caught.exception, cleanup_error)
                case.doCleanups()
            cleanup.assert_called_once_with(case.temp, [(case.root, case.workspace_identity)])
            self.assertIsNone(case.workspace_identity)
        finally:
            if hasattr(case, "temp"):
                case.temp.cleanup()

    def test_successful_normal_detach_checks_absent_row_before_cleanup(self) -> None:
        temp, root, image, identity = self._fixture()
        original_cleanup = temp.cleanup
        calls: list[str] = []

        def verified_cleanup() -> None:
            calls.append("cleanup")
            self.assertIsNone(retention._session_image_row(root))
            original_cleanup()

        try:
            with (mock.patch.object(retention, "_detach_session_workspace",
                                    side_effect=lambda actual_root, actual_identity:
                                    calls.append("detach") or {"attached": False}) as detach,
                  mock.patch.object(retention, "_session_image_row", return_value=None),
                  mock.patch.object(temp, "cleanup", side_effect=verified_cleanup)):
                _cleanup_adversarial_fixture(temp, [(root, identity)])
            detach.assert_called_once_with(root, identity)
            self.assertEqual(calls, ["detach", "cleanup"])
            self.assertFalse(root.exists())
            self.assertFalse(image.exists())
        finally:
            original_cleanup()

    def test_unhandled_keyboard_interrupt_runs_fixture_cleanup_without_recording_success(self) -> None:
        temp, root, image, identity = self._fixture()
        events: list[str] = []

        class InterruptedCase(HostBuildLeaseV3AdversarialTests):
            def setUp(self) -> None:
                self.addCleanup(_cleanup_adversarial_fixture, temp, [(root, identity)])

            def runTest(self) -> None:
                raise KeyboardInterrupt("synthetic interrupt")

        def retire(_root, _identity):
            events.append("detach")
            return {"attached": False}

        def image_row(_root):
            events.append("row-absence")
            return None

        result = RecordingTestResult()
        try:
            with (mock.patch.object(retention, "_detach_session_workspace", side_effect=retire),
                  mock.patch.object(retention, "_session_image_row", side_effect=image_row)):
                with self.assertRaisesRegex(KeyboardInterrupt, "synthetic interrupt"):
                    InterruptedCase().run(result)
            self.assertEqual(result.testsRun, 1)
            self.assertEqual(result.successes, [])
            self.assertEqual(events, ["detach", "row-absence"])
            self.assertFalse(root.exists())
            self.assertFalse(image.exists())
        finally:
            if root.exists():
                self._remove_synthetic_fixture(temp, root, image)

    def test_unittest_cleanup_failure_prevents_success_and_preserves_temp_root(self) -> None:
        temp, root, image, identity = self._fixture()

        class CleanupFailureCase(unittest.TestCase):
            def runTest(self) -> None:
                self.addCleanup(_cleanup_adversarial_fixture, temp, [(root, identity)])

        result = RecordingTestResult()
        try:
            with mock.patch.object(retention, "_detach_session_workspace",
                                   side_effect=retention.RetentionError("synthetic detach failure")):
                CleanupFailureCase().run(result)
            self.assertEqual(result.testsRun, 1)
            self.assertEqual(result.successes, [])
            self.assertEqual(len(result.errors), 1)
            self.assertTrue(root.exists())
            self.assertTrue(image.exists())
        finally:
            self._remove_synthetic_fixture(temp, root, image)


class HostBuildLeaseV3AdversarialTests(unittest.TestCase):
    """Exercise real HFS+ image operations, live processes, and Seatbelt rules."""

    def run(self, result: unittest.TestResult | None = None) -> unittest.TestResult:
        owns_result = result is None
        selected_result = result if result is not None else self.defaultTestResult()
        start_test_run = getattr(selected_result, "startTestRun", None) if owns_result else None
        stop_test_run = getattr(selected_result, "stopTestRun", None) if owns_result else None
        if start_test_run is not None:
            start_test_run()
        stopped_test_run = False
        try:
            return super().run(selected_result)
        except BaseException:
            # unittest.TestCase.run skips doCleanups when a test part propagates
            # KeyboardInterrupt or another uncaught BaseException.
            while self._cleanups:
                function, args, kwargs = self._cleanups.pop()
                try:
                    function(*args, **kwargs)
                except BaseException as error:
                    selected_result.addError(self, (type(error), error, error.__traceback__))
            if stop_test_run is not None:
                stopped_test_run = True
                try:
                    stop_test_run()
                except BaseException as error:
                    selected_result.addError(self, (type(error), error, error.__traceback__))
            raise
        finally:
            if stop_test_run is not None and not stopped_test_run:
                stop_test_run()

    @unittest.skipUnless(DARWIN_V3_AVAILABLE, "requires Darwin hdiutil, diskutil, Seatbelt, and C compiler")
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-host-v3-adversarial-", dir="/private/tmp")
        self.root: Path | None = None
        self.workspace_identity: dict | None = None
        self.fixture_workspaces: list[tuple[Path, dict | None]] = []
        self.addCleanup(self._cleanup_fixture_resources)
        self.base = Path(self.temp.name).resolve(strict=True)
        self.root = self.base / "build-root"
        self.root.mkdir(mode=0o700)
        self.fixture_workspaces.append((self.root, None))
        self.workspace_identity = retention._attach_session_workspace(self.root, 32 * 1024 * 1024)
        self.fixture_workspaces[-1] = (self.root, self.workspace_identity)
        self.workspace = Path(self.workspace_identity["mountpoint"])
        self.image = Path(self.workspace_identity["image_path"])
        self.child_pids: set[int] = set()
        self.child_processes: list[subprocess.Popen] = []
        for relative in (".home", ".home/.cache", ".home/.config", ".home/.local/share", ".tmp"):
            (self.workspace / relative).mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        for process in self.child_processes:
            if process.poll() is None:
                process.kill()
        for pid in tuple(self.child_pids):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            live = []
            for pid in tuple(self.child_pids):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                live.append(pid)
            if not live:
                break
            time.sleep(0.05)

    def _cleanup_fixture_resources(self) -> None:
        _cleanup_adversarial_fixture(self.temp, self.fixture_workspaces)

    def _attach_fixture_workspace(self, root: Path) -> dict:
        root.mkdir(mode=0o700)
        self.fixture_workspaces.append((root, None))
        identity = retention._attach_session_workspace(root, 32 * 1024 * 1024)
        self.fixture_workspaces[-1] = (root, identity)
        return identity

    def _remember_fixture_workspace_identity(self, root: Path, identity: dict) -> None:
        for index, (tracked_root, _tracked_identity) in enumerate(self.fixture_workspaces):
            if tracked_root == root:
                self.fixture_workspaces[index] = (root, identity)
                return
        self.fail(f"untracked HFS fixture root: {root}")

    def _environment(self) -> dict[str, str]:
        return {"PATH": "/usr/bin:/bin", "HOME": str(self.workspace / ".home"), "LANG": "C", "LC_ALL": "C"}

    def _sandboxed(self, argv: list[str]) -> tuple[list[str], dict[str, str]]:
        wrapped, env, _launcher = retention._sandbox_session_command(argv, self.workspace, self._environment())
        return wrapped, env

    def _launch_sandboxed(self, code: str, *arguments: str) -> subprocess.Popen:
        wrapped, env = self._sandboxed([sys.executable, "-c", code, *arguments])
        process = subprocess.Popen(wrapped, cwd=self.workspace, env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True, pass_fds=())
        self.child_processes.append(process)
        self.child_pids.add(process.pid)
        return process

    def _wait_text(self, path: Path, *, timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                text = path.read_text(encoding="ascii").strip()
                if text:
                    return text
            except FileNotFoundError:
                pass
            time.sleep(0.01)
        self.fail(f"timed out waiting for {path}")
        return ""

    def _kill_pid(self, pid: int) -> None:
        self.child_pids.add(pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)

    def _detach_after_stopping(self, pid: int | None = None) -> dict:
        if pid is not None:
            self._kill_pid(pid)
        return retention._detach_session_workspace(self.root, self.workspace_identity)

    def _spawn_open_fd_child(self, *, setsid: bool = False, continuous: bool = False,
                             double_fork: bool = False) -> tuple[subprocess.Popen, int, Path]:
        target = self.workspace / "held-writable-file.bin"
        target.write_bytes(b"initial")
        pid_file = self.workspace / ".tmp" / f"child-{time.time_ns()}.pid"
        ready_file = self.workspace / ".tmp" / f"child-{time.time_ns()}.ready"
        if double_fork:
            script = r"""
import os,sys,time
target,pidfile,readyfile,continuous=sys.argv[1:]
first=os.fork()
if first:
    os.waitpid(first,0)
    raise SystemExit(0)
os.setsid()
second=os.fork()
if second:
    os._exit(0)
fd=os.open(target,os.O_RDWR)
with open(pidfile,'w') as f: f.write(str(os.getpid()))
with open(readyfile,'w') as f: f.write('ready')
while True:
    if continuous == 'yes':
        os.write(fd,b'W'*4096)
        os.fsync(fd)
    time.sleep(0.01)
"""
            args = (str(target), str(pid_file), str(ready_file), "yes" if continuous else "no")
        else:
            script = r"""
import os,sys,time
target,pidfile,readyfile,wantsid,continuous=sys.argv[1:]
if wantsid == 'yes': os.setsid()
fd=os.open(target,os.O_RDWR)
with open(pidfile,'w') as f: f.write(str(os.getpid()))
with open(readyfile,'w') as f: f.write('ready')
while True:
    if continuous == 'yes':
        os.write(fd,b'W'*4096)
        os.fsync(fd)
    time.sleep(0.01)
"""
            args = (str(target), str(pid_file), str(ready_file), "yes" if setsid else "no",
                    "yes" if continuous else "no")
        process = self._launch_sandboxed(script, *args)
        if double_fork:
            process.wait(timeout=5)
        self.assertEqual(self._wait_text(ready_file), "ready")
        child_pid = int(self._wait_text(pid_file))
        self.child_pids.add(child_pid)
        return process, child_pid, target

    def _signal_observer_code(self, source_path: Path, evidence_path: Path,
                              image_path: Path, result_fd: int) -> str:
        """Return a live setsid writer that reports attempts through a test-only pipe."""
        return rf"""
import errno,json,os,sys,time
workspace={str(self.workspace)!r}
marker=os.path.join(workspace,'.tmp','quiescence-marker')
image={str(image_path)!r}
source={str(source_path)!r}
evidence={str(evidence_path)!r}
outside={str(self.base / 'host-cache-probe')!r}
oracle_fd={result_fd}
child=os.fork()
if child:
    deadline=time.monotonic()+5
    ready=os.path.join(workspace,'.tmp','escaped-ready')
    while time.monotonic()<deadline and not os.path.exists(ready): time.sleep(.01)
    if not os.path.exists(ready): raise SystemExit(91)
    raise SystemExit(0)
os.setsid()
os.chdir('/')
for fd in range(0, min(256, os.sysconf('SC_OPEN_MAX'))):
    if fd == oracle_fd: continue
    try: os.close(fd)
    except OSError: pass
ready=os.path.join(workspace,'.tmp','escaped-ready')
fd=os.open(ready,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
os.write(fd,str(os.getpid()).encode()); os.close(fd)
absent_since=None
deadline=time.monotonic()+45
while time.monotonic()<deadline:
    if not os.path.exists(marker):
        absent_since=absent_since or time.monotonic()
        if time.monotonic()-absent_since>.15: break
    else: absent_since=None
    time.sleep(.002)
checks=[]
for path,flags in ((os.path.join(workspace,'after-detach-write'),os.O_WRONLY|os.O_CREAT|os.O_EXCL),
                   (image,os.O_WRONLY),(source,os.O_WRONLY),(evidence,os.O_WRONLY),
                   (outside,os.O_WRONLY|os.O_CREAT|os.O_EXCL)):
    try:
        opened=os.open(path,flags,0o600)
    except OSError as err:
        checks.append(err.errno)
    else:
        os.close(opened); checks.append(0)
expected=(errno.EACCES,errno.EPERM,errno.EPERM,errno.EPERM,errno.EPERM)
report={{'pid':os.getpid(),'mount_marker_absent':absent_since is not None,
        'write_errnos':checks,'all_writes_denied':tuple(checks)==expected}}
os.write(oracle_fd,json.dumps(report,sort_keys=True).encode())
time.sleep(3)
raise SystemExit(0 if report['mount_marker_absent'] and report['all_writes_denied'] else 93)
"""

    def _install_signal_observer(self):
        received: list[int] = []
        previous_usr1 = signal.getsignal(signal.SIGUSR1)
        previous_usr2 = signal.getsignal(signal.SIGUSR2)
        signal.signal(signal.SIGUSR1, lambda signum, _frame: received.append(signum))
        signal.signal(signal.SIGUSR2, lambda signum, _frame: received.append(signum))
        self.addCleanup(signal.signal, signal.SIGUSR1, previous_usr1)
        self.addCleanup(signal.signal, signal.SIGUSR2, previous_usr2)
        return received

    def test_01_normal_child_exit_allows_normal_unmount_and_detach(self) -> None:
        (self.workspace / "normal-child.txt").write_text("complete\n", encoding="utf-8")
        process = subprocess.run([sys.executable, "-c", "open('normal-child.txt','a').write('child\\n')"],
                                 cwd=self.workspace, check=False, timeout=10)
        self.assertEqual(process.returncode, 0)
        detached = retention._detach_session_workspace(self.root, self.workspace_identity)
        self.assertIs(detached["attached"], False)
        self.assertIsNone(retention._session_image_row(self.root))

    def test_02_child_surviving_its_parent_holds_the_mount_busy(self) -> None:
        target = self.workspace / "held-writable-file.bin"
        target.write_bytes(b"initial")
        pid_file = self.workspace / ".tmp/parent-exited-child.pid"
        ready_file = self.workspace / ".tmp/parent-exited-child.ready"
        code = r"""
import os,sys,time
target,pidfile,ready=sys.argv[1:]
child=os.fork()
if child:
    deadline=time.monotonic()+5
    while time.monotonic()<deadline and not os.path.exists(ready): time.sleep(.01)
    raise SystemExit(0 if os.path.exists(ready) else 91)
fd=os.open(target,os.O_RDWR)
with open(pidfile,'w') as f: f.write(str(os.getpid()))
with open(ready,'w') as f: f.write('ready')
time.sleep(60)
"""
        process = self._launch_sandboxed(code, str(target), str(pid_file), str(ready_file))
        process.wait(timeout=5)
        pid = int(self._wait_text(pid_file))
        self.assertEqual(process.poll(), 0)
        with self.assertRaisesRegex(retention.RetentionError, "normal unmount failed|normal detach failed"):
            retention._detach_session_workspace(self.root, self.workspace_identity)
        self._detach_after_stopping(pid)

    def test_03_setsid_descendant_with_writable_fd_blocks_unmount(self) -> None:
        _process, pid, _target = self._spawn_open_fd_child(setsid=True)
        with self.assertRaisesRegex(retention.RetentionError, "normal unmount failed|normal detach failed"):
            retention._detach_session_workspace(self.root, self.workspace_identity)
        self._detach_after_stopping(pid)

    def test_04_double_forked_reparented_descendant_blocks_unmount(self) -> None:
        process, pid, _target = self._spawn_open_fd_child(double_fork=True)
        self.assertEqual(process.poll(), 0)
        with self.assertRaisesRegex(retention.RetentionError, "normal unmount failed|normal detach failed"):
            retention._detach_session_workspace(self.root, self.workspace_identity)
        self._detach_after_stopping(pid)

    def test_05_continuous_writer_remains_active_during_busy_unmount(self) -> None:
        _process, pid, target = self._spawn_open_fd_child(setsid=True, continuous=True)
        time.sleep(0.1)
        before = target.stat().st_size
        with self.assertRaisesRegex(retention.RetentionError, "normal unmount failed|normal detach failed"):
            retention._detach_session_workspace(self.root, self.workspace_identity)
        time.sleep(0.1)
        after = target.stat().st_size
        self.assertGreater(after, before)
        self._detach_after_stopping(pid)

    def test_06_descendant_keeps_writable_file_descriptor_open(self) -> None:
        _process, pid, target = self._spawn_open_fd_child(setsid=True)
        fd = os.open(target, os.O_WRONLY)
        try:
            with self.assertRaisesRegex(retention.RetentionError, "normal unmount failed|normal detach failed"):
                retention._detach_session_workspace(self.root, self.workspace_identity)
        finally:
            os.close(fd)
        self._detach_after_stopping(pid)

    def test_07_descendant_keeps_writable_mmap_open(self) -> None:
        target = self.workspace / "mapped-file.bin"
        target.write_bytes(b"0" * 4096)
        pid_file = self.workspace / ".tmp" / "mmap.pid"
        ready_file = self.workspace / ".tmp" / "mmap.ready"
        code = r"""
import mmap,os,sys,time
path,pidfile,ready=sys.argv[1:]
fd=os.open(path,os.O_RDWR)
mapped=mmap.mmap(fd,4096,access=mmap.ACCESS_WRITE)
with open(pidfile,'w') as f: f.write(str(os.getpid()))
with open(ready,'w') as f: f.write('ready')
i=0
while True:
    mapped[0:1]=bytes([48+(i%10)]); mapped.flush(); i+=1; time.sleep(.01)
"""
        process = self._launch_sandboxed(code, str(target), str(pid_file), str(ready_file))
        self.assertEqual(self._wait_text(ready_file), "ready")
        pid = int(self._wait_text(pid_file))
        with self.assertRaisesRegex(retention.RetentionError, "normal unmount failed|normal detach failed"):
            retention._detach_session_workspace(self.root, self.workspace_identity)
        self._detach_after_stopping(pid)
        if process.poll() is None:
            process.kill()

    def test_08_nonroot_cwd_inside_workspace_blocks_detach(self) -> None:
        nested = self.workspace / "nested-cwd"
        nested.mkdir()
        pid_file = self.workspace / ".tmp/cwd.pid"
        code = "import os,sys,time; os.chdir(sys.argv[1]); open(sys.argv[2],'w').write(str(os.getpid())); time.sleep(60)"
        process = self._launch_sandboxed(code, str(nested), str(pid_file))
        pid = int(self._wait_text(pid_file))
        with self.assertRaisesRegex(retention.RetentionError, "normal unmount failed|normal detach failed"):
            retention._detach_session_workspace(self.root, self.workspace_identity)
        self._detach_after_stopping(pid)
        if process.poll() is None:
            process.kill()

    def test_09_executable_mapped_from_workspace_is_fail_closed_or_write_denied(self) -> None:
        signal_result = self._install_signal_observer()
        source = self.base / "mapped-sleeper.c"
        source.write_text(r"""
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
int main(int argc,char **argv) {
  const char *workspace=argv[1], *image=argv[2];
  char marker[4096], target[4096];
  snprintf(marker,sizeof(marker),"%s/.tmp/mapped-marker",workspace);
  snprintf(target,sizeof(target),"%s/.tmp/mapped-write",workspace);
  for (int i=0;i<5000 && access(marker,F_OK)==0;i++) usleep(10000);
  int fd=open(target,O_WRONLY|O_CREAT|O_EXCL,0600);
  int path_errno=fd<0?errno:0; if(fd>=0) close(fd);
  fd=open(image,O_WRONLY); int image_errno=fd<0?errno:0; if(fd>=0) close(fd);
  kill((pid_t)strtol(argv[3],NULL,10),
       path_errno==EACCES && image_errno==EPERM ? SIGUSR1 : SIGUSR2);
  sleep(2); return 0;
}
""", encoding="utf-8")
        binary = self.workspace / "mapped-executable"
        compile_result = subprocess.run(["/usr/bin/cc", str(source), "-o", str(binary)],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(compile_result.returncode, 0, compile_result.stderr.decode(errors="replace"))
        (self.workspace / ".tmp/mapped-marker").write_text("mounted", encoding="ascii")
        wrapped, env = self._sandboxed([str(binary), str(self.workspace), str(self.image), str(os.getpid())])
        process = subprocess.Popen(wrapped, cwd="/", env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   close_fds=True, pass_fds=())
        self.child_processes.append(process)
        self.child_pids.add(process.pid)
        time.sleep(0.1)
        try:
            detached = retention._detach_session_workspace(self.root, self.workspace_identity)
        except retention.RetentionError:
            process.kill()
            process.wait(timeout=5)
            self.assertIsNotNone(retention._session_image_row(self.root))
            self._detach_after_stopping()
        else:
            self.assertFalse(detached["attached"])
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and not signal_result:
                time.sleep(0.02)
            self.assertEqual(signal_result, [signal.SIGUSR1])
            process.wait(timeout=5)
            self.assertFalse((self.workspace / ".tmp/mapped-write").exists())

    def test_10_frozen_source_write_is_denied_by_real_seatbelt(self) -> None:
        source = self.base / "frozen-source.txt"
        source.write_text("frozen\n", encoding="utf-8")
        code = "import sys; open(sys.argv[1],'w').write('mutation')"
        wrapped, env = self._sandboxed([sys.executable, "-c", code, str(source)])
        result = subprocess.run(wrapped, cwd=self.workspace, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(source.read_text(encoding="utf-8"), "frozen\n")

    def test_11_protected_repository_evidence_write_is_denied(self) -> None:
        evidence = retention.REPO / "r6/provenance/R6-BASELINE-0-SOURCE-TO-BINARY.json"
        if not evidence.is_file():
            self.skipTest("the protected BUILD-1 source-to-binary receipt is absent in this checkout")
        before = retention._hash_path(evidence)
        code = "import os,sys; os.open(sys.argv[1],os.O_WRONLY)"
        wrapped, env = self._sandboxed([sys.executable, "-c", code, str(evidence)])
        result = subprocess.run(wrapped, cwd=self.workspace, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(retention._hash_path(evidence), before)

    def test_12_symlink_escape_is_rejected_and_seatbelt_blocks_target_write(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        protected = outside / "protected.txt"
        protected.write_text("outside\n", encoding="utf-8")
        (self.workspace / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(retention.RetentionError, "symlink"):
            retention._session_tree_digest(self.workspace)
        wrapped, env = self._sandboxed(["/usr/bin/touch", str(self.workspace / "escape" / "written.txt")])
        result = subprocess.run(wrapped, cwd=self.workspace, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((outside / "written.txt").exists())
        self.assertEqual(protected.read_text(encoding="utf-8"), "outside\n")

    def test_13_hardlink_escape_is_rejected_when_the_filesystem_allows_it(self) -> None:
        inside = self.workspace / "link-source.bin"
        inside.write_bytes(b"hardlink\n")
        outside = self.base / "outside-hardlink.bin"
        try:
            os.link(inside, outside)
        except OSError as error:
            self.assertEqual(error.errno, errno.EXDEV)
            return
        try:
            with self.assertRaisesRegex(retention.RetentionError, "hardlinks outside its tree"):
                retention._session_tree_digest(self.workspace)
        finally:
            outside.unlink(missing_ok=True)

    def test_14_controller_parent_can_exit_while_sandboxed_setsid_child_survives(self) -> None:
        code = r"""
import os,sys,time
workspace,pidfile,ready=sys.argv[1:]
child=os.fork()
if child:
    deadline=time.monotonic()+5
    while time.monotonic()<deadline and not os.path.exists(ready): time.sleep(.01)
    raise SystemExit(0 if os.path.exists(ready) else 91)
os.setsid(); os.chdir('/')
for fd in range(0,min(256,os.sysconf('SC_OPEN_MAX'))):
    try: os.close(fd)
    except OSError: pass
with open(pidfile,'w') as f: f.write(str(os.getpid()))
with open(ready,'w') as f: f.write('ready')
time.sleep(30)
"""
        pid_file = self.workspace / ".tmp/orphan.pid"
        ready_file = self.workspace / ".tmp/orphan.ready"
        process = self._launch_sandboxed(code, str(self.workspace), str(pid_file), str(ready_file))
        process.wait(timeout=5)
        pid = int(self._wait_text(pid_file))
        self.assertEqual(process.returncode, 0)
        os.kill(pid, 0)
        detached = retention._detach_session_workspace(self.root, self.workspace_identity)
        self.assertFalse(detached["attached"])
        os.kill(pid, 0)
        self._kill_pid(pid)

    def test_15_normal_unmount_busy_is_failure_and_never_uses_force(self) -> None:
        _process, pid, _target = self._spawn_open_fd_child(setsid=True)
        observed: list[list[str]] = []
        real_run = subprocess.run

        def record_run(command, *args, **kwargs):
            if isinstance(command, (list, tuple)) and command and command[0] in {
                    "/usr/bin/hdiutil", "/usr/sbin/diskutil"}:
                observed.append([str(item) for item in command])
            return real_run(command, *args, **kwargs)

        with mock.patch.object(retention.subprocess, "run", side_effect=record_run):
            with self.assertRaisesRegex(retention.RetentionError, "normal unmount failed|normal detach failed"):
                retention._detach_session_workspace(self.root, self.workspace_identity)
        self.assertTrue(observed)
        self.assertTrue(any(command[:3] == ["/usr/sbin/diskutil", "unmount", str(self.workspace)]
                            for command in observed))
        self.assertFalse(any(command[0] == "/usr/bin/hdiutil" and command[1] == "unmount"
                             for command in observed))
        self.assertFalse(any("-force" in command for command in observed))
        self._detach_after_stopping(pid)

    def test_16_root_cwd_fails_closed_and_normal_detach_failure_is_rejected(self) -> None:
        pid_file = self.workspace / ".tmp/root-cwd.pid"
        code = "import os,sys,time; os.chdir(sys.argv[1]); open(sys.argv[2],'w').write(str(os.getpid())); time.sleep(60)"
        process = self._launch_sandboxed(code, str(self.workspace), str(pid_file))
        pid = int(self._wait_text(pid_file))
        with self.assertRaisesRegex(retention.RetentionError, "normal (unmount|detach) failed"):
            retention._detach_session_workspace(self.root, self.workspace_identity)
        self.assertIsNotNone(retention._session_image_row(self.root))
        self._detach_after_stopping(pid)
        if process.poll() is None:
            process.kill()

        # Exercise a normal eject error after a real successful HFS+ unmount.
        # Darwin 27 has returned EBUSY at unmount for the live root-cwd case on
        # this host, so inject only the second operation's failure here.
        detach_root = self.base / "detach-failure-root"
        detach_identity = self._attach_fixture_workspace(detach_root)
        real_run = subprocess.run
        intercepted: list[list[str]] = []

        def fail_only_normal_detach(command, *args, **kwargs):
            argv = [str(item) for item in command] if isinstance(command, (list, tuple)) else []
            if len(argv) >= 3 and argv[0] == "/usr/sbin/diskutil" and argv[1] == "eject":
                intercepted.append(argv)
                return subprocess.CompletedProcess(command, 16, b"", b"injected busy detach")
            return real_run(command, *args, **kwargs)

        with mock.patch.object(retention.subprocess, "run", side_effect=fail_only_normal_detach):
            with self.assertRaisesRegex(retention.RetentionError, "normal detach failed"):
                retention._detach_session_workspace(detach_root, detach_identity)
        self.assertTrue(intercepted)
        self.assertFalse(any("-force" in argv for argv in intercepted))
        self.assertIsNotNone(retention._session_image_row(detach_root))
        self.assertIsNone(retention._session_image_entity(detach_root))
        # Fixture teardown retries only after the rejected failure assertion.

    def test_17_surviving_setid_descendant_cannot_write_after_successful_detach(self) -> None:
        source = self.base / "source-guard.txt"
        evidence = self.base / "evidence-guard.txt"
        source.write_text("source stays fixed\n", encoding="utf-8")
        evidence.write_text("evidence stays fixed\n", encoding="utf-8")
        (self.workspace / ".tmp/quiescence-marker").write_text("mounted", encoding="ascii")
        oracle_read, oracle_write = os.pipe()
        os.set_inheritable(oracle_write, True)
        code = self._signal_observer_code(source, evidence, self.image, oracle_write)
        wrapped, env = self._sandboxed([sys.executable, "-c", code])
        process = subprocess.Popen(wrapped, cwd=self.workspace, env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True,
                                   pass_fds=(oracle_write,))
        self.child_processes.append(process)
        self.assertEqual(process.wait(timeout=5), 0)
        os.close(oracle_write)
        ready = self.workspace / ".tmp/escaped-ready"
        pid = int(self._wait_text(ready))
        detached = retention._detach_session_workspace(self.root, self.workspace_identity)
        self.assertFalse(detached["attached"])
        import select
        readable, _, _ = select.select([oracle_read], [], [], 8)
        self.assertTrue(readable, "surviving setsid child did not report its post-detach write attempts")
        report = json.loads(os.read(oracle_read, 4096).decode("utf-8"))
        os.close(oracle_read)
        self.assertTrue(report["mount_marker_absent"])
        self.assertTrue(report["all_writes_denied"], report)
        self.assertEqual(report["write_errnos"], [errno.EACCES, errno.EPERM, errno.EPERM,
                                                    errno.EPERM, errno.EPERM])
        self.assertEqual(report["pid"], pid)
        os.kill(pid, 0)
        self.assertFalse((self.workspace / "after-detach-write").exists())
        self.assertEqual(source.read_text(encoding="utf-8"), "source stays fixed\n")
        self.assertEqual(evidence.read_text(encoding="utf-8"), "evidence stays fixed\n")
        self.assertEqual(retention._session_image_content_identity(self.image), detached["detached_image_identity"])
        self._kill_pid(pid)

    def test_18_exact_image_readonly_remount_succeeds(self) -> None:
        (self.workspace / "output.txt").write_text("accepted\n", encoding="utf-8")
        detached = retention._detach_session_workspace(self.root, self.workspace_identity)
        readonly = retention._attach_session_workspace_readonly(self.root, detached)
        self.assertTrue(readonly["readonly"])
        self.assertIs(retention._session_image_row(self.root)["writeable"], False)
        self.assertIs(retention._session_diskutil_info(readonly["device_node"])["Writable"], False)
        detached_ro = retention._detach_session_workspace_readonly(self.root, readonly)
        self.assertFalse(detached_ro["attached"])

    def test_19_write_to_readonly_remount_fails_with_erofs(self) -> None:
        output = self.workspace / "readonly.txt"
        output.write_text("frozen\n", encoding="utf-8")
        detached = retention._detach_session_workspace(self.root, self.workspace_identity)
        readonly = retention._attach_session_workspace_readonly(self.root, detached)
        with self.assertRaises(OSError) as caught:
            (self.workspace / "forbidden.txt").write_text("no\n", encoding="utf-8")
        self.assertEqual(caught.exception.errno, errno.EROFS)
        retention._detach_session_workspace_readonly(self.root, readonly)

    def test_20_readonly_output_hash_stays_stable_during_observation_interval(self) -> None:
        output = self.workspace / "stable-output.bin"
        output.write_bytes(b"stable output" * 8192)
        detached = retention._detach_session_workspace(self.root, self.workspace_identity)
        readonly = retention._attach_session_workspace_readonly(self.root, detached)
        before_tree = retention._session_tree_digest(self.workspace)
        before_image = retention._session_image_content_identity(self.image)
        time.sleep(0.3)
        after_tree = retention._session_tree_digest(self.workspace)
        after_image = retention._session_image_content_identity(self.image)
        self.assertEqual(before_tree, after_tree)
        self.assertEqual(before_image, after_image)
        retention._verify_session_workspace_readonly(self.root, readonly)
        retention._detach_session_workspace_readonly(self.root, readonly)

    def test_21_backing_image_substitution_is_rejected_before_readonly_attach(self) -> None:
        detached = retention._detach_session_workspace(self.root, self.workspace_identity)
        replacement_root = self.base / "replacement-root"
        replacement_identity = self._attach_fixture_workspace(replacement_root)
        replacement_detached = retention._detach_session_workspace(replacement_root, replacement_identity)
        self.assertIs(replacement_detached.get("attached"), False)
        self.assertIsNone(retention._session_image_row(replacement_root))
        self._remember_fixture_workspace_identity(replacement_root, replacement_detached)
        replacement_image = Path(replacement_detached["image_path"])
        replacement_copy = self.base / "replacement.sparseimage"
        shutil.copyfile(replacement_image, replacement_copy)
        original_image = self.base / "original-workspace.sparseimage"
        os.replace(self.image, original_image)
        os.replace(replacement_copy, self.image)
        try:
            with self.assertRaisesRegex(
                    retention.RetentionError,
                    "backing identity changed|contents changed|detached workspace or backing image identity changed") as caught:
                retention._attach_session_workspace_readonly(self.root, detached)
            self.assertIsInstance(caught.exception.__cause__, retention.RetentionError)
            self.assertIn("setup error:", str(caught.exception))
            self.assertIn("cleanup error:", str(caught.exception))
            self.assertIn("detached workspace or backing image identity changed",
                          str(caught.exception.__cause__))
            self.assertIsNone(retention._session_image_row(self.root))
            self.assertIsNone(retention._session_image_row(replacement_root))
        finally:
            # Restore the exact original inode once both owned images are proven
            # detached so fixture teardown can clean without trusting the substitute.
            if (original_image.exists()
                    and retention._session_image_row(self.root) is None
                    and retention._session_image_row(replacement_root) is None):
                self.image.unlink(missing_ok=True)
                os.replace(original_image, self.image)

    def test_22_controller_crash_before_detach_cannot_produce_accepted_output(self) -> None:
        # A real writable image remains attached while the durable session record is
        # moved to the interrupted phase. Recovery must reject and detach for cleanup,
        # never create a terminal SUCCESS or completion manifest.
        import json

        marker_fd = retention._open_dir(self.root)
        try:
            retention._write_marker(marker_fd, {"build_id": "crash-boundary", "state": "ACTIVE",
                                                "path": str(self.root)})
        finally:
            os.close(marker_fd)
        session_dir = self.root / retention.BUILD_SESSION_DIRECTORY
        session_dir.mkdir(mode=0o700)
        session_record = {"schema_version": retention.BUILD_SESSION_SCHEMA_VERSION,
                          "executor_mode": retention.BUILD_SESSION_EXECUTOR_MODE,
                          "executor_version": retention.BUILD_SESSION_SCHEMA_VERSION,
                          "state": "QUIESCING", "acceptance_state": "UNACCEPTED_UNTIL_NORMAL_UNMOUNT_AND_DETACH",
                          "write_quiescence_state": "WRITABLE_IMAGE_DETACH_IN_PROGRESS",
                          "workspace_mount_identity": self.workspace_identity}
        record = {"build_id": "crash-boundary", "state": "ACTIVE", "path": str(self.root),
                  "session_v3": session_record, "build_manifest_id": None}
        manifests = self.base / "manifests"
        manifests.mkdir(mode=0o700)
        with mock.patch.object(retention, "MANIFEST_ROOT", manifests), \
                mock.patch.object(retention, "_store_lease", side_effect=lambda _lease: None), \
                mock.patch.object(retention, "_require_no_live_process", return_value=None):
            retention._reject_interrupted_host_v3_session(record)
            self.assertEqual(record["state"], "FAILED")
            self.assertEqual(record["session_v3"]["acceptance_state"], "REJECTED_CONTROLLER_CRASH_UNKNOWN")
            self.assertFalse((session_dir / retention.BUILD_SESSION_TERMINAL).exists())
            self.assertFalse((manifests / "crash-boundary.json").exists())
            retention._recover_failed_session_workspace_detach(record)
            self.assertFalse(retention._session_image_row(self.root))
            self.assertEqual(record["session_v3"]["acceptance_state"], "REJECTED_NO_OUTPUT_PROMOTION")
            self.assertFalse((manifests / "crash-boundary.json").exists())

