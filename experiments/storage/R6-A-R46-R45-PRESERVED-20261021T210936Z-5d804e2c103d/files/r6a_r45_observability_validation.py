"""Synthetic-only acceptance checks for R45 failure evidence.

No filesystem fixture, helper child, scanner, or subprocess is dispatched here.
The only writes are the diagnostic report artifacts supplied by the caller.
"""

import copy
import ast
import hashlib
import json
from pathlib import Path
import stat

from r6a_r45_failure_observability import FailureRecorder


REQUIRED_FAILURE_KEYS = {
    "operation_id", "case_id", "phase", "description", "timestamp",
    "exception_type", "errno", "strerror", "args_repr", "filename",
    "filename_type", "filename2", "filename2_type", "full_traceback",
    "cwd", "input_paths", "path_types", "relevant_object_identities_before",
    "relevant_object_identities_after_or_failure", "root_pin_identity_if_live",
    "helper_identity_if_live", "scanner_child_identity_if_known",
    "execution_context", "evidence_capture_failures", "authoritative",
}


def _identity(inode, file_type):
    return {"device": 123, "inode": inode, "file_type": file_type}


def _synthetic_snapshot(stage, *, parent_only=False):
    root = _identity(1001, stat.S_IFDIR)
    child = _identity(1002, stat.S_IFREG)
    def path_record(identity, path):
        fields = {**identity, "mode": identity["file_type"] | 0o600}
        sample = {"status": "ok", "fields": fields,
                  "identity": (identity["device"], identity["inode"], identity["file_type"])}
        return {"status": "observed", "path_fspath": {"type": "builtins.bytes", "value": path},
                "lstat": copy.deepcopy(sample), "stat": copy.deepcopy(sample),
                "canonical_path": path, "canonical_path_bytes": path,
                "canonical_stat": copy.deepcopy(sample)}
    return {
        "data_origin": "SYNTHETIC_ONLY", "stage": stage,
        "suite_root": path_record(_identity(1000, stat.S_IFDIR), b"/synthetic/suite"),
        "case_root": {"status": "not_yet_known"} if parent_only else path_record(root, b"/synthetic/suite/case_8"),
        "child": {"status": "not_yet_known"} if parent_only else path_record(child, b"/synthetic/suite/case_8/child\nline.bin"),
        "root_pin": {"status": "not_open"} if parent_only else {"status": "ok", "fd": 91, "fields": root,
                      "identity": (123, 1001, stat.S_IFDIR)},
        "child_fd": {"status": "not_open"} if parent_only else {"status": "ok", "fd": 92, "fields": child,
                      "identity": (123, 1002, stat.S_IFREG)},
        "helper": {"status": "known", "pid": 6201, "live": True,
                   "ready_identity": {"device": 123, "inode": 1001, "mode_type": stat.S_IFDIR}, "stderr_raw": b""},
        "scanner_child": {"status": "not_dispatched", "identity": None},
        "observer_errors": [],
    }


def run_validation(*, run_id, output_directory, publish, verify, decode,
                   success_schema, runner_info, scanner_info, guards, state,exercise,helper_source):
    """Run each specified injection once, stopping at the first failed check."""
    output_directory = Path(output_directory)
    evidence_directory = output_directory / f"R6-A-R45-OBSERVABILITY-PREFLIGHT-{run_id}-EVIDENCE"
    evidence_directory.mkdir()
    results = []
    state.update({"result": "RUNNING", "tests": results, "active_test": None,
                  "evidence_directory": str(evidence_directory), "scanner_suite_executed": False})

    def require(condition, message):
        if not condition:
            raise AssertionError("R45 synthetic observability check failed: " + message)

    baseline_path=output_directory/'R6-A-R45-R44-OBSERVABILITY-FAILURE-EVIDENCE'/'r44-runner.py'
    baseline_bytes=baseline_path.read_bytes()
    current_bytes=Path(runner_info['path']).read_bytes()
    require(hashlib.sha256(baseline_bytes).hexdigest()=='d7041b85c54b2de0dea614bdc891c8591cfab65197189aba0260f061c3b2c101','preserved R44 runner identity')
    baseline_tree=ast.parse(baseline_bytes)
    current_tree=ast.parse(current_bytes)
    def assignments(tree,name):
        return [ast.dump(node.value,include_attributes=False) for node in ast.walk(tree)
                if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id==name for target in node.targets)]
    # ast.walk visits nested scopes breadth first, so compare predicate multisets
    # rather than allowing wrapper nesting to change their traversal ordering.
    require(sorted(assignments(baseline_tree,'ok'))==sorted(assignments(current_tree,'ok')),'all original case acceptance predicates unchanged')
    def function(tree,name):
        return next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name==name)
    old_checks=assignments(function(baseline_tree,'live_scan_contract'),'checks')
    new_checks=assignments(function(current_tree,'live_scan_contract'),'checks')
    require(old_checks==new_checks,'status-zero/stderr/framing/identity scan checks unchanged')
    old_case8=function(baseline_tree,'_case8_pre_scan_assertions');new_case8=function(current_tree,'_case8_pre_scan_assertions')
    for name in ('checks','expected_basename','actual_basename','output_paths'):
        require(assignments(old_case8,name)==assignments(new_case8,name),'Case-8 original pre-scan '+name+' unchanged')
    old_helper=function(baseline_tree,'helper')
    old_helper_source=next(ast.literal_eval(node.value) for node in old_helper.body
                           if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='code' for target in node.targets))
    old_child_tree=ast.parse(old_helper_source)
    new_child_tree=ast.parse(helper_source())
    new_child_success=next(node.body for node in new_child_tree.body if isinstance(node,ast.Try))
    class RemoveObservationMarkers(ast.NodeTransformer):
        def visit_Assign(self,node):
            if any(isinstance(target,ast.Name) and target.id=='operation' for target in node.targets):return None
            return self.generic_visit(node)
    stripped_child=RemoveObservationMarkers().visit(ast.Module(body=new_child_success,type_ignores=[]))
    old_child_success=ast.Module(body=[node for node in old_child_tree.body if not isinstance(node,ast.Import)],type_ignores=[])
    require(ast.dump(stripped_child,include_attributes=False)==ast.dump(old_child_success,include_attributes=False),'helper original successful holder/protocol statements unchanged')
    expected_hashes={
        'r6a_r33_live_open_capture.py':'6e138795a18c2f3cb21a806cbe4025fe6e59eabe673d0ec72863dd885f185b45',
        'build_retention.py':'3c1669e56713babf36f99037aee3a5a07e8c40e4ed6a41ac1558466d1a8f1997',
        'test_build_retention.py':'00a8ceda17823358599de1832474f94378ff1db2514c07b62a7cc6803ecab19b',
    }
    for name,expected_hash in expected_hashes.items():
        require(hashlib.sha256((output_directory/name).read_bytes()).hexdigest()==expected_hash,name+' untouched accepted source hash')
    semantic_checks={'original_case_predicates':'PASS','original_live_scan_checks':'PASS','original_case8_pre_scan_assertions':'PASS',
                     'original_helper_successful_holder_protocol':'PASS',
                     'preserved_r44_runner_sha256':hashlib.sha256(baseline_bytes).hexdigest(),'accepted_untouched_source_hashes':expected_hashes}
    state['semantic_checks']=semantic_checks

    filename = b"/synthetic/suite/case_8/child\n\xff.bin"
    filename2 = b"/synthetic/suite/case_8/second\x00\xfe.bin"
    specifications = [
        ("01-parent-creation", "suite", "suite.root.create", "parent creation", True, False),
        ("02-child-write-open", "8", "case8.child.write_bytes", "case-8 fixture setup", False, False),
        ("02b-child-open", "8", "case8.child_fd.open", "case-8 fixture setup", False, False),
        ("03-child-stat", "8", "case8.child.named_stat_before", "case-8 fixture setup", False, False),
        ("04-root-pin-stat", "8", "case8.scan.pre_scan.root.pin_fstat", "pre_scan pinned-root identity", False, False),
        ("05-scanner-before-dispatch", "8", "case8.scan.invoke", "scanner invocation", False, False),
        ("06-postscan-named-root", "8", "case8.scan.post_scan.root.named_stat", "post_scan named-root identity", False, False),
        ("07-postscan-pinned-root", "8", "case8.scan.post_scan.root.pin_fstat", "post_scan pinned-root identity", False, False),
        ("08-postscan-child", "8", "case8.post_scan.child.canonical_stat", "post-scan child identity", False, False),
        ("09-helper-cleanup", "4", "case4.helper.stop.kill_wait", "helper shutdown", False, False),
        ("10-unanticipated-generic", "8", "case8.unexpected.generic", "unexpected generic boundary", False, True),
        ("11-secondary-observer-preserves-primary", "8", "case8.scan.invoke", "scanner invocation", False, False),
    ]

    def publish_test(slug, payload, test_run_id):
        base = evidence_directory / slug
        j = base.with_suffix(".json")
        m = base.with_suffix(".md")
        c = evidence_directory / (slug + "-COMPLETE.json")
        inc = evidence_directory / (slug + "-INCOMPLETE.json")
        artifacts = publish(payload, j, m, c, inc, test_run_id)
        manifest = verify(j, m, c, inc)
        reconstructed = decode(json.loads(j.read_text(encoding="ascii"))["root"])
        require(manifest["status"] == "COMPLETE", slug + " complete publication")
        require(artifacts["json_size_bytes"] > 0 and artifacts["markdown_size_bytes"] > 0, slug + " nonempty report pair")
        return artifacts, reconstructed

    for slug, case_id, operation_id, phase, parent_only, generic in specifications:
        test_report = {"schema_version": 3, "result": "EXPECTED_INJECTED_FAILURE",
                       "data_origin": "SYNTHETIC_ONLY", "cases": [], "cleanup": [], "first_failure": None,
                       "case8_setup_operations":[],"case8_observer_errors":[],"case8_pre_scan_contract":None}
        recorder = FailureRecorder(test_report, run_id + "-" + slug)
        recorder.set_case(case_id, suite_root=b"/synthetic/suite",
                          case_root=None if parent_only else b"/synthetic/suite/case_8",
                          child_path=None if parent_only else filename)
        secondary = slug.startswith("11-")

        def synthetic_snapshot(stage):
            snapshot = _synthetic_snapshot(stage, parent_only=parent_only)
            if not slug.startswith('09-'):
                snapshot['helper']={'status':'not_initialized'}
            else:
                snapshot['child']={'status':'not_applicable'}
                snapshot['child_fd']={'status':'not_open'}
            if slug.startswith(('06-','07-','08-','09-')):
                snapshot['scanner_child']={'status':'known','pid':6301,'ppid':6101,
                                           'identity':{'start_identity':b'synthetic-scanner-start'}}
            if slug.startswith('02-'):
                snapshot['child']={'status':'missing','path_bytes':b'/synthetic/suite/case_8/child\nline.bin'}
                snapshot['root_pin']={'status':'not_open'}
                snapshot['child_fd']={'status':'not_open'}
            elif slug.startswith('02b-'):
                snapshot['child_fd']={'status':'not_open'}
            if secondary and stage == "failure":
                try:
                    raise OSError(5, "R45 injected secondary identity observation failure", b"/synthetic/root")
                except OSError as secondary_exc:
                    return snapshot, [{"field": "failure.injected_identity", "exception": secondary_exc,
                                       "traceback_object": secondary_exc.__traceback__}]
            return snapshot, []

        recorder.snapshot = synthetic_snapshot
        active = {"name": slug, "operation_id": operation_id, "case_id": case_id,
                  "phase": phase, "result": "RUNNING", "transcript": test_report}
        state["active_test"] = active
        results.append(active)

        def injected_original_operation():
            raise OSError(13, "R45 injected original operation failure", filename, None, filename2)

        captured,original_reference=exercise((slug,case_id,operation_id,phase,parent_only,generic),recorder,test_report,injected_original_operation)

        require(REQUIRED_FAILURE_KEYS <= set(captured), slug + " required failure keys")
        require(captured["operation_id"] == operation_id and captured["case_id"] == case_id and captured["phase"] == phase,
                slug + " exact case/phase/operation")
        require(captured["full_traceback"] and "injected_original_operation" in captured["full_traceback"], slug + " original full traceback")
        require(captured["errno"] == 13 and captured["strerror"] == "R45 injected original operation failure", slug + " errno/strerror")
        require(captured["filename"] == filename and captured["filename_type"] == "builtins.bytes", slug + " bytes filename")
        require(captured["filename2"] == filename2 and captured["filename2_type"] == "builtins.bytes", slug + " bytes filename2")
        require(captured["args_repr"] and captured["cwd"]["status"] == "ok", slug + " args/cwd")
        require(captured["input_paths"] and captured["path_types"], slug + " typed path arguments")
        require(captured["relevant_object_identities_before"]["suite_root"]["lstat"]["fields"]["inode"] == 1000,
                slug + " parent identity")
        require(captured["relevant_object_identities_after_or_failure"]["data_origin"] == "SYNTHETIC_ONLY", slug + " failure identities")
        if not parent_only and not slug.startswith('02-'):
            require(captured["root_pin_identity_if_live"]["fields"] == _identity(1001, stat.S_IFDIR), slug + " pin identity")
        else:
            require(captured['root_pin_identity_if_live']['status']=='not_open',slug+' explicit unavailable pin')
        if slug.startswith('09-'):
            require(captured['helper_identity_if_live']['pid']==6201 and captured['helper_identity_if_live']['ready_identity']['inode']==1001,slug+' available helper identity')
        if slug.startswith(('06-','07-','08-','09-')):
            require(captured['scanner_child_identity_if_known']['identity']['start_identity']==b'synthetic-scanner-start',slug+' known scanner child identity')
        require(test_report["primary_failure"] is captured and any(original is original_reference and record is captured for original,record in recorder.original_exceptions),
                slug + " immutable primary ownership")
        if secondary:
            require(captured["evidence_capture_failures"] and not captured["evidence_complete"], slug + " secondary capture retained")
            require(captured["evidence_capture_failures"][0]["full_traceback"], slug + " secondary traceback")
            require(captured["exception_type"] == "builtins.PermissionError", slug + " secondary did not replace primary")
        else:
            require(captured["evidence_complete"] and not captured["evidence_capture_failures"], slug + " complete evidence")
        artifacts, reconstructed = publish_test(slug, test_report, recorder.run_id)
        published = reconstructed["primary_failure"]
        require(published["filename"] == filename and published["filename2"] == filename2, slug + " lossless JSON filename round trip")
        require(published["full_traceback"] == captured["full_traceback"], slug + " traceback round trip")
        require(published["relevant_object_identities_before"] == captured["relevant_object_identities_before"], slug + " identity round trip")
        require(published["authoritative"] and published["failure_id"] == captured["failure_id"], slug + " primary preserved in report")
        active.update({"result": "PASS", "publication": artifacts,
                       "checks": ["operation_id", "full_traceback", "exception_fields", "typed_paths", "context_identities",
                                  "publication", "primary_preserved", "lossless_roundtrip"]})

    root = _identity(1001, stat.S_IFDIR)
    child = _identity(1002, stat.S_IFREG)
    root_path = b"/synthetic/private/suite/case_8"
    child_path = root_path + b"/child\nline.bin"
    root_row = {"pid": 6101, "ppid": 6100, "fd_raw": b"91", "fd_number": 91,
                "fd_type_raw": b"DIR", "device_raw": b"0x7b", "device": 123,
                "inode_raw": b"1001", "inode": 1001, "name_raw": root_path, "parser_status": "PASS"}
    child_row = {"pid": 6101, "ppid": 6100, "fd_raw": b"92", "fd_number": 92,
                 "fd_type_raw": b"REG", "device_raw": b"0x7b", "device": 123,
                 "inode_raw": b"1002", "inode": 1002, "name_raw": child_path, "parser_status": "PASS"}
    success = success_schema(
        root_before={"named_root": root, "pinned_root": root, "canonical_root": root},
        root_after={"named_root": root, "pinned_root": root, "canonical_root": root},
        child_before={"named_child": child, "descriptor_child": child, "canonical_child": child},
        child_after={"named_child": child, "descriptor_child": child, "canonical_child": child},
        path_bytes={"root_raw": b"/synthetic/suite/case_8", "child_raw": b"/synthetic/suite/case_8/child\nline.bin",
                    "root_canonical_before": root_path, "root_canonical_after": root_path,
                    "child_canonical_before": child_path, "child_canonical_after": child_path},
        basename_bytes=b"child\nline.bin", pin_fd=91, child_fd=92, checker_pid=6101,
        scanner_rows=[root_row, child_row], root_pin_rows=[root_row], child_rows=[child_row],
        scanner_child={"pid": 6102, "ppid": 6101, "identity": {"start_identity": b"synthetic-start"}})
    require(success["identity_comparisons_pass"] and all(success["comparisons"].values()), "synthetic Case-8 comparisons")
    success_payload = {"schema_version": 3, "result": "SYNTHETIC_CASE8_SUCCESS", "data_origin": "SYNTHETIC_ONLY",
                       "case8_success_evidence": success, "cases": []}
    success_artifacts, success_reconstructed = publish_test("13-case8-success-schema", success_payload, run_id + "-case8-success")
    restored = success_reconstructed["case8_success_evidence"]
    require(restored == success, "synthetic Case-8 complete schema round trip")
    require(restored["root"]["before"]["pinned_root"] == root and restored["root"]["after"]["pinned_root"] == root,
            "actual pre/post root values retained")
    require(restored["child"]["before"]["descriptor_child"] == child and restored["child"]["after"]["descriptor_child"] == child,
            "actual pre/post child values retained")
    require(restored["scanner_rows"]["fixture_child"][0]["name_raw"] == child_path and restored["basename"]["lf_count"] == 1,
            "raw newline/scanner row bytes retained")
    require(not any(guards.values()), "zero scanner or real subprocess dispatch attempts")
    state.update({"result": "PASS", "active_test": None,
                  "success_schema": {"result": "PASS", "publication": success_artifacts, "evidence": success},
                  "scanner_dispatch_guards": dict(guards)})
    payload = {"schema_version": 3, "result": "PASS", "validation_kind": "SYNTHETIC_OBSERVABILITY_ONLY",
               "run_id": run_id, "runner": runner_info, "scanner": scanner_info,
               "historical_case8_root_cause": "UNRESOLVED", "real_scanner_suite_executed": False,
               "real_scanner_calls": 0, "real_subprocess_dispatches": 0,
               "failure_injections": results, "case8_success_schema": state["success_schema"]}
    payload['semantic_checks']=semantic_checks
    prefix = output_directory / f"R6-A-R45-OBSERVABILITY-PREFLIGHT-{run_id}"
    j = prefix.with_suffix(".json")
    m = prefix.with_suffix(".md")
    c = output_directory / (prefix.name + "-COMPLETE.json")
    inc = output_directory / (prefix.name + "-INCOMPLETE.json")
    artifacts = publish(payload, j, m, c, inc, run_id)
    verify(j, m, c, inc)
    restored_payload = decode(json.loads(j.read_text(encoding="ascii"))["root"])
    require(restored_payload["result"] == "PASS" and len(restored_payload["failure_injections"]) == len(specifications),
            "complete aggregate publication")
    state["publication"] = artifacts
    return artifacts

