"""Focused fail-closed tests for the R71 dependency manifest primitives."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import r6a_dependency_manifest as dependency_manifest
import build_retention


class R71DependencyManifestTests(unittest.TestCase):
    def test_r87_phase_aware_candidate_uses_its_exact_manifest_and_binding_field(self) -> None:
        digest = "7" * 64
        for suffix, manifest in (
            (2, dependency_manifest.R87_MANIFEST_NAME),
            (3, dependency_manifest.R87_CANDIDATE_3_MANIFEST_NAME),
            (4, dependency_manifest.R87_CANDIDATE_4_MANIFEST_NAME),
            (5, dependency_manifest.R87_CANDIDATE_5_MANIFEST_NAME),
            (6, dependency_manifest.R87_CANDIDATE_6_MANIFEST_NAME),
            (7, dependency_manifest.R87_CANDIDATE_7_MANIFEST_NAME),
            (8, dependency_manifest.R87_CANDIDATE_8_MANIFEST_NAME),
            (9, dependency_manifest.R87_CANDIDATE_9_MANIFEST_NAME),
            (10, dependency_manifest.R87_CANDIDATE_10_MANIFEST_NAME),
            (11, dependency_manifest.R87_CANDIDATE_11_MANIFEST_NAME),
            (12, dependency_manifest.R87_CANDIDATE_12_MANIFEST_NAME),
            (13, dependency_manifest.R87_CANDIDATE_13_MANIFEST_NAME),
            (19, dependency_manifest.R87_CANDIDATE_19_MANIFEST_NAME),
            (18, dependency_manifest.R87_CANDIDATE_18_MANIFEST_NAME),
            (22, dependency_manifest.R87_CANDIDATE_22_MANIFEST_NAME),
            (26, dependency_manifest.R87_CANDIDATE_26_MANIFEST_NAME),
            (27, dependency_manifest.R87_CANDIDATE_27_MANIFEST_NAME),
        ):
            with self.subTest(candidate=suffix):
                candidate_id = f"R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-{suffix}"
                candidate_path = f"{candidate_id}.json"
                candidate = {
                    "candidate_id": candidate_id,
                    "milestone": "R87",
                    "r87_binding": {"execution_dependency_manifest": {
                        "path": manifest, "sha256": digest}},
                }
                candidate_core = dependency_manifest._candidate_core_sha256(candidate)
                review = {"status": "PASS", "findings": [], "r87_binding": {
                    "candidate": {"path": candidate_path, "candidate_id": candidate_id,
                                  "candidate_core_sha256": candidate_core},
                    "execution_dependency_manifest": {"path": manifest, "sha256": digest},
                }}
                identity = dependency_manifest.validate_candidate_g0c_binding(
                    candidate=candidate, g0c_review=review, candidate_path=candidate_path,
                    manifest_sha256=digest)
                self.assertEqual(identity["candidate_id"], candidate_id)
                self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate_id), manifest)
                self.assertIs(dependency_manifest._candidate_id_pattern(candidate_id),
                              dependency_manifest.R87_CANDIDATE_ID_PATTERN)
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / manifest).write_text("{}\n", encoding="utf-8")
                    self.assertEqual(
                        dependency_manifest.resolve_manifest_path(root, manifest).name, manifest)
        self.assertIsNone(dependency_manifest._candidate_id_pattern(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-1"))
        self.assertIs(dependency_manifest._candidate_id_pattern(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-13"),
            dependency_manifest.R87_CANDIDATE_ID_PATTERN)
        candidate14_id = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-14"
        self.assertIs(dependency_manifest._candidate_id_pattern(candidate14_id),
                      dependency_manifest.R87_CANDIDATE_ID_PATTERN)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate14_id),
                         dependency_manifest.R87_CANDIDATE_14_MANIFEST_NAME)
        candidate16_id = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-16"
        self.assertIs(dependency_manifest._candidate_id_pattern(candidate16_id),
                      dependency_manifest.R87_CANDIDATE_ID_PATTERN)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate16_id),
                         dependency_manifest.R87_CANDIDATE_16_MANIFEST_NAME)
        candidate17_id = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-17"
        self.assertIs(dependency_manifest._candidate_id_pattern(candidate17_id),
                      dependency_manifest.R87_CANDIDATE_ID_PATTERN)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate17_id),
                         dependency_manifest.R87_CANDIDATE_17_MANIFEST_NAME)
        candidate18_id = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-18"
        self.assertIs(dependency_manifest._candidate_id_pattern(candidate18_id),
                      dependency_manifest.R87_CANDIDATE_ID_PATTERN)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate18_id),
                         dependency_manifest.R87_CANDIDATE_18_MANIFEST_NAME)
        candidate19_id = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-19"
        self.assertIs(dependency_manifest._candidate_id_pattern(candidate19_id),
                      dependency_manifest.R87_CANDIDATE_ID_PATTERN)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate19_id),
                         dependency_manifest.R87_CANDIDATE_19_MANIFEST_NAME)
        self.assertEqual(dependency_manifest._binding_field(candidate),
                         dependency_manifest.R87_BINDING_FIELD)
        with self.assertRaises(dependency_manifest.DependencyManifestError):
            dependency_manifest.manifest_name_for_candidate(candidate_id + "-alias")
        candidate12_id = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-12"
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate12_id),
                         dependency_manifest.R87_CANDIDATE_12_MANIFEST_NAME)
        self.assertIs(dependency_manifest._candidate_id_pattern(candidate12_id),
                      dependency_manifest.R87_CANDIDATE_ID_PATTERN)
        self.assertEqual(dependency_manifest._manifest_envelope({
            "candidate_id": candidate12_id,
            "r87_binding": {"execution_dependency_manifest": {
                "path": dependency_manifest.R87_CANDIDATE_12_MANIFEST_NAME,
                "sha256": "7" * 64}},
        }, role="Candidate12")["path"], dependency_manifest.R87_CANDIDATE_12_MANIFEST_NAME)
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            for name in (
                    dependency_manifest.R87_CANDIDATE_12_MANIFEST_NAME,
                    dependency_manifest.R87_CANDIDATE_13_MANIFEST_NAME,
                    dependency_manifest.R87_CANDIDATE_14_MANIFEST_NAME,
                    dependency_manifest.R87_CANDIDATE_15_MANIFEST_NAME,
                    dependency_manifest.R87_CANDIDATE_16_MANIFEST_NAME,
                    dependency_manifest.R87_CANDIDATE_17_MANIFEST_NAME,
                    dependency_manifest.R87_CANDIDATE_18_MANIFEST_NAME,
                    dependency_manifest.R87_CANDIDATE_19_MANIFEST_NAME):
                (repo / name).write_text("{}\n", encoding="utf-8")
                self.assertEqual(
                    dependency_manifest.resolve_manifest_path(repo, name).name, name)

    def test_r94_candidate25_gets_fresh_exact_manifest_and_preserves_candidate24_history(self) -> None:
        candidate_ids_and_manifests = (
            ("R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-19",
             "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-19.json"),
            ("R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-20",
             "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-20.json"),
            ("R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-21",
             "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-21.json"),
            ("R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-22",
             "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-22.json"),
            ("R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-23",
             "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-23.json"),
            ("R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-24",
             "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-24.json"),
            ("R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-25",
             "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-25.json"),
        )
        digest = "9" * 64
        for candidate_id, manifest in candidate_ids_and_manifests:
            with self.subTest(candidate_id=candidate_id):
                candidate_path = f"{candidate_id}.json"
                candidate = {
                    "candidate_id": candidate_id,
                    "milestone": "R87",
                    "r87_binding": {"execution_dependency_manifest": {
                        "path": manifest, "sha256": digest}},
                }
                core = dependency_manifest._candidate_core_sha256(candidate)
                review = {"status": "PASS", "findings": [], "r87_binding": {
                    "candidate": {"path": candidate_path, "candidate_id": candidate_id,
                                  "candidate_core_sha256": core},
                    "execution_dependency_manifest": {"path": manifest, "sha256": digest},
                }}
                result = dependency_manifest.validate_candidate_g0c_binding(
                    candidate=candidate, g0c_review=review, candidate_path=candidate_path,
                    manifest_sha256=digest)
                self.assertEqual(result["candidate_id"], candidate_id)
                self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate_id),
                                 manifest)
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / manifest).write_text("{}\n", encoding="utf-8")
                    self.assertEqual(
                        dependency_manifest.resolve_manifest_path(root, manifest).name, manifest)

    def test_r87_candidate11_rejects_candidate10_manifest_cross_binding(self) -> None:
        candidate_id = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-11"
        candidate_path = f"{candidate_id}.json"
        digest = "8" * 64
        candidate = {
            "candidate_id": candidate_id,
            "milestone": "R87",
            "r87_binding": {"execution_dependency_manifest": {
                "path": dependency_manifest.R87_CANDIDATE_10_MANIFEST_NAME,
                "sha256": digest}},
        }
        candidate_core = dependency_manifest._candidate_core_sha256(candidate)
        review = {"status": "PASS", "findings": [], "r87_binding": {
            "candidate": {"path": candidate_path, "candidate_id": candidate_id,
                          "candidate_core_sha256": candidate_core},
            "execution_dependency_manifest": {
                "path": dependency_manifest.R87_CANDIDATE_10_MANIFEST_NAME,
                "sha256": digest},
        }}
        with self.assertRaisesRegex(
                dependency_manifest.DependencyManifestError,
                "candidate and G0C envelopes do not bind the exact manifest"):
            dependency_manifest.validate_candidate_g0c_binding(
                candidate=candidate, g0c_review=review, candidate_path=candidate_path,
                manifest_sha256=digest)

    def test_r87_candidate12_rejects_candidate11_manifest_cross_binding(self) -> None:
        candidate_id = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-12"
        candidate_path = f"{candidate_id}.json"
        digest = "9" * 64
        candidate = {
            "candidate_id": candidate_id,
            "milestone": "R87",
            "r87_binding": {"execution_dependency_manifest": {
                "path": dependency_manifest.R87_CANDIDATE_11_MANIFEST_NAME,
                "sha256": digest}},
        }
        core = dependency_manifest._candidate_core_sha256(candidate)
        review = {"status": "PASS", "findings": [], "r87_binding": {
            "candidate": {"path": candidate_path, "candidate_id": candidate_id,
                          "candidate_core_sha256": core},
            "execution_dependency_manifest": {
                "path": dependency_manifest.R87_CANDIDATE_11_MANIFEST_NAME,
                "sha256": digest},
        }}
        with self.assertRaisesRegex(
                dependency_manifest.DependencyManifestError,
                "candidate and G0C envelopes do not bind the exact manifest"):
            dependency_manifest.validate_candidate_g0c_binding(
                candidate=candidate, g0c_review=review, candidate_path=candidate_path,
                manifest_sha256=digest)

    def test_r75_candidate_uses_exact_fresh_manifest_and_binding_field(self) -> None:
        candidate_id = "R6-A-R75-G0C-EVIDENCE-SCHEMA-CANDIDATE-1"
        candidate_path = f"{candidate_id}.json"
        digest = "f" * 64
        manifest = dependency_manifest.R75_MANIFEST_NAME
        candidate = {
            "candidate_id": candidate_id,
            "milestone": "R75",
            "r75_binding": {"execution_dependency_manifest": {
                "path": manifest, "sha256": digest}},
        }
        core = dependency_manifest._candidate_core_sha256(candidate)
        review = {"status": "PASS", "findings": [], "r75_binding": {
            "candidate": {"path": candidate_path, "candidate_id": candidate_id,
                          "candidate_core_sha256": core},
            "execution_dependency_manifest": {"path": manifest, "sha256": digest},
        }}
        result = dependency_manifest.validate_candidate_g0c_binding(
            candidate=candidate, g0c_review=review, candidate_path=candidate_path,
            manifest_sha256=digest)
        self.assertEqual(result["candidate_id"], candidate_id)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate_id), manifest)
        self.assertIs(dependency_manifest._candidate_id_pattern(candidate_id),
                      dependency_manifest.R75_CANDIDATE_ID_PATTERN)
        self.assertIsNone(dependency_manifest._candidate_id_pattern(candidate_id + "-alias"))

    def test_r76_candidate_g0c_and_manifest_route_is_bound_and_alias_closed(self) -> None:
        candidate_id = "R6-A-R76-RUNNER-ROUTE-CANDIDATE-1"
        candidate_path = f"{candidate_id}.json"
        digest = "a" * 64
        manifest = dependency_manifest.R76_MANIFEST_NAME
        candidate = {
            "candidate_id": candidate_id,
            "milestone": "R76",
            "r76_binding": {"execution_dependency_manifest": {
                "path": manifest, "sha256": digest}},
        }
        core = dependency_manifest._candidate_core_sha256(candidate)
        review = {"status": "PASS", "findings": [], "r76_binding": {
            "candidate": {"path": candidate_path, "candidate_id": candidate_id,
                          "candidate_core_sha256": core},
            "execution_dependency_manifest": {"path": manifest, "sha256": digest},
        }}
        result = dependency_manifest.validate_candidate_g0c_binding(
            candidate=candidate, g0c_review=review, candidate_path=candidate_path,
            manifest_sha256=digest)
        self.assertEqual(result["candidate_id"], candidate_id)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate_id), manifest)
        self.assertIs(dependency_manifest._candidate_id_pattern(candidate_id),
                      dependency_manifest.R76_CANDIDATE_ID_PATTERN)
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / manifest).write_text("{}\n", encoding="utf-8")
            self.assertEqual(dependency_manifest.resolve_manifest_path(repo, manifest).name, manifest)
        for alias in (candidate_id + "-alias", "R6-A-R76-OTHER-CANDIDATE-1"):
            with self.subTest(alias=alias):
                self.assertIsNone(dependency_manifest._candidate_id_pattern(alias))
                with self.assertRaises(dependency_manifest.DependencyManifestError):
                    dependency_manifest.manifest_name_for_candidate(alias)

    def test_r77_candidate_g0c_and_manifest_route_is_bound_and_alias_closed(self) -> None:
        candidate_id = "R6-A-R77-GATE-DRIFT-DIAGNOSTIC-CANDIDATE-1"
        candidate_path = f"{candidate_id}.json"
        digest = "b" * 64
        manifest = dependency_manifest.R77_MANIFEST_NAME
        candidate = {
            "candidate_id": candidate_id,
            "milestone": "R77",
            "r77_binding": {"execution_dependency_manifest": {
                "path": manifest, "sha256": digest}},
        }
        core = dependency_manifest._candidate_core_sha256(candidate)
        review = {"status": "PASS", "findings": [], "r77_binding": {
            "candidate": {"path": candidate_path, "candidate_id": candidate_id,
                          "candidate_core_sha256": core},
            "execution_dependency_manifest": {"path": manifest, "sha256": digest},
        }}
        result = dependency_manifest.validate_candidate_g0c_binding(
            candidate=candidate, g0c_review=review, candidate_path=candidate_path,
            manifest_sha256=digest)
        self.assertEqual(result["candidate_id"], candidate_id)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate_id), manifest)
        self.assertIs(dependency_manifest._candidate_id_pattern(candidate_id),
                      dependency_manifest.R77_CANDIDATE_ID_PATTERN)
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / manifest).write_text("{}\n", encoding="utf-8")
            self.assertEqual(dependency_manifest.resolve_manifest_path(repo, manifest).name, manifest)
        for alias in (candidate_id + "-alias", "R6-A-R77-OTHER-CANDIDATE-1"):
            with self.subTest(alias=alias):
                self.assertIsNone(dependency_manifest._candidate_id_pattern(alias))
                with self.assertRaises(dependency_manifest.DependencyManifestError):
                    dependency_manifest.manifest_name_for_candidate(alias)

    def test_r78_candidate_g0c_and_manifest_route_is_bound_and_alias_closed(self) -> None:
        candidate_id = "R6-A-R78-BOUNDED-GATE-TRANSITION-CANDIDATE-1"
        candidate_path = f"{candidate_id}.json"
        digest = "c" * 64
        manifest = dependency_manifest.R78_MANIFEST_NAME
        candidate = {
            "candidate_id": candidate_id,
            "milestone": "R78",
            "r78_binding": {"execution_dependency_manifest": {
                "path": manifest, "sha256": digest}},
        }
        core = dependency_manifest._candidate_core_sha256(candidate)
        review = {"status": "PASS", "findings": [], "r78_binding": {
            "candidate": {"path": candidate_path, "candidate_id": candidate_id,
                          "candidate_core_sha256": core},
            "execution_dependency_manifest": {"path": manifest, "sha256": digest},
        }}
        result = dependency_manifest.validate_candidate_g0c_binding(
            candidate=candidate, g0c_review=review, candidate_path=candidate_path,
            manifest_sha256=digest)
        self.assertEqual(result["candidate_id"], candidate_id)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate_id), manifest)
        self.assertIs(dependency_manifest._candidate_id_pattern(candidate_id),
                      dependency_manifest.R78_CANDIDATE_ID_PATTERN)
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / manifest).write_text("{}\n", encoding="utf-8")
            self.assertEqual(dependency_manifest.resolve_manifest_path(repo, manifest).name, manifest)
        for alias in (candidate_id + "-alias", "R6-A-R78-OTHER-CANDIDATE-1"):
            with self.subTest(alias=alias):
                self.assertIsNone(dependency_manifest._candidate_id_pattern(alias))
                with self.assertRaises(dependency_manifest.DependencyManifestError):
                    dependency_manifest.manifest_name_for_candidate(alias)

    def test_r79_failed_output_candidate_uses_only_its_manifest_and_closed_id(self) -> None:
        candidate_id = "R6-A-R79-FAILED-OUTPUT-DISPOSITION-CANDIDATE-1"
        candidate_path = f"{candidate_id}.json"
        digest = "9" * 64
        manifest = dependency_manifest.R79_MANIFEST_NAME
        candidate = {
            "candidate_id": candidate_id,
            "milestone": "R79",
            "r79_binding": {"execution_dependency_manifest": {
                "path": manifest, "sha256": digest}},
        }
        core = dependency_manifest._candidate_core_sha256(candidate)
        review = {"status": "PASS", "findings": [], "r79_binding": {
            "candidate": {"path": candidate_path, "candidate_id": candidate_id,
                          "candidate_core_sha256": core},
            "execution_dependency_manifest": {"path": manifest, "sha256": digest},
        }}
        result = dependency_manifest.validate_candidate_g0c_binding(
            candidate=candidate, g0c_review=review, candidate_path=candidate_path,
            manifest_sha256=digest)
        self.assertEqual(result["candidate_id"], candidate_id)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate_id), manifest)
        self.assertIs(dependency_manifest._candidate_id_pattern(candidate_id),
                      dependency_manifest.R79_CANDIDATE_ID_PATTERN)
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / manifest).write_text("{}\n", encoding="utf-8")
            self.assertEqual(dependency_manifest.resolve_manifest_path(repo, manifest).name, manifest)
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "canonical repository-relative filename"):
                dependency_manifest.resolve_manifest_path(
                    repo, "R6-A-R79-EXECUTION-DEPENDENCY-MANIFEST.json.bak")
        for alias in (candidate_id + "-alias", "R6-A-R79-FAILED-OUTPUT-DISPOSITION-CANDIDATE-2"):
            with self.subTest(alias=alias):
                self.assertIsNone(dependency_manifest._candidate_id_pattern(alias))
                with self.assertRaises(dependency_manifest.DependencyManifestError):
                    dependency_manifest.manifest_name_for_candidate(alias)

    def test_r80_materialized_prefixlease_candidate_uses_fresh_manifest_and_closed_id(self) -> None:
        candidate_id = "R6-A-R80-POST-MATERIALIZATION-PREFIXLEASE-CANDIDATE-1"
        candidate_path = f"{candidate_id}.json"
        digest = "8" * 64
        manifest = dependency_manifest.R80_MANIFEST_NAME
        candidate = {
            "candidate_id": candidate_id,
            "milestone": "R80",
            "r80_binding": {"execution_dependency_manifest": {
                "path": manifest, "sha256": digest}},
        }
        core = dependency_manifest._candidate_core_sha256(candidate)
        review = {"status": "PASS", "findings": [], "r80_binding": {
            "candidate": {"path": candidate_path, "candidate_id": candidate_id,
                          "candidate_core_sha256": core},
            "execution_dependency_manifest": {"path": manifest, "sha256": digest},
        }}
        result = dependency_manifest.validate_candidate_g0c_binding(
            candidate=candidate, g0c_review=review, candidate_path=candidate_path,
            manifest_sha256=digest)
        self.assertEqual(result["candidate_id"], candidate_id)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate_id), manifest)
        self.assertIs(dependency_manifest._candidate_id_pattern(candidate_id),
                      dependency_manifest.R80_CANDIDATE_ID_PATTERN)
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / manifest).write_text("{}\n", encoding="utf-8")
            self.assertEqual(dependency_manifest.resolve_manifest_path(repo, manifest).name, manifest)
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "canonical repository-relative filename"):
                dependency_manifest.resolve_manifest_path(
                    repo, "R6-A-R80-EXECUTION-DEPENDENCY-MANIFEST.json.bak")
        for alias in (candidate_id + "-alias",
                      "R6-A-R80-POST-MATERIALIZATION-PREFIXLEASE-CANDIDATE-2",
                      "R6-A-R80-FAILED-OUTPUT-DISPOSITION-CANDIDATE-1"):
            with self.subTest(alias=alias):
                self.assertIsNone(dependency_manifest._candidate_id_pattern(alias))
                with self.assertRaises(dependency_manifest.DependencyManifestError):
                    dependency_manifest.manifest_name_for_candidate(alias)

    def test_r81_smoke_publication_candidate_uses_exact_manifest_and_closed_id(self) -> None:
        candidate_id = "R6-A-R81-SMOKE-MANIFEST-PUBLICATION-CANDIDATE-1"
        candidate_path = f"{candidate_id}.json"
        digest = "9" * 64
        manifest = dependency_manifest.R81_MANIFEST_NAME
        candidate = {
            "candidate_id": candidate_id, "milestone": "R81",
            "r81_binding": {"execution_dependency_manifest": {
                "path": manifest, "sha256": digest}},
        }
        core = dependency_manifest._candidate_core_sha256(candidate)
        review = {"status": "PASS", "findings": [], "r81_binding": {
            "candidate": {"path": candidate_path, "candidate_id": candidate_id,
                          "candidate_core_sha256": core},
            "execution_dependency_manifest": {"path": manifest, "sha256": digest},
        }}
        result = dependency_manifest.validate_candidate_g0c_binding(
            candidate=candidate, g0c_review=review, candidate_path=candidate_path,
            manifest_sha256=digest)
        self.assertEqual(result["candidate_id"], candidate_id)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate_id), manifest)
        self.assertIs(dependency_manifest._candidate_id_pattern(candidate_id),
                      dependency_manifest.R81_CANDIDATE_ID_PATTERN)
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / manifest).write_text("{}\n", encoding="utf-8")
            self.assertEqual(dependency_manifest.resolve_manifest_path(repo, manifest).name,
                             manifest)
        for alias in (candidate_id + "-alias",
                      "R6-A-R81-SMOKE-MANIFEST-PUBLICATION-CANDIDATE-2"):
            with self.subTest(alias=alias):
                self.assertIsNone(dependency_manifest._candidate_id_pattern(alias))
                with self.assertRaises(dependency_manifest.DependencyManifestError):
                    dependency_manifest.manifest_name_for_candidate(alias)

    def test_r86_canonical_regression_candidate_uses_exact_manifest_and_closed_id(self) -> None:
        candidate_id = "R6-A-R86-CANONICAL-REGRESSION-REPAIR-CANDIDATE-1"
        candidate_path = f"{candidate_id}.json"
        digest = "8" * 64
        manifest = dependency_manifest.R86_MANIFEST_NAME
        candidate = {
            "candidate_id": candidate_id, "milestone": "R86",
            "r86_binding": {"execution_dependency_manifest": {
                "path": manifest, "sha256": digest}},
        }
        core = dependency_manifest._candidate_core_sha256(candidate)
        review = {"status": "PASS", "findings": [], "r86_binding": {
            "candidate": {"path": candidate_path, "candidate_id": candidate_id,
                          "candidate_core_sha256": core},
            "execution_dependency_manifest": {"path": manifest, "sha256": digest},
        }}
        result = dependency_manifest.validate_candidate_g0c_binding(
            candidate=candidate, g0c_review=review, candidate_path=candidate_path,
            manifest_sha256=digest)
        self.assertEqual(result["candidate_id"], candidate_id)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate_id), manifest)
        self.assertIs(dependency_manifest._candidate_id_pattern(candidate_id),
                      dependency_manifest.R86_CANDIDATE_ID_PATTERN)
        self.assertIs(dependency_manifest._candidate_id_pattern(
            "R6-A-R81-SMOKE-MANIFEST-PUBLICATION-CANDIDATE-1"),
            dependency_manifest.R81_CANDIDATE_ID_PATTERN)
        self.assertEqual(dependency_manifest._binding_field({
            "r81_binding": {}, "r86_binding": {}}), "r86_binding")
        self.assertEqual(dependency_manifest._binding_field(candidate), "r86_binding")
        self.assertEqual(dependency_manifest._binding_field(review), "r86_binding")
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / manifest).write_text("{}\n", encoding="utf-8")
            self.assertEqual(dependency_manifest.resolve_manifest_path(repo, manifest).name,
                             manifest)
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "canonical repository-relative filename"):
                dependency_manifest.resolve_manifest_path(
                    repo, "R6-A-R86-EXECUTION-DEPENDENCY-MANIFEST.json.bak")
        for alias in (candidate_id + "-alias",
                      "R6-A-R86-CANONICAL-REGRESSION-REPAIR-CANDIDATE-2"):
            with self.subTest(alias=alias):
                self.assertIsNone(dependency_manifest._candidate_id_pattern(alias))
                with self.assertRaises(dependency_manifest.DependencyManifestError):
                    dependency_manifest.manifest_name_for_candidate(alias)

        wrong_manifest_candidate = dict(candidate)
        wrong_manifest_candidate["r86_binding"] = {
            "execution_dependency_manifest": {
                "path": dependency_manifest.R81_MANIFEST_NAME, "sha256": digest}}
        with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                    "exact manifest"):
            dependency_manifest.validate_candidate_g0c_binding(
                candidate=wrong_manifest_candidate, g0c_review=review,
                candidate_path=candidate_path, manifest_sha256=digest)

    def test_r74_candidate_uses_its_exact_fresh_manifest_and_rejects_aliases(self) -> None:
        candidate_id = "R6-A-R74-HISTORICAL-OUTPUT-ACCOUNTING-CANDIDATE-1"
        candidate_path = f"{candidate_id}.json"
        digest = "e" * 64
        r74_manifest = dependency_manifest.R74_MANIFEST_NAME
        candidate = {
            "candidate_id": candidate_id,
            "milestone": "R74",
            "r74_binding": {"execution_dependency_manifest": {
                "path": r74_manifest, "sha256": digest}},
        }
        candidate_core = dependency_manifest._candidate_core_sha256(candidate)
        review = {"status": "PASS", "findings": [], "r74_binding": {
            "candidate": {"path": candidate_path, "candidate_id": candidate_id,
                          "candidate_core_sha256": candidate_core},
            "execution_dependency_manifest": {"path": r74_manifest, "sha256": digest},
        }}

        accepted = dependency_manifest.validate_candidate_g0c_binding(
            candidate=candidate, g0c_review=review, candidate_path=candidate_path,
            manifest_sha256=digest)
        self.assertEqual(accepted["candidate_id"], candidate_id)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate_id),
                         r74_manifest)
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / r74_manifest).write_text("{}\n", encoding="utf-8")
            self.assertEqual(dependency_manifest.resolve_manifest_path(repo, r74_manifest).name,
                             r74_manifest)

        for alias in (
                "R6-A-R74-HISTORICAL-OUTPUT-ACCOUNTING-CANDIDATE-2",
                "R6-A-R74-HISTORICAL-OUTPUT-ACCOUNTING-CANDIDATE-1-ALIAS"):
            with self.subTest(alias=alias):
                self.assertIsNone(dependency_manifest._candidate_id_pattern(alias))
                with self.assertRaises(dependency_manifest.DependencyManifestError):
                    dependency_manifest.manifest_name_for_candidate(alias)

        candidate["r74_binding"]["execution_dependency_manifest"]["path"] = (
            dependency_manifest.R73_MANIFEST_NAME)
        review["r74_binding"]["execution_dependency_manifest"]["path"] = (
            dependency_manifest.R73_MANIFEST_NAME)
        with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                    "do not bind the exact manifest"):
            dependency_manifest.validate_candidate_g0c_binding(
                candidate=candidate, g0c_review=review, candidate_path=candidate_path,
                manifest_sha256=digest)

    def test_r73_candidate_uses_its_exact_fresh_manifest_and_rejects_aliases(self) -> None:
        candidate_id = "R6-A-R73-60GIB-POLICY-MIGRATION-CANDIDATE-1"
        candidate_path = f"{candidate_id}.json"
        digest = "d" * 64
        r73_manifest = dependency_manifest.R73_MANIFEST_NAME
        candidate = {
            "candidate_id": candidate_id,
            "milestone": "R73",
            "r73_binding": {"execution_dependency_manifest": {
                "path": r73_manifest, "sha256": digest}},
        }
        candidate_core = dependency_manifest._candidate_core_sha256(candidate)
        review = {"status": "PASS", "findings": [], "r73_binding": {
            "candidate": {"path": candidate_path, "candidate_id": candidate_id,
                          "candidate_core_sha256": candidate_core},
            "execution_dependency_manifest": {"path": r73_manifest, "sha256": digest},
        }}
        accepted = dependency_manifest.validate_candidate_g0c_binding(
            candidate=candidate, g0c_review=review, candidate_path=candidate_path,
            manifest_sha256=digest)
        self.assertEqual(accepted["candidate_id"], candidate_id)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate_id), r73_manifest)
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / r73_manifest).write_text("{}\n", encoding="utf-8")
            self.assertEqual(dependency_manifest.resolve_manifest_path(repo, r73_manifest).name,
                             r73_manifest)

        for alias in (
                "R6-A-R73-60GIB-POLICY-MIGRATION-CANDIDATE-2",
                "R6-A-R73-60GIB-POLICY-MIGRATION-CANDIDATE-1-ALIAS"):
            with self.subTest(alias=alias):
                self.assertIsNone(dependency_manifest._candidate_id_pattern(alias))
                with self.assertRaises(dependency_manifest.DependencyManifestError):
                    dependency_manifest.manifest_name_for_candidate(alias)
        with tempfile.TemporaryDirectory() as alias_temporary:
            alias_repo = Path(alias_temporary)
            for path_alias in ("R6-A-R73-EXECUTION-DEPENDENCY-MANIFEST.json.bak",):
                with self.subTest(path_alias=path_alias):
                    with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                                "canonical repository-relative filename"):
                        dependency_manifest.resolve_manifest_path(alias_repo, path_alias)

        r72_envelope = {"path": dependency_manifest.R72_MANIFEST_NAME, "sha256": digest}
        candidate_with_r72_manifest = {
            **candidate,
            "r73_binding": {**candidate["r73_binding"],
                            "execution_dependency_manifest": r72_envelope},
        }
        review_with_r72_manifest = {
            **review,
            "r73_binding": {**review["r73_binding"],
                            "execution_dependency_manifest": r72_envelope},
        }
        with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                    "do not bind the exact manifest"):
            dependency_manifest.validate_candidate_g0c_binding(
                candidate=candidate_with_r72_manifest,
                g0c_review=review_with_r72_manifest,
                candidate_path=candidate_path, manifest_sha256=digest)

    def test_r72_candidate_uses_fresh_manifest_and_cannot_bypass_core_binding(self) -> None:
        candidate_id = "R6-A-R72-CAPACITY-RECALIBRATION-CANDIDATE-1"
        candidate_path = f"{candidate_id}.json"
        digest = "c" * 64
        r72_manifest = dependency_manifest.R72_MANIFEST_NAME
        candidate = {
            "candidate_id": candidate_id,
            "policy": {"project_budget_bytes": 60 * 1024 ** 3},
            "r72_binding": {"execution_dependency_manifest": {
                "path": r72_manifest, "sha256": digest}},
        }
        candidate_core = dependency_manifest._candidate_core_sha256(candidate)
        review = {"status": "PASS", "findings": [], "r72_binding": {
            "candidate": {"path": candidate_path, "candidate_id": candidate_id,
                          "candidate_core_sha256": candidate_core},
            "execution_dependency_manifest": {"path": r72_manifest, "sha256": digest},
        }}

        accepted = dependency_manifest.validate_candidate_g0c_binding(
            candidate=candidate, g0c_review=review, candidate_path=candidate_path,
            manifest_sha256=digest)
        self.assertEqual(accepted["candidate_core_sha256"], candidate_core)
        self.assertEqual(dependency_manifest.manifest_name_for_candidate(candidate_id),
                         r72_manifest)

        # An R72 identity cannot point at or replay the historical R71 manifest.
        candidate["r72_binding"]["execution_dependency_manifest"]["path"] = (
            dependency_manifest.MANIFEST_NAME)
        review["r72_binding"]["execution_dependency_manifest"]["path"] = (
            dependency_manifest.MANIFEST_NAME)
        with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                    "do not bind the exact manifest"):
            dependency_manifest.validate_candidate_g0c_binding(
                candidate=candidate, g0c_review=review, candidate_path=candidate_path,
                manifest_sha256=digest)

        candidate["r72_binding"]["execution_dependency_manifest"]["path"] = r72_manifest
        review["r72_binding"]["execution_dependency_manifest"]["path"] = r72_manifest
        candidate["policy"]["project_budget_bytes"] += 1
        with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                    "does not bind the exact candidate core"):
            dependency_manifest.validate_candidate_g0c_binding(
                candidate=candidate, g0c_review=review, candidate_path=candidate_path,
                manifest_sha256=digest)

    def test_legacy_execution_candidate_name_is_rejected(self) -> None:
        candidate_id = "R6-A-R71-EXECUTION-CANDIDATE-1"
        digest = "a" * 64
        candidate = {
            "candidate_id": candidate_id,
            "r71_binding": {"execution_dependency_manifest": {
                "path": dependency_manifest.MANIFEST_NAME, "sha256": digest}},
        }
        review = {
            "r71_binding": {
                "candidate": {
                    "path": f"{candidate_id}.json", "candidate_id": candidate_id,
                    "candidate_core_sha256": dependency_manifest._candidate_core_sha256(candidate),
                },
                "execution_dependency_manifest": {
                    "path": dependency_manifest.MANIFEST_NAME, "sha256": digest},
            },
        }
        with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                    "candidate_id has no canonical dependency manifest"):
            dependency_manifest.validate_candidate_g0c_binding(
                candidate=candidate, g0c_review=review,
                candidate_path=f"{candidate_id}.json", manifest_sha256=digest)

    def test_candidate_and_g0c_envelopes_bind_manifest_and_g0c_binds_candidate_core(self) -> None:
        candidate_id = "R6-A-R71-PROVENANCE-CAPACITY-CANDIDATE-1"
        digest = "a" * 64
        candidate = {
            "candidate_id": candidate_id,
            "scope": "frozen inputs",
            "r71_binding": {"execution_dependency_manifest": {
                "path": dependency_manifest.MANIFEST_NAME, "sha256": digest}},
        }
        candidate_core = dependency_manifest._candidate_core_sha256(candidate)
        review = {
            "status": "PASS", "findings": [],
            "r71_binding": {
                "candidate": {"path": f"{candidate_id}.json", "candidate_id": candidate_id,
                              "candidate_core_sha256": candidate_core},
                "execution_dependency_manifest": {
                    "path": dependency_manifest.MANIFEST_NAME, "sha256": digest},
            },
        }
        accepted = dependency_manifest.validate_candidate_g0c_binding(
            candidate=candidate, g0c_review=review,
            candidate_path=f"{candidate_id}.json", manifest_sha256=digest)
        self.assertEqual(accepted["candidate_core_sha256"], candidate_core)
        core_before = dependency_manifest._g0c_core_sha256(review)
        replayed_candidate = json.loads(json.dumps(candidate))
        replayed_candidate["candidate_id"] = "R6-A-R71-PROVENANCE-CAPACITY-CANDIDATE-2"
        with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                    "candidate_id does not match"):
            dependency_manifest.validate_candidate_g0c_binding(
                candidate=replayed_candidate, g0c_review=review,
                candidate_path=f"{candidate_id}.json", manifest_sha256=digest)
        candidate["r71_binding"]["execution_dependency_manifest"]["sha256"] = "b" * 64
        review["r71_binding"]["execution_dependency_manifest"]["sha256"] = "b" * 64
        self.assertEqual(dependency_manifest._candidate_core_sha256(candidate), candidate_core)
        self.assertEqual(dependency_manifest._g0c_core_sha256(review), core_before)
        with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                    "do not bind the exact manifest"):
            dependency_manifest.validate_candidate_g0c_binding(
                candidate=candidate, g0c_review=review,
                candidate_path=f"{candidate_id}.json", manifest_sha256=digest)

        review["r71_binding"]["execution_dependency_manifest"]["sha256"] = digest
        candidate["r71_binding"]["execution_dependency_manifest"]["sha256"] = digest
        candidate["scope"] = "tampered candidate core"
        with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                    "does not bind the exact candidate core"):
            dependency_manifest.validate_candidate_g0c_binding(
                candidate=candidate, g0c_review=review,
                candidate_path=f"{candidate_id}.json", manifest_sha256=digest)

    def test_regular_file_snapshot_binds_bytes_and_detects_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "input.bin"
            path.write_bytes(b"frozen input")
            first = dependency_manifest._snapshot_path(
                str(path), kind="file", role="unit-input")
            self.assertEqual(first["sha256"], hashlib.sha256(b"frozen input").hexdigest())
            path.write_bytes(b"changed input")
            second = dependency_manifest._snapshot_path(
                str(path), kind="file", role="unit-input")
            self.assertNotEqual(first, second)

    def test_symlink_cannot_be_declared_as_a_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "fixture"
            root.mkdir()
            target = root / "target"
            target.write_text("data", encoding="utf-8")
            alias = root / "alias"
            alias.symlink_to(target)
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "may not be a symlink"):
                dependency_manifest._snapshot_path(
                    str(alias), kind="file", role="unit-input")

    def test_explicit_symlink_row_binds_final_alias_but_rejects_symlink_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "fixture"
            root.mkdir()
            target_dir = root / "real"
            target_dir.mkdir()
            target = target_dir / "input.bin"
            target.write_bytes(b"declared target")
            alias = root / "input-alias"
            alias.symlink_to(target)

            observed = dependency_manifest._snapshot_path(
                str(alias), kind="symlink", role="declared-alias")
            self.assertEqual(observed["kind"], "symlink")
            self.assertEqual(observed["target"], str(target))
            self.assertEqual(observed["resolved_path"], str(target))
            self.assertEqual(observed["resolved_identity"]["sha256"],
                             hashlib.sha256(b"declared target").hexdigest())
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "may not be a symlink"):
                dependency_manifest._snapshot_path(
                    str(alias), kind="file", role="ordinary-file")

            parent_alias = root / "alias-dir"
            parent_alias.symlink_to(target_dir, target_is_directory=True)
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "symlink component"):
                dependency_manifest._snapshot_path(
                    str(parent_alias / "input.bin"), kind="symlink", role="aliased-parent")


    def test_absolute_host_resource_binds_resolved_path_through_system_prefix_alias(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as temp:
            path = Path(temp) / "host-input.bin"
            path.write_bytes(b"host resource")
            observed = dependency_manifest._snapshot_path(
                str(path), kind="file", role="host-resource")
            self.assertEqual(observed["resolved_path"], str(path.resolve(strict=True)))
            self.assertEqual(observed["sha256"], hashlib.sha256(b"host resource").hexdigest())
            target = Path(temp) / "host-target.bin"
            target.write_bytes(b"host symlink target")
            alias = Path(temp) / "host-link"
            alias.symlink_to(target)
            symlink_observed = dependency_manifest._snapshot_path(
                str(alias), kind="symlink", role="host-symlink")
            self.assertEqual(symlink_observed["resolved_path"], str(target.resolve(strict=True)))
            self.assertEqual(symlink_observed["resolved_identity"]["sha256"],
                             hashlib.sha256(b"host symlink target").hexdigest())
            resource = dependency_manifest._root_relative_file(
                Path(temp), "host-input.bin", role="plan input")
            self.assertEqual(resource, path)

    def test_canonical_repo_and_resource_paths_reject_in_root_symlink_parents_and_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            repo = base / "repo"
            repo.mkdir()
            target_dir = repo / "real"
            target_dir.mkdir()
            (target_dir / "input.bin").write_bytes(b"data")
            repo_alias = repo / "alias"
            repo_alias.symlink_to(target_dir, target_is_directory=True)

            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "symlink component"):
                dependency_manifest._repo_relative_file(
                    repo, "alias/input.bin", role="candidate")
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "symlink component"):
                dependency_manifest._root_relative_file(
                    repo, "alias/input.bin", role="plan input")

            repo_root_alias = base / "repo-root-alias"
            repo_root_alias.symlink_to(repo, target_is_directory=True)
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "root path contains a symlink component"):
                dependency_manifest._repo_relative_file(
                    repo_root_alias, "real/input.bin", role="candidate")
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "root path contains a symlink component"):
                dependency_manifest._root_relative_file(
                    repo_root_alias, "real/input.bin", role="plan input")

    def test_tree_internal_symlink_remains_supported_only_for_declared_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "tree"
            root.mkdir()
            (root / "data.bin").write_bytes(b"data")
            (root / "data-alias").symlink_to("data.bin")
            observed = dependency_manifest._snapshot_path(
                str(root), kind="tree_internal_symlinks", role="internal-symlink-tree")
            row = next(item for item in observed["entries"] if item["path"] == "data-alias")
            self.assertEqual(row["kind"], "symlink")
            self.assertEqual(row["target"], "data.bin")
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "unsupported or forbidden"):
                dependency_manifest._snapshot_path(
                    str(root), kind="tree", role="ordinary-tree")

    def test_tree_snapshot_fails_closed_on_forbidden_symlink_or_missing_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp).resolve()
            root = temp_root / "tree"
            root.mkdir()
            (root / "inside").write_text("data", encoding="utf-8")
            outside = temp_root / "outside"
            outside.write_text("outside", encoding="utf-8")
            (root / "alias").symlink_to(outside)
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "unsupported or forbidden"):
                dependency_manifest._snapshot_path(
                    str(root), kind="tree", role="unit-tree")
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "missing or unreadable"):
                dependency_manifest._snapshot_path(
                    str(temp_root / "missing"), kind="file", role="unit-missing")

    def test_candidate_relative_paths_reject_traversal_and_symlink_parents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp).resolve()
            root = temp_root / "repo"
            root.mkdir()
            outside = temp_root / "outside"
            outside.mkdir()
            (outside / "secret").write_text("data", encoding="utf-8")
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "not canonical repository-relative"):
                dependency_manifest._repo_relative_file(root, "../outside/secret", role="candidate")
            (root / "alias").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "escapes the repository"):
                dependency_manifest._repo_relative_file(root, "alias/secret", role="candidate")

    def test_manifest_validation_requires_canonical_bytes_and_expected_digest(self) -> None:
        value = {"schema_version": 2, "status": "FROZEN_DEPENDENCY_CLOSURE",
                 "candidate_id": "R6-A-R71-PROVENANCE-CAPACITY-CANDIDATE-1",
                 "candidate_core_sha256": "a" * 64,
                 "candidate_core_sha256_projection": "canonical candidate JSON with only r71_binding.execution_dependency_manifest removed",
                 "plan_raw_sha256": "b" * 64, "plan_canonical_sha256": "c" * 64,
                 "g0c_sha256": "d" * 64, "graph_pair_key": "f" * 64,
                 "dependency_rows": []}
        canonical = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / dependency_manifest.MANIFEST_NAME
            path.write_bytes(canonical)
            digest = hashlib.sha256(canonical).hexdigest()
            candidate = {"candidate_id": value["candidate_id"], "r71_binding": {
                "execution_dependency_manifest": {"path": dependency_manifest.MANIFEST_NAME,
                                                    "sha256": digest}}}
            with mock.patch.object(dependency_manifest, "build_manifest", return_value=value):
                result = dependency_manifest.validate_manifest(
                    manifest_path=path, expected_sha256=digest, candidate=candidate, plan={},
                    plan_raw_sha256="b" * 64, plan_canonical_sha256="c" * 64,
                    g0c_sha256="d" * 64,
                    candidate_path="candidate.json", plan_path="plan.json",
                    repo=Path(temp))
            self.assertEqual(result["sha256"], digest)
            self.assertTrue({"candidate_id", "plan_raw_sha256", "plan_canonical_sha256",
                             "g0c_sha256", "graph_pair_key", "dependency_rows"} <= set(value))
            self.assertNotIn("candidate_core_raw_sha256", value)
            self.assertNotIn("candidate_core_canonical_sha256", value)
            with mock.patch.object(dependency_manifest, "build_manifest", return_value=value):
                with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                            "digest differs"):
                    dependency_manifest.validate_manifest(
                        manifest_path=path, expected_sha256="e" * 64, candidate=candidate, plan={},
                        plan_raw_sha256="b" * 64, plan_canonical_sha256="c" * 64,
                        g0c_sha256="d" * 64,
                        candidate_path="candidate.json", plan_path="plan.json",
                        repo=Path(temp))
            path.write_text('{ "status": "FROZEN_DEPENDENCY_CLOSURE", "schema_version": 1 }\n',
                            encoding="utf-8")
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "not canonical"):
                dependency_manifest.validate_manifest(
                    manifest_path=path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    candidate=candidate, plan={}, plan_raw_sha256="b" * 64,
                    plan_canonical_sha256="c" * 64,
                    g0c_sha256="d" * 64, candidate_path="candidate.json",
                    plan_path="plan.json", repo=Path(temp))

    def test_manifest_validation_rejects_intermediate_symlink_escape_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            repo = base / "repo"
            outside = base / "outside"
            repo.mkdir()
            outside.mkdir()
            escaped_manifest = outside / dependency_manifest.MANIFEST_NAME
            escaped_manifest.write_bytes(b"must not be read")
            (repo / "alias").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "not canonical within the repository"):
                dependency_manifest.validate_manifest(
                    manifest_path=repo / "alias" / dependency_manifest.MANIFEST_NAME,
                    expected_sha256="a" * 64, candidate={}, plan={},
                    plan_raw_sha256="b" * 64, plan_canonical_sha256="c" * 64,
                    g0c_sha256="d" * 64, candidate_path="candidate.json",
                    plan_path="plan.json", repo=repo)

    def test_built_manifest_binds_candidate_plan_source_and_ordered_graph_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "fixture"
            root.mkdir()
            gate_dir = root / "experiments" / "storage"
            gate_dir.mkdir(parents=True)
            gate_controls = {}
            for control_name in dependency_manifest.GATE_CONTROL_FILES:
                control_file = gate_dir / control_name
                control_file.write_text(f"synthetic Gate control {control_name}\n", encoding="utf-8")
                gate_controls[control_name] = hashlib.sha256(control_file.read_bytes()).hexdigest()
            gate_path = gate_dir / "storage-gate0-attestation.json"
            gate_path.write_text(
                json.dumps({"retention_control_sha256": gate_controls}, sort_keys=True) + "\n",
                encoding="utf-8")
            # Model source changes made before R71 candidate review, after the
            # historical Gate receipt was written. The pre-Gate roster is
            # derived from the exact fixed control files; final validation
            # must still reject the stale signed Gate map.
            stale_control = gate_dir / "storage_inventory.py"
            stale_control.write_text("current R71 storage inventory control\n", encoding="utf-8")
            runner_input = gate_dir / "run_r6a_execution.py"
            runner_input.write_text("synthetic runner\n", encoding="utf-8")
            test_input = gate_dir / "test_build_retention.py"
            test_input.write_text("synthetic test\n", encoding="utf-8")
            gate_controls["test_build_retention.py"] = hashlib.sha256(
                test_input.read_bytes()).hexdigest()
            gate_path.write_text(
                json.dumps({"retention_control_sha256": gate_controls}, sort_keys=True) + "\n",
                encoding="utf-8")
            current_control_roster = dependency_manifest.current_gate_control_roster(root)
            tool_dir = root / "tools"
            tool_dir.mkdir()
            meson_tool = tool_dir / "meson"
            meson_tool.write_text("synthetic Meson\n", encoding="utf-8")
            seatbelt_tool = tool_dir / "sandbox-exec"
            seatbelt_tool.write_text("synthetic Seatbelt\n", encoding="utf-8")
            runtime = root / "runtime" / "SDL3.dylib"
            runtime.parent.mkdir()
            runtime.write_bytes(b"synthetic runtime\n")
            source = root / "source"
            source.mkdir()
            (source / "src.txt").write_text("source", encoding="utf-8")
            source_hash, _ = build_retention._tree_hash(
                source, reject_symlinks=True, reject_external_hardlinks=True)
            candidate_filename = "R6-A-R71-PROVENANCE-CAPACITY-CANDIDATE-1.json"
            candidate_path = root / candidate_filename
            candidate = {"candidate_id": "R6-A-R71-PROVENANCE-CAPACITY-CANDIDATE-1",
                         "r71_binding": {"execution_dependency_manifest": {
                             "path": dependency_manifest.MANIFEST_NAME, "sha256": "0" * 64}},
                         "changed_source_hashes": {
                             "experiments/storage/run_r6a_execution.py": hashlib.sha256(
                                 runner_input.read_bytes()).hexdigest()},
                         "changed_test_hashes": {
                             "experiments/storage/test_build_retention.py": hashlib.sha256(
                                 test_input.read_bytes()).hexdigest()},
                         "exact_diff": {"path": "change.diff",
                                        "sha256": hashlib.sha256(b"diff").hexdigest()},
                         "transaction_contracts": {}}
            candidate_path.write_bytes(json.dumps(candidate, sort_keys=True).encode("utf-8"))
            plan = {
                "build_graphs": [
                    {"graph_id": "windows-product-r6a"},
                    {"graph_id": "native-verification-r6a"},
                ],
                "build_configuration_key_sha256": "f" * 64,
                "source_identity": {"root": str(source), "sha256": source_hash},
                "tools": {
                    "meson": {"path": str(meson_tool), "sha256": hashlib.sha256(
                        meson_tool.read_bytes()).hexdigest()},
                    "seatbelt": {"path": str(seatbelt_tool), "sha256": hashlib.sha256(
                        seatbelt_tool.read_bytes()).hexdigest()},
                },
                "input_receipts": [{"path": "src.txt", "sha256": hashlib.sha256(
                    (source / "src.txt").read_bytes()).hexdigest()}],
                "host_dependency_receipts": [],
                "runtime_receipts": [{"runtime_id": "SDL3", "path": str(runtime),
                                      "sha256": hashlib.sha256(runtime.read_bytes()).hexdigest()}],
            }
            plan_path = root / "plan.json"
            plan_raw = json.dumps(plan, sort_keys=True).encode("utf-8")
            plan_path.write_bytes(plan_raw)
            (root / "change.diff").write_bytes(b"diff")
            identity = lambda value: hashlib.sha256(
                build_retention._canonical_json(value)).hexdigest()
            g0c_sha = "d" * 64
            manifest = dependency_manifest.build_manifest(
                candidate=candidate, plan=plan,
                plan_raw_sha256=hashlib.sha256(plan_raw).hexdigest(),
                plan_canonical_sha256=identity(plan), g0c_sha256=g0c_sha,
                candidate_path=candidate_filename, plan_path="plan.json", repo=root,
                gate_control_roster=current_control_roster)
            manifest_raw = dependency_manifest._canonical_bytes(manifest)
            manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
            candidate["r71_binding"]["execution_dependency_manifest"]["sha256"] = manifest_sha
            candidate_path.write_bytes(json.dumps(candidate, sort_keys=True).encode("utf-8"))
            manifest = dependency_manifest.build_manifest(
                candidate=candidate, plan=plan,
                plan_raw_sha256=hashlib.sha256(plan_raw).hexdigest(),
                plan_canonical_sha256=identity(plan), g0c_sha256=g0c_sha,
                candidate_path=candidate_filename, plan_path="plan.json", repo=root,
                gate_control_roster=current_control_roster)
            self.assertEqual(dependency_manifest._canonical_bytes(manifest), manifest_raw)
            manifest_path = root / dependency_manifest.MANIFEST_NAME
            manifest_path.write_bytes(manifest_raw)
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "Gate-attested control differs from current bytes: storage_inventory.py"):
                dependency_manifest.validate_manifest(
                    manifest_path=manifest_path, expected_sha256=manifest_sha,
                    candidate=candidate, plan=plan,
                    plan_raw_sha256=hashlib.sha256(plan_raw).hexdigest(),
                    plan_canonical_sha256=identity(plan), g0c_sha256=g0c_sha,
                    candidate_path=candidate_filename, plan_path="plan.json", repo=root)
            pre_gate_validation = dependency_manifest.validate_manifest(
                manifest_path=manifest_path, expected_sha256=manifest_sha,
                candidate=candidate, plan=plan,
                plan_raw_sha256=hashlib.sha256(plan_raw).hexdigest(),
                plan_canonical_sha256=identity(plan), g0c_sha256=g0c_sha,
                candidate_path=candidate_filename, plan_path="plan.json", repo=root,
                gate_control_roster=current_control_roster)
            self.assertEqual(pre_gate_validation["sha256"], manifest_sha,
                             "an explicit current source roster validates the frozen pre-Gate closure")
            fresh_gate = {"retention_control_sha256": current_control_roster}
            gate_path.write_bytes(dependency_manifest._canonical_bytes(fresh_gate))
            result = dependency_manifest.validate_manifest(
                manifest_path=manifest_path, expected_sha256=manifest_sha,
                candidate=candidate, plan=plan,
                plan_raw_sha256=hashlib.sha256(plan_raw).hexdigest(),
                plan_canonical_sha256=identity(plan), g0c_sha256=g0c_sha,
                candidate_path=candidate_filename, plan_path="plan.json", repo=root)
            self.assertEqual(result["candidate_id"], candidate["candidate_id"])
            self.assertEqual(manifest["graph_pair"], [
                "windows-product-r6a", "native-verification-r6a"])
            self.assertTrue({"candidate_id", "plan_raw_sha256", "plan_canonical_sha256",
                             "g0c_sha256", "graph_pair_key", "dependency_rows"}
                            <= set(manifest))
            self.assertEqual(manifest["g0c_sha256_projection"],
                             "canonical G0C review JSON with only r71_binding.execution_dependency_manifest removed")
            self.assertEqual(manifest["identity_rule"],
                             "candidate_and_g0c_envelopes_bind_manifest_raw_sha256; candidate_and_g0c_core_bind_each_other_without_envelopes")
            self.assertEqual({row["role"] for row in manifest["dependency_rows"]} & {
                "execution_plan"}, {"execution_plan"})
            dependency_roles = {row["role"] for row in manifest["dependency_rows"]}
            self.assertNotIn("gate_attestation", dependency_roles,
                             "mutable Gate output bytes are pinned separately by ALLOW")
            self.assertIn("gate_control:storage_inventory.py",
                          {row["role"] for row in manifest["dependency_rows"]})
            manifest_path = root / dependency_manifest.MANIFEST_NAME
            manifest_path.write_bytes(manifest_raw)

            refreshed_gate = json.loads(gate_path.read_bytes())
            refreshed_gate.update({
                "created_at_unix": 1234567890,
                "supplemental_evidence_sha256": {
                    "g0c-launcher-independent-review.json": "a" * 64},
                "gate0_hmac_sha256": "b" * 64,
            })
            gate_path.write_bytes(dependency_manifest._canonical_bytes(refreshed_gate))
            refreshed_closure = dependency_manifest.build_manifest(
                candidate=candidate, plan=plan,
                plan_raw_sha256=hashlib.sha256(plan_raw).hexdigest(),
                plan_canonical_sha256=identity(plan), g0c_sha256=g0c_sha,
                candidate_path=candidate_filename, plan_path="plan.json", repo=root)
            self.assertEqual(refreshed_closure, manifest,
                             "refreshing Gate output with the same control inventory must not change closure")
            refreshed_validation = dependency_manifest.validate_manifest(
                manifest_path=manifest_path, expected_sha256=manifest_sha,
                candidate=candidate, plan=plan,
                plan_raw_sha256=hashlib.sha256(plan_raw).hexdigest(),
                plan_canonical_sha256=identity(plan), g0c_sha256=g0c_sha,
                candidate_path=candidate_filename, plan_path="plan.json", repo=root)
            self.assertEqual(refreshed_validation["sha256"], manifest_sha,
                             "closure validation must accept mutable final Gate output bytes")
            unrelated_report = gate_dir / "unrelated-report.log"
            unrelated_report.write_bytes(b"r" * 4096)
            unaffected = dependency_manifest.build_manifest(
                candidate=candidate, plan=plan,
                plan_raw_sha256=hashlib.sha256(plan_raw).hexdigest(),
                plan_canonical_sha256=identity(plan), g0c_sha256=g0c_sha,
                candidate_path=candidate_filename, plan_path="plan.json", repo=root)
            self.assertEqual(unaffected, manifest,
                             "unrelated 4 KiB report growth must not alter execution provenance")
            validated_unaffected = dependency_manifest.validate_manifest(
                manifest_path=manifest_path, expected_sha256=manifest_sha,
                candidate=candidate, plan=plan,
                plan_raw_sha256=hashlib.sha256(plan_raw).hexdigest(),
                plan_canonical_sha256=identity(plan), g0c_sha256=g0c_sha,
                candidate_path=candidate_filename, plan_path="plan.json", repo=root)
            self.assertEqual(validated_unaffected["sha256"], manifest_sha)
            source_file = source / "src.txt"
            original_source = source_file.read_bytes()
            for boundary in ("after Gate", "after reservation", "before consumption"):
                with self.subTest(dependency_mutation_boundary=boundary):
                    source_file.write_bytes(original_source + f"changed at {boundary}\n".encode())
                    with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                                "source tree differs"):
                        dependency_manifest.validate_manifest(
                            manifest_path=manifest_path, expected_sha256=manifest_sha,
                            candidate=candidate, plan=plan,
                            plan_raw_sha256=hashlib.sha256(plan_raw).hexdigest(),
                            plan_canonical_sha256=identity(plan), g0c_sha256=g0c_sha,
                            candidate_path=candidate_filename, plan_path="plan.json", repo=root)
                    source_file.write_bytes(original_source)
            control_file = gate_dir / "storage_inventory.py"
            original_control = control_file.read_bytes()
            control_file.write_bytes(original_control + b"tampered after Gate\n")
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "Gate-attested control differs"):
                dependency_manifest.build_manifest(
                    candidate=candidate, plan=plan,
                    plan_raw_sha256=hashlib.sha256(plan_raw).hexdigest(),
                    plan_canonical_sha256=identity(plan), g0c_sha256=g0c_sha,
                    candidate_path=candidate_filename, plan_path="plan.json", repo=root)
            control_file.write_bytes(original_control)
            self.assertNotIn("candidate_raw_sha256", manifest)

            tamper_cases = (
                (source / "src.txt", b"changed source\n", "source tree differs"),
                (runner_input, b"changed runner\n", "candidate file differs"),
                (test_input, b"changed test\n", "Gate-attested control differs"),
                (meson_tool, b"changed Meson\n", "tool differs"),
                (seatbelt_tool, b"changed Seatbelt\n", "tool differs"),
                (runtime, b"changed runtime\n", "runtime input differs"),
            )
            for changed_path, changed_bytes, error_text in tamper_cases:
                original_bytes = changed_path.read_bytes()
                changed_path.write_bytes(changed_bytes)
                with self.subTest(dependency=str(changed_path)), self.assertRaisesRegex(
                        dependency_manifest.DependencyManifestError, error_text):
                    dependency_manifest.build_manifest(
                        candidate=candidate, plan=plan,
                        plan_raw_sha256=hashlib.sha256(plan_raw).hexdigest(),
                        plan_canonical_sha256=identity(plan), g0c_sha256=g0c_sha,
                        candidate_path=candidate_filename, plan_path="plan.json", repo=root)
                changed_path.write_bytes(original_bytes)

            original_plan_bytes = plan_path.read_bytes()
            plan_path.write_bytes(original_plan_bytes + b" \n")
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "plan file or parsed plan differs"):
                dependency_manifest.build_manifest(
                    candidate=candidate, plan=plan,
                    plan_raw_sha256=hashlib.sha256(original_plan_bytes).hexdigest(),
                    plan_canonical_sha256=identity(plan), g0c_sha256=g0c_sha,
                    candidate_path=candidate_filename, plan_path="plan.json", repo=root)
            plan_path.write_bytes(original_plan_bytes)
            plan["build_graphs"].reverse()
            with self.assertRaisesRegex(dependency_manifest.DependencyManifestError,
                                        "ordered R6-A graph pair"):
                dependency_manifest.build_manifest(
                    candidate=candidate, plan=plan,
                    plan_raw_sha256=hashlib.sha256(plan_raw).hexdigest(),
                    plan_canonical_sha256=identity(plan), g0c_sha256=g0c_sha,
                    candidate_path=candidate_filename, plan_path="plan.json", repo=root)


if __name__ == "__main__":
    unittest.main()
