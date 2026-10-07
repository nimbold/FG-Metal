"""Real filesystem tests of zero-copy references and transaction recovery."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import consolidate_evidence as ce


class EvidenceConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-reference-test-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "evidence"
        self.root.mkdir()
        self.source = self.root / "d3d11.dll"
        self.canonical = self.base / "canonical.dll"
        self.canonical.write_bytes(b"synthetic immutable evidence")
        self.digest = hashlib.sha256(self.canonical.read_bytes()).hexdigest()
        self.manifest = "a" * 64
        patcher = mock.patch.object(ce, "canonical_record", side_effect=self.record)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, digest):
        if hashlib.sha256(self.canonical.read_bytes()).hexdigest() != digest:
            raise ce.br.RetentionError("canonical digest mismatch")
        fd = os.open(self.canonical, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            identity = ce.identity(info, fd)
            link_count = info.st_nlink
        finally:
            os.close(fd)
        return self.canonical, {"identity": identity, "link_count": link_count,
                                "manifest_sha256": self.manifest,
                                "original_paths": [str(self.source)]}

    def row(self):
        fd = os.open(self.source, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            original = ce.identity(info, fd)
        finally:
            os.close(fd)
        canonical_fd = os.open(self.canonical, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            canonical_identity = ce.identity(os.fstat(canonical_fd), canonical_fd)
        finally:
            os.close(canonical_fd)
        return {"path": str(self.source), "sha256": self.digest,
                "canonical_path": str(self.canonical),
                "original_identity": original,
                "original_link_count": info.st_nlink,
                "canonical": {"identity": canonical_identity,
                              "manifest_sha256": self.manifest},
                "certification": "PRESERVE_OR_HARDLINK_DEDUP"}

    def test_hardlinked_evidence_becomes_zero_copy_reference(self):
        os.link(self.canonical, self.source)
        inode = self.canonical.stat().st_ino
        result = ce.replace_reference(self.row(), [self.root])
        self.assertEqual(result["copy_bytes"], 0)
        self.assertEqual(result["original_inode_link_count_before"], 2)
        self.assertEqual(result["original_inode_link_count_after"], 1)
        self.assertTrue(self.source.is_symlink())
        self.assertEqual(self.source.read_bytes(), self.canonical.read_bytes())
        self.assertEqual(self.canonical.stat().st_ino, inode)
        self.assertEqual(self.canonical.stat().st_nlink, 1)
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_each_transaction_phase_is_persisted_before_irreversible_unlink(self):
        self.source.write_bytes(self.canonical.read_bytes())
        persisted = []
        ce.replace_reference(self.row(), [self.root], lambda tx: persisted.append(dict(tx)))
        phases = [item["phase"] for item in persisted]
        self.assertEqual(phases[:4], ["PREPARED", "TEMP_READY", "SWAP_INTENT", "SWAPPED"])
        self.assertLess(phases.index("DELETE_INTENT"), phases.index("DELETED"))
        self.assertEqual(phases[-1], "COMPLETE")
        self.assertIn("temporary_identity", persisted[2])

    def test_hash_mismatch_does_not_remove_evidence(self):
        self.source.write_bytes(b"wrong bytes")
        with self.assertRaises(ce.br.RetentionError):
            ce.replace_reference(self.row(), [self.root])
        self.assertEqual(self.source.read_bytes(), b"wrong bytes")
        self.assertFalse(self.source.is_symlink())

    def test_failure_during_unlink_restores_original_and_removes_temporary(self):
        self.source.write_bytes(self.canonical.read_bytes())
        row = self.row()
        with mock.patch.object(ce.br, "_unlink_pinned_regular_at", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                ce.replace_reference(row, [self.root])
        self.assertFalse(self.source.is_symlink())
        self.assertEqual(self.source.read_bytes(), self.canonical.read_bytes())
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_restart_recovers_exchange_from_durable_swap_intent(self):
        self.source.write_bytes(self.canonical.read_bytes())
        row = self.row()
        persisted = []
        durable = ce.recover_transaction
        real_swap = ce.br._rename_swap_at

        def exchange_then_simulate_process_loss(directory_fd, first, second):
            real_swap(directory_fd, first, second)
            raise RuntimeError("simulated interruption after exchange")

        with mock.patch.object(ce.br, "_rename_swap_at", side_effect=exchange_then_simulate_process_loss), \
             mock.patch.object(ce, "recover_transaction", side_effect=RuntimeError("process stopped")):
            with self.assertRaisesRegex(RuntimeError, "process stopped"):
                ce.replace_reference(row, [self.root], lambda tx: persisted.append(dict(tx)))
        self.assertTrue(self.source.is_symlink())
        tx = persisted[-1]
        self.assertEqual(tx["phase"], "SWAP_INTENT")
        outcome = durable(tx, lambda state: persisted.append(dict(state)))
        self.assertEqual(outcome, "ROLLED_BACK")
        self.assertFalse(self.source.is_symlink())
        self.assertEqual(self.source.read_bytes(), self.canonical.read_bytes())
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_unlisted_hardlink_blocks_group_before_any_change(self):
        self.source.write_bytes(self.canonical.read_bytes())
        hidden_alias = self.root / "unlisted.dll"
        os.link(self.source, hidden_alias)
        row = self.row()
        with self.assertRaisesRegex(ce.br.RetentionError, "complete original hardlink group"):
            ce.validate_group_closure([row], [self.root])
        self.assertFalse(self.source.is_symlink())
        self.assertEqual(self.source.stat().st_ino, hidden_alias.stat().st_ino)

    def test_inventory_recertification_rejects_changed_row(self):
        self.source.write_bytes(self.canonical.read_bytes())
        row = self.row()
        inventory = {"large_objects": [{"path": str(self.source),
            "deletability_classification": "REQUIRED ACTIVE", "sha256": self.digest,
            "logical_size_bytes": row["original_identity"]["size"],
            "filesystem_device": row["original_identity"]["device"],
            "inode": row["original_identity"]["inode"]}]}
        with self.assertRaises(ce.br.RetentionError):
            ce._validate_inventory_row(row, inventory)

    def test_plan_rejects_duplicate_paths_before_row_processing(self):
        reviewed = {"schema_version": 2, "operation": ce.OPERATION,
                    "approved_roots": [str(self.root)],
                    "inventory_sha256": hashlib.sha256(b"inventory").hexdigest(),
                    "rows": [{"path": str(self.source)}, {"path": str(self.source)}]}
        inventory = {"large_objects": []}
        with mock.patch.object(ce.sp, "_verify_storage_evidence", return_value=True):
            with self.assertRaisesRegex(ce.br.RetentionError, "duplicate or invalid plan path"):
                ce.validate_reviewed_plan(reviewed, b"signed", inventory, b"inventory", [self.root])

    def test_bad_plan_signature_fails_closed(self):
        reviewed = {"rows": []}
        with mock.patch.object(ce.sp, "_verify_storage_evidence", return_value=False):
            with self.assertRaisesRegex(ce.br.RetentionError, "signature is missing or invalid"):
                ce.validate_reviewed_plan(reviewed, b"tampered", {}, b"inventory", [self.root])

    def test_manifest_must_bind_original_path(self):
        self.source.write_bytes(self.canonical.read_bytes())
        row = self.row()
        with mock.patch.object(ce, "canonical_record", return_value=(self.canonical, {
                "identity": row["canonical"]["identity"], "link_count": 1,
                "manifest_sha256": self.manifest, "original_paths": []})):
            with self.assertRaisesRegex(ce.br.RetentionError, "no longer certifies evidence path"):
                ce._validate_cas_binding(row)


if __name__ == "__main__":
    unittest.main()
