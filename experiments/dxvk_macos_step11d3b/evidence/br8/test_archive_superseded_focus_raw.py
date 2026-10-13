"""Offline regression fixtures for the BR8 raw-trace retirement tool."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("archive_superseded_focus_raw.py")
SPEC = importlib.util.spec_from_file_location("archive_superseded_focus_raw_fixture", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
retirement = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retirement)


def _make_archive(path: Path, member_name: str, payload: bytes) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                member = tarfile.TarInfo(member_name)
                member.type = tarfile.REGTYPE
                member.size = len(payload)
                member.mode = 0o600
                archive.addfile(member, io.BytesIO(payload))


def _record(member_name: str, payload: bytes) -> dict[str, object]:
    return {
        "archive_member": member_name,
        "source_relative_path": member_name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "identity": {"size": len(payload), "mode": 0o600, "mtime_ns": 0},
    }


class ArchiveRetirementSafetyFixtures(unittest.TestCase):
    def test_archive_path_swap_after_hash_is_rejected_after_parsing_held_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="br8-archive-swap-") as temporary:
            root = Path(temporary)
            archive_path = root / retirement.OUTPUTS["focus"][0]
            replacement_path = root / "replacement.tar.gz"
            original_member = "runs/original.jsonl"
            payload = b"original held archive bytes\n"
            _make_archive(archive_path, original_member, payload)
            replacement_path.write_bytes(b"not a tar archive")
            evidence_fd = os.open(root, retirement.DIR_FLAGS)
            archive_fd_before: os.stat_result | None = None
            original_hash = retirement._hash_fd
            replaced = False

            def hash_then_replace(fd: int) -> str:
                nonlocal replaced, archive_fd_before
                digest = original_hash(fd)
                if not replaced:
                    archive_fd_before = os.fstat(fd)
                    os.replace(replacement_path, archive_path)
                    replaced = True
                return digest

            try:
                with mock.patch.object(retirement, "_hash_fd", side_effect=hash_then_replace):
                    with self.assertRaisesRegex(retirement.GuardError, "archive changed during verification"):
                        retirement._verify_archive(
                            evidence_fd, archive_path.name, [_record(original_member, payload)]
                        )
                self.assertTrue(replaced)
                self.assertIsNotNone(archive_fd_before)
                self.assertEqual(archive_path.read_bytes(), b"not a tar archive")
            finally:
                os.close(evidence_fd)

    def _restore_fixture(self, *, replace_run: bool = False, replace_host: bool = False) -> None:
        run_id = retirement.FOCUS_CANDIDATES[0]["run_id"]
        source_relative = f"{retirement.RUNS_REL}/{run_id}/host-traces/host-trace.2627.jsonl"
        payload = b"offline restore fixture\n"
        with tempfile.TemporaryDirectory(prefix="br8-restore-parent-") as temporary:
            repo = Path(temporary)
            runs_path = repo.joinpath(*retirement.RUNS_REL.split("/"))
            evidence_path = repo.joinpath(*retirement.EVIDENCE_REL.split("/"))
            run_path = runs_path / run_id
            host_path = run_path / "host-traces"
            host_path.mkdir(parents=True)
            (run_path / "run-result.json").write_text('{"fixture":true}\n', encoding="utf-8")
            run_identity = retirement._identity(run_path.stat())
            host_identity = retirement._identity(host_path.stat())

            if replace_run:
                run_path.rename(runs_path / f"{run_id}.saved")
                (runs_path / run_id / "host-traces").mkdir(parents=True)
            elif replace_host:
                host_path.rename(run_path / "host-traces.saved")
                (run_path / "host-traces").mkdir()

            evidence_path.mkdir(parents=True)
            archive_name, _receipt_name = retirement.OUTPUTS["focus"]
            archive_path = evidence_path / archive_name
            _make_archive(archive_path, source_relative, payload)
            archive_stat = archive_path.stat()
            selected = [_record(source_relative, payload) | {"run_id": run_id}]
            first = {
                "event": "archive-verified-before-retirement",
                "scope": "focus",
                "archive_relative_path": f"{retirement.EVIDENCE_REL}/{archive_name}",
                "archive": {
                    "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                    "size": archive_stat.st_size,
                },
                "target_runs": [
                    {
                        "run_id": run_id,
                        "run_directory_identity": run_identity,
                        "host_traces_directory_identity": host_identity,
                    }
                ],
            }
            repo_fd = retirement._open_absolute_directory(str(repo.resolve()))
            evidence_fd = retirement._open_absolute_directory(str(evidence_path.resolve()))
            try:
                with mock.patch.object(retirement, "_load_receipt", return_value=(first, selected, {})):
                    expected_error = (
                        "restore run directory no longer matches"
                        if replace_run
                        else "restore host-traces directory no longer matches"
                    )
                    with self.assertRaisesRegex(retirement.GuardError, expected_error):
                        retirement._restore(repo_fd, evidence_fd, "focus", signer=object())
                restored_path = run_path / "host-traces" / "host-trace.2627.jsonl"
                self.assertFalse(restored_path.exists())
            finally:
                os.close(evidence_fd)
                os.close(repo_fd)

    def test_restore_refuses_replaced_run_directory(self) -> None:
        self._restore_fixture(replace_run=True)

    def test_restore_refuses_replaced_host_traces_directory(self) -> None:
        self._restore_fixture(replace_host=True)

    def test_initial_receipt_fsync_failure_preserves_source_paths_and_json(self) -> None:
        run_id = retirement.FOCUS_CANDIDATES[0]["run_id"]
        with tempfile.TemporaryDirectory(prefix="br8-receipt-fsync-") as temporary:
            root = Path(temporary)
            run_path = root / "run"
            evidence_path = root / "evidence"
            run_path.mkdir()
            evidence_path.mkdir()
            raw_path = run_path / "app-trace.jsonl"
            json_path = run_path / "run-result.json"
            raw_payload = b"raw fixture bytes\n"
            json_payload = b'{"status":"fixture-preserved"}\n'
            raw_path.write_bytes(raw_payload)
            json_path.write_bytes(json_payload)
            before_paths = sorted(p.relative_to(run_path).as_posix() for p in run_path.rglob("*"))
            before_stats = {p.name: retirement._identity(p.stat()) for p in (raw_path, json_path)}
            run_fd = retirement._open_absolute_directory(str(run_path.resolve()))
            evidence_fd = retirement._open_absolute_directory(str(evidence_path.resolve()))
            runs_fd = retirement._open_absolute_directory(str(root.resolve()))
            raw_fd = os.open(raw_path.name, retirement.READ_FLAGS, dir_fd=run_fd)
            raw_info = os.fstat(raw_fd)
            selected = [{
                "run_id": run_id,
                "parts": ("app-trace.jsonl",),
                "filename": raw_path.name,
                "parent_fd": run_fd,
                "fd": raw_fd,
                "source_relative_path": f"{retirement.RUNS_REL}/{run_id}/app-trace.jsonl",
                "archive_member": f"{retirement.RUNS_REL}/{run_id}/app-trace.jsonl",
                "sha256": hashlib.sha256(raw_payload).hexdigest(),
                "identity": retirement._identity(raw_info),
            }]
            target = {
                "run_id": run_id,
                "replacement_run_id": "fixture-replacement",
                "reviewed_retention_basis": ["fixture"],
                "candidate_result": {"sha256": "fixture"},
                "replacement_result": {"sha256": "fixture"},
                "assessment": {"sha256": "fixture"},
                "run_directory_identity": retirement._identity(os.fstat(run_fd)),
                "host_traces_directory_identity": retirement._identity(os.fstat(run_fd)),
                "files": {
                    ("app-trace.jsonl",): retirement._identity(raw_info),
                    ("run-result.json",): retirement._identity(json_path.stat()),
                },
                "directories": {},
                "selected_paths": {("app-trace.jsonl",): len(raw_payload)},
            }

            class FixtureSigner:
                @staticmethod
                def _seal_storage_evidence(body: dict[str, object], _domain: str) -> dict[str, object]:
                    return {**body, "fixture_signature": "offline"}

            original_create = retirement._create_outputs
            receipt_fd_holder: dict[str, int] = {}

            def capture_receipt_fd(directory_fd: int, scope: str):
                result = original_create(directory_fd, scope)
                receipt_fd_holder["fd"] = result[3]
                return result

            original_fsync = os.fsync
            failed_receipt_fsync = False

            def fail_initial_receipt_fsync(fd: int) -> None:
                nonlocal failed_receipt_fsync
                if fd == receipt_fd_holder.get("fd") and not failed_receipt_fsync:
                    failed_receipt_fsync = True
                    raise OSError("fixture receipt fsync failure")
                original_fsync(fd)

            try:
                with (
                    mock.patch.object(retirement, "_assert_no_active_retention", return_value={
                        "active_prefix_lease_markers": 0,
                        "active_build_lease_ids": [],
                    }),
                    mock.patch.object(retirement, "_create_outputs", side_effect=capture_receipt_fd),
                    mock.patch.object(retirement.os, "fsync", side_effect=fail_initial_receipt_fsync),
                ):
                    with self.assertRaisesRegex(retirement.GuardError, "stopped before source retirement"):
                        retirement._run_execute(
                            str(root),
                            evidence_fd,
                            runs_fd,
                            [target],
                            selected,
                            {run_id: run_fd},
                            "focus",
                            FixtureSigner(),
                        )
                self.assertTrue(failed_receipt_fsync)
                self.assertEqual(sorted(p.relative_to(run_path).as_posix() for p in run_path.rglob("*")), before_paths)
                self.assertEqual(raw_path.read_bytes(), raw_payload)
                self.assertEqual(json_path.read_bytes(), json_payload)
                self.assertEqual({p.name: retirement._identity(p.stat()) for p in (raw_path, json_path)}, before_stats)
                self.assertEqual(list(evidence_path.iterdir()), [])
            finally:
                for fd in (raw_fd, run_fd, evidence_fd, runs_fd):
                    try:
                        os.close(fd)
                    except OSError:
                        pass


if __name__ == "__main__":
    unittest.main()
