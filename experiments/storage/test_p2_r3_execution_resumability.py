#!/usr/bin/env python3
"""Synthetic regression tests for P2 R3 resumability and descriptor scans."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

import p2_base_orchestrator as p2
import vm_capacity_domain as vm


def completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["lsof"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class DescriptorScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="p2-r3-descriptor-")
        self.root = pathlib.Path(self.temporary.name)
        self.target = self.root / "target.raw"
        self.target.write_bytes(b"stable target")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_stable_target_with_no_descriptors_passes(self) -> None:
        with mock.patch.object(p2.subprocess, "run", return_value=completed(1)):
            evidence = p2._descriptor_scan_evidence([self.target])
        self.assertTrue(evidence["scan_clear"])
        self.assertEqual(evidence["scan_status"], "NO_MATCHING_OPEN_DESCRIPTOR")
        self.assertEqual(evidence["open_file_descriptors"], [])

    def test_exact_historical_before_only_lsof_metadata_fixture_is_normalized(self) -> None:
        identity = p2._seal_file_identity(os.stat(self.target, follow_symlinks=False))
        historical_before = {"path": str(self.target), "file_identity": identity,
                             "lsof_device_number_hex": f"0x{identity['device']:x}"}
        historical_after = {"path": str(self.target), "file_identity": identity}
        self.assertNotEqual(historical_before, historical_after)
        self.assertEqual(p2._stable_descriptor_target_identity(historical_before),
                         p2._stable_descriptor_target_identity(historical_after))
        with mock.patch.object(p2.subprocess, "run", return_value=completed(1)):
            evidence = p2._descriptor_scan_evidence([self.target])
        self.assertTrue(evidence["scan_clear"])
        self.assertEqual(evidence["expected_file_identities_before_scan"],
                         evidence["expected_file_identities_after_scan"])
        self.assertEqual(evidence["expected_lsof_device_numbers_by_path"][str(self.target)],
                         f"0x{identity['device']:x}")

    def test_changed_device_inode_or_size_is_rejected(self) -> None:
        original = p2._seal_file_identity
        for field in ("device", "inode", "logical_bytes"):
            with self.subTest(field=field):
                calls = 0

                def seal(info, selected=field):
                    nonlocal calls
                    calls += 1
                    result = original(info)
                    if calls == 2:
                        result[selected] += 1
                    return result

                with (mock.patch.object(p2, "_seal_file_identity", side_effect=seal),
                      mock.patch.object(p2.subprocess, "run", return_value=completed(1))):
                    with self.assertRaisesRegex(RuntimeError, "identity changed"):
                        p2._descriptor_scan_evidence([self.target])

    def test_path_replacement_and_symlink_substitution_are_rejected(self) -> None:
        replacement = self.root / "replacement.raw"
        replacement.write_bytes(b"replacement")

        def replace_target(*_args, **_kwargs):
            os.replace(replacement, self.target)
            return completed(1)

        with mock.patch.object(p2.subprocess, "run", side_effect=replace_target):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                p2._descriptor_scan_evidence([self.target])

        self.target.write_bytes(b"stable again")
        other = self.root / "other.raw"
        other.write_bytes(b"other")

        def substitute_symlink(*_args, **_kwargs):
            self.target.unlink()
            self.target.symlink_to(other)
            return completed(1)

        with mock.patch.object(p2.subprocess, "run", side_effect=substitute_symlink):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                p2._descriptor_scan_evidence([self.target])

    def test_writable_descriptor_is_not_clear_and_unexpected_lsof_fails_closed(self) -> None:
        info = os.stat(self.target, follow_symlinks=False)
        output = (f"p99999\ncwriter\nf4\naw\nD0x{info.st_dev:x}\ntREG\n"
                  f"i{info.st_ino}\nk1\nn{self.target}\n")
        with mock.patch.object(p2.subprocess, "run", return_value=completed(0, output)):
            evidence = p2._descriptor_scan_evidence([self.target])
        self.assertFalse(evidence["scan_clear"])
        self.assertEqual(evidence["open_file_descriptors"][0]["files"][0]["access_mode"], "w")
        self.assertTrue(evidence["exact_target_descriptor_matches"][0]["exact_identity_match"])
        for result in (completed(2, stderr="lsof failed"), completed(1, stdout="unexpected"),
                       completed(1, stderr="diagnostic"), completed(0)):
            with self.subTest(result=(result.returncode, result.stdout, result.stderr)):
                with mock.patch.object(p2.subprocess, "run", return_value=result):
                    with self.assertRaises(RuntimeError):
                        p2._descriptor_scan_evidence([self.target])


class LaunchCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="p2-r3-capability-", dir=HERE)
        self.root = pathlib.Path(self.temporary.name) / "control-root"
        self.root.mkdir(mode=0o700)
        self.key = vm.policy._manifest_signing_key()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def contract(self, token: bytes | None = None):
        selected = token or os.urandom(32)
        session_id = uuid.uuid4().hex
        commitment = hashlib.sha256(selected).hexdigest()
        record = {
            "record_type": "R6_VM_P2_PROVISIONING_ADMISSION",
            "run_id": "synthetic-r3-" + uuid.uuid4().hex,
            "decision": "PASS", "authorized_phase": "P2",
            "authority_scope": "PROVISION_MACOS_GUEST_BASE_ONLY",
            "session_id": session_id, "helper_control_token_sha256": commitment,
            "launch_capability": {
                "format_version": p2.P2_LAUNCH_CAPABILITY_FORMAT_VERSION,
                "commitment_sha256": commitment, "session_id": session_id,
                "private_store": p2.P2_LAUNCH_CAPABILITY_STORE_ID,
                "path_derivation": "<session_id>/<admission_sha256>.ready",
                "state": "READY", "single_use": True,
            },
        }
        return record, selected

    def persist(self, token: bytes | None = None):
        record, selected = self.contract(token)
        digest, path = p2._prepare_admission_launch_capability(
            record, self.key, selected, control_root=self.root)
        return record, selected, digest, path

    def test_new_admission_private_file_and_signed_commitment(self) -> None:
        token = os.urandom(32)
        record, _ = self.contract(token)
        admission_path = pathlib.Path(self.temporary.name) / "admission.json"
        p2.signed_write(admission_path, record, "vm_admission_hmac_sha256", self.key)
        digest, capability_path = p2._prepare_admission_launch_capability(
            record, self.key, token, control_root=self.root)
        self.assertEqual(digest, vm._file_sha256(admission_path))
        info = capability_path.stat()
        self.assertEqual((info.st_mode & 0o777, info.st_uid, info.st_nlink, info.st_size),
                         (0o600, os.geteuid(), 1, 32))
        signed, actual_sha = p2.read_signed_with_key(admission_path, "vm_admission_hmac_sha256", self.key)
        inspected = p2._validate_launch_capability_contract(signed, actual_sha, control_root=self.root)
        self.assertEqual(inspected["status"], "READY")
        self.assertEqual(inspected["commitment_sha256"], hashlib.sha256(token).hexdigest())
        raw = admission_path.read_bytes()
        self.assertNotIn(token, raw)
        self.assertNotIn(token.hex().encode(), raw)
        self.assertNotIn(base64.b64encode(token), raw)

    def test_wrong_commitment_missing_capability_and_wrong_owner_fail(self) -> None:
        record, token, digest, path = self.persist()
        with self.assertRaises((RuntimeError, OSError)):
            p2._inspect_launch_capability(record["session_id"], digest,
                hashlib.sha256(os.urandom(32)).hexdigest(), control_root=self.root)
        path.unlink()
        with self.assertRaises((RuntimeError, OSError)):
            p2._validate_launch_capability_contract(record, digest, control_root=self.root)
        p2._write_launch_capability_ready(record["session_id"], digest, token, control_root=self.root)
        with self.assertRaisesRegex(RuntimeError, "owner"):
            p2._inspect_launch_capability(record["session_id"], digest,
                hashlib.sha256(token).hexdigest(), control_root=self.root,
                expected_uid=os.geteuid() + 1)

    def test_wrong_mode_symlink_hardlink_and_wrong_length_fail(self) -> None:
        for operation in ("mode", "symlink", "hardlink", "length"):
            with self.subTest(operation=operation):
                root = pathlib.Path(tempfile.mkdtemp(prefix="cap-case-", dir=self.temporary.name))
                record, token = self.contract()
                digest = hashlib.sha256(os.urandom(32)).hexdigest()
                path = p2._write_launch_capability_ready(record["session_id"], digest,
                                                         token, control_root=root)
                if operation == "mode":
                    path.chmod(0o644)
                elif operation == "symlink":
                    path.unlink()
                    target = root / "target"
                    target.write_bytes(token)
                    path.symlink_to(target)
                elif operation == "hardlink":
                    os.link(path, root / "extra-link")
                else:
                    path.write_bytes(token[:-1])
                with self.assertRaises((RuntimeError, OSError)):
                    p2._inspect_launch_capability(record["session_id"], digest,
                        hashlib.sha256(token).hexdigest(), control_root=root)

    def test_changed_bytes_wrong_admission_or_session_fail(self) -> None:
        record, token, digest, path = self.persist()
        changed = bytearray(path.read_bytes())
        changed[0] ^= 1
        path.write_bytes(changed)
        with self.assertRaisesRegex(RuntimeError, "commitment"):
            p2._inspect_launch_capability(record["session_id"], digest,
                hashlib.sha256(token).hexdigest(), control_root=self.root)
        path.unlink()
        p2._write_launch_capability_ready(record["session_id"], digest, token, control_root=self.root)
        with self.assertRaises((RuntimeError, OSError)):
            p2._inspect_launch_capability(record["session_id"], hashlib.sha256(b"other admission").hexdigest(),
                hashlib.sha256(token).hexdigest(), control_root=self.root)
        with self.assertRaises((RuntimeError, OSError)):
            p2._inspect_launch_capability(uuid.uuid4().hex, digest,
                hashlib.sha256(token).hexdigest(), control_root=self.root)

    def test_first_claim_succeeds_and_second_or_consumed_validation_fails(self) -> None:
        record, token, digest, path = self.persist()
        claimed, receipt = p2._claim_launch_capability(
            record["session_id"], digest, record["helper_control_token_sha256"], control_root=self.root)
        self.assertEqual(claimed, token)
        self.assertEqual(receipt["status"], "CLAIMED")
        self.assertFalse(path.exists())
        self.assertTrue(path.with_suffix(".claimed").exists())
        with self.assertRaises((RuntimeError, OSError)):
            p2._claim_launch_capability(record["session_id"], digest,
                record["helper_control_token_sha256"], control_root=self.root)
        with self.assertRaises((RuntimeError, OSError)):
            p2._validate_launch_capability_contract(record, digest, control_root=self.root)

    def test_only_one_concurrent_process_claims(self) -> None:
        record, token, digest, path = self.persist()
        child = "\n".join([
            "import pathlib, sys",
            f"sys.path.insert(0, {str(HERE)!r})",
            "import p2_base_orchestrator as p2",
            "root, session, digest, commitment = pathlib.Path(sys.argv[1]), *sys.argv[2:]",
            "try:",
            "    _token, claim = p2._claim_launch_capability(session, digest, commitment, control_root=root)",
            "    print('CLAIMED')",
            "except Exception:",
            "    print('REJECTED')",
        ])
        command = [sys.executable, "-c", child, str(self.root), record["session_id"], digest,
                   record["helper_control_token_sha256"]]
        first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        first_out, first_err = first.communicate(timeout=30)
        second_out, second_err = second.communicate(timeout=30)
        self.assertEqual((first.returncode, first_err), (0, ""))
        self.assertEqual((second.returncode, second_err), (0, ""))
        self.assertCountEqual((first_out.strip(), second_out.strip()), ("CLAIMED", "REJECTED"))
        self.assertFalse(path.exists())
        self.assertTrue(path.with_suffix(".claimed").exists())

    def test_existing_admission_does_not_regenerate_missing_capability(self) -> None:
        record, _token = self.contract()
        admission_path = pathlib.Path(self.temporary.name) / "existing-admission.json"
        p2.signed_write(admission_path, record, "vm_admission_hmac_sha256", self.key)
        original = p2.read_signed_with_key

        def validate(path, **_kwargs):
            signed, digest = original(path, "vm_admission_hmac_sha256", self.key)
            return p2._validate_launch_capability_contract(signed, digest, control_root=self.root)

        args = argparse.Namespace(admission=str(admission_path), validate_only=False)
        with (mock.patch.object(p2, "_validate_execution_entry", side_effect=validate),
              mock.patch.object(p2, "_prepare_admission_launch_capability") as regenerate,
              mock.patch.object(p2, "provision") as dispatch):
            with self.assertRaises((RuntimeError, OSError)):
                p2._resume_existing_admission(args, signing_key=self.key, control_root=self.root)
        regenerate.assert_not_called()
        dispatch.assert_not_called()
        self.assertFalse((self.root / "p2-launch-capabilities").exists())

    def test_separate_process_validates_without_issuing_lease_or_claiming(self) -> None:
        record, token = self.contract()
        admission_path = pathlib.Path(self.temporary.name) / "admit-only.json"
        p2.signed_write(admission_path, record, "vm_admission_hmac_sha256", self.key)
        digest = vm._file_sha256(admission_path)
        p2._write_launch_capability_ready(record["session_id"], digest, token, control_root=self.root)
        child = "\n".join([
            "import hashlib, json, pathlib, sys",
            f"sys.path.insert(0, {str(HERE)!r})",
            "import p2_base_orchestrator as p2",
            "import vm_capacity_domain as vm",
            "admission_path, control_root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])",
            "key = vm.policy._manifest_signing_key()",
            "def validate(path, **_kwargs):",
            "    admission, digest = p2.read_signed_with_key(path, 'vm_admission_hmac_sha256', key)",
            "    capability = p2._validate_launch_capability_contract(admission, digest, control_root=control_root)",
            "    return {'admission': admission, 'admission_path': pathlib.Path(path), 'admission_sha256': digest,",
            "            'manifest_path': pathlib.Path('/synthetic/manifest'), 'review_path': pathlib.Path('/synthetic/review'),",
            "            'launch_capability': capability, 'active_marker_sha256': 'd' * 64}",
            "p2._validate_execution_entry = validate",
            "args = type('Args', (), {'admission': str(admission_path), 'validate_only': True})()",
            "p2._resume_existing_admission(args, signing_key=key, control_root=control_root)",
        ])
        result = subprocess.run([sys.executable, "-c", child, str(admission_path), str(self.root)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("READY_BEFORE_ATOMIC_CLAIM", result.stdout)
        self.assertIn("REQUIRED_NOT_ISSUED", result.stdout)
        self.assertIn(digest, result.stdout)
        self.assertNotIn(token.hex(), result.stdout)
        self.assertNotIn(base64.b64encode(token).decode(), result.stdout)
        self.assertIsNotNone(next(self.root.rglob("*.ready"), None))
        self.assertIsNone(next(self.root.rglob("*.claimed"), None))
        self.assertIsNone(next(self.root.rglob("*execution-start*.json"), None))

    def test_historical_r2_pass_admission_cannot_enter_r3_execution(self) -> None:
        with mock.patch.object(p2, "_validate_launch_capability_contract") as inspect_capability:
            with self.assertRaisesRegex(RuntimeError, "bounded P2 provisioning authority"):
                p2._validate_execution_entry(p2.P2_R2_ADMISSION_PATH,
                    signing_key=self.key, control_root=self.root)
        inspect_capability.assert_not_called()

    def test_signed_write_syncs_parent_and_verifies_exact_readback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2-r3-signed-write-", dir=HERE) as temporary:
            path = pathlib.Path(temporary) / "signed.json"
            with mock.patch.object(p2.os, "fsync", wraps=os.fsync) as sync:
                written = p2.signed_write(path, {"record_type": "SYNTHETIC"},
                    "vm_admission_hmac_sha256", self.key)
            readback, digest = p2.read_signed_with_key(
                path, "vm_admission_hmac_sha256", self.key)
            self.assertEqual(readback, written)
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(sync.call_count, 2)


class ActiveMarkerLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = vm.policy._manifest_signing_key()

    def ancestry(self) -> dict[str, str]:
        return {
            "admission_path": str(p2.P2_R2_ADMISSION_PATH.relative_to(REPO)),
            "admission_sha256": p2.P2_R2_ADMISSION_SHA256,
            "run_id": p2.P2_R2_ADMISSION_PATH.stem.removeprefix("R6-VM-P2-ADMISSION-"),
            "session_id": p2.P2_R2_SESSION_ID,
            "execution_blocker_receipt_path": str(p2.P2_R2_EXECUTION_BLOCKER_PATH.relative_to(REPO)),
            "execution_blocker_receipt_sha256": p2.P2_R2_EXECUTION_BLOCKER_SHA256,
        }

    def test_only_one_pass_r3_admission_can_occupy_the_reused_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2-r3-admission-slot-", dir=REPO) as temporary:
            root = pathlib.Path(temporary)
            admission_path = root / "R6-VM-P2-ADMISSION-synthetic-r3.json"
            admission = {
                "record_type": "R6_VM_P2_PROVISIONING_ADMISSION",
                "run_id": "synthetic-r3-" + uuid.uuid4().hex,
                "session_id": p2.P2_R2_SESSION_ID,
                "decision": "PASS", "authorized_phase": "P2",
            }
            p2.signed_write(admission_path, admission,
                "vm_admission_hmac_sha256", self.key)
            digest = vm._file_sha256(admission_path)
            with mock.patch.object(p2, "REPO", root):
                with self.assertRaisesRegex(RuntimeError, "already occupies this marker lineage"):
                    p2._assert_single_r3_pass_admission(p2.P2_R2_SESSION_ID)
                p2._assert_single_r3_pass_admission(
                    p2.P2_R2_SESSION_ID, current_admission_path=admission_path,
                    current_admission_sha256=digest)

    def test_reused_session_is_lineage_only_and_new_admission_is_bound(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2-r3-marker-", dir=REPO) as temporary:
            marker_path = pathlib.Path(temporary) / "transition.json"
            admission_path = pathlib.Path(temporary) / "admission.json"
            run_id = "synthetic-r3-" + uuid.uuid4().hex
            before_sha, after_sha, admission_sha = "a" * 64, "a" * 64, "c" * 64
            ancestry = self.ancestry()
            admission = {"run_id": run_id, "session_id": p2.P2_R2_SESSION_ID,
                "historical_r2_ancestry": ancestry,
                "active_marker_before_sha256": before_sha,
                "active_marker_transition_receipt_path": str(marker_path.relative_to(REPO))}
            marker = {"session_id": p2.P2_R2_SESSION_ID, "volume_uuid": p2.VM_VOLUME_UUID,
                "state": "ACTIVE", "phase_id": "P2_AUTHORIZED",
                "p1_admission_sha256": p2.P1_ADMISSION_SHA256}
            transition = {
                "record_type": "R6_VM_P2_ACTIVE_MARKER_TRANSITION_RECONCILIATION",
                "run_id": run_id, "status": "PASS", "authority_scope": "NONE",
                "execution_authority": "NONE", "cross_session_transition_protocol_added": False,
                "transition_protocol": "no-op: retain the exact authenticated active marker; the new admission is separately bound to its digest",
                "session_id_role": "AUTHENTICATED_ACTIVE_MARKER_LINEAGE_ONLY",
                "new_admission_path": str(admission_path.relative_to(REPO)),
                "new_admission_sha256": admission_sha,
                "new_admission_session_id": p2.P2_R2_SESSION_ID,
                "historical_r2_ancestry": ancestry,
                "prior_active_marker": marker,
                "active_marker_before_sha256": before_sha,
                "resulting_active_marker": marker,
                "active_marker_after_sha256": after_sha,
            }
            p2.signed_write(marker_path, transition,
                            "vm_p2_active_marker_transition_hmac_sha256", self.key)
            with (mock.patch.object(p2, "unique_path", return_value=marker_path),
                  mock.patch.object(p2, "_validate_accepted_p1",
                    return_value={"historical_r2_boundary": {"verified": True}})):
                validated, _ = p2._validate_r3_active_marker_transition(
                    admission, admission_path, admission_sha,
                    {"active_marker_sha256": before_sha}, marker, after_sha,
                    signing_key=self.key)
            self.assertEqual(validated["session_id_role"], "AUTHENTICATED_ACTIVE_MARKER_LINEAGE_ONLY")
            self.assertFalse(validated["cross_session_transition_protocol_added"])

            for mutation in ("after_sha", "resulting_marker"):
                with self.subTest(mutation=mutation):
                    bad_path = pathlib.Path(temporary) / f"transition-{mutation}.json"
                    bad_admission = {**admission,
                        "active_marker_transition_receipt_path": str(bad_path.relative_to(REPO))}
                    bad_transition = {**transition}
                    if mutation == "after_sha":
                        bad_transition["active_marker_after_sha256"] = "b" * 64
                    else:
                        bad_transition["resulting_active_marker"] = {**marker,
                            "committed_growth_bytes": 123}
                    p2.signed_write(bad_path, bad_transition,
                        "vm_p2_active_marker_transition_hmac_sha256", self.key)
                    with (mock.patch.object(p2, "unique_path", return_value=bad_path),
                          mock.patch.object(p2, "_validate_accepted_p1",
                            return_value={"historical_r2_boundary": {"verified": True}})):
                        with self.assertRaisesRegex(RuntimeError, "does not bind the exact admission"):
                            p2._validate_r3_active_marker_transition(
                                bad_admission, admission_path, admission_sha,
                                {"active_marker_sha256": before_sha}, marker, after_sha,
                                signing_key=self.key)

    def test_r2_run_id_and_other_session_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2-r3-marker-", dir=REPO) as temporary:
            marker_path = pathlib.Path(temporary) / "missing-transition.json"
            ancestry = self.ancestry()
            cases = ((ancestry["run_id"], p2.P2_R2_SESSION_ID),
                     ("synthetic-r3-new", uuid.uuid4().hex))
            with (mock.patch.object(p2, "unique_path", return_value=marker_path),
                  mock.patch.object(p2, "_validate_accepted_p1",
                    return_value={"historical_r2_boundary": {"verified": True}})):
                for run_id, session_id in cases:
                    admission = {"run_id": run_id, "session_id": session_id,
                        "historical_r2_ancestry": ancestry,
                        "active_marker_before_sha256": "a" * 64,
                        "active_marker_transition_receipt_path": "missing-transition.json"}
                    with self.subTest(run_id=run_id, session_id=session_id):
                        with self.assertRaisesRegex(RuntimeError, "does not distinguish"):
                            p2._validate_r3_active_marker_transition(
                                admission, marker_path, "c" * 64,
                                {"active_marker_sha256": "a" * 64},
                                {"session_id": p2.P2_R2_SESSION_ID}, "b" * 64,
                                signing_key=self.key)


if __name__ == "__main__":
    unittest.main()
