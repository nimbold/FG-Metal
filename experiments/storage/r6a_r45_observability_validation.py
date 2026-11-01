"""Synthetic-only acceptance checks for R46 terminal failure evidence.

No filesystem fixture, helper child, scanner, or subprocess is dispatched here.
The only writes are the diagnostic report artifacts supplied by the caller.
"""

import copy
import ast
import hashlib
import json
import os
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
                   freeze_evidence,restore_evidence,
                   success_schema, runner_info, scanner_info, guards, state,exercise,helper_source,
                   terminal_reporter,terminal_state_factory):
    """Run each specified injection once, stopping at the first failed check."""
    output_directory = Path(output_directory)
    evidence_directory = output_directory / f"R6-A-R46-OBSERVABILITY-PREFLIGHT-{run_id}-EVIDENCE"
    evidence_directory.mkdir()
    results = []
    state.update({"result": "RUNNING", "tests": results, "active_test": None,
                  "evidence_directory": str(evidence_directory), "scanner_suite_executed": False})

    active_terminal_assertions=None

    def require(condition, message):
        passed=bool(condition)
        if active_terminal_assertions is not None:
            active_terminal_assertions.append({'check_id':message,'result':'PASS' if passed else 'FAIL'})
            state['active_terminal_assertions']=active_terminal_assertions
        if not passed:
            raise AssertionError("R46 synthetic observability check failed: " + message)

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

    def publish_test(slug, payload, test_run_id, *, check_prefix=None):
        base = evidence_directory / slug
        j = base.with_suffix(".json")
        m = base.with_suffix(".md")
        c = evidence_directory / (slug + "-COMPLETE.json")
        inc = evidence_directory / (slug + "-INCOMPLETE.json")
        artifacts = publish(payload, j, m, c, inc, test_run_id)
        manifest = verify(j, m, c, inc)
        reconstructed = decode(json.loads(j.read_text(encoding="ascii"))["root"])
        prefix=check_prefix or slug
        require(manifest["status"] == "COMPLETE", prefix + ".publication_manifest_complete")
        require(artifacts["json_size_bytes"] > 0, prefix + ".publication_json_nonempty")
        require(artifacts["markdown_size_bytes"] > 0, prefix + ".publication_markdown_nonempty")
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

    class SyntheticTerminalIO:
        """In-memory terminal sinks; no descriptor, subprocess, or lsof calls."""
        def __init__(self,faults):
            self.faults=faults
            self.stderr_attempts=[]
            self.stderr_accepted=[]
            self.file_bytes=bytearray()
            self.file_events=[]
            self.raw_tracebacks=[]
            self.write_count=0
            self.pending_stderr=None

        @staticmethod
        def _error(stage,path=None):
            raw_path=os.fsencode(path) if path is not None else (b'/synthetic/terminal/'+stage.encode('ascii'))
            secondary=b'/synthetic/terminal/secondary-'+stage.encode('ascii')
            err={'stderr_write':5,'open':13,'write':28,'fsync':5,'close':9}.get(stage,5)
            return OSError(err,'R46 injected terminal '+stage+' failure',raw_path,None,secondary)

        def stderr_write(self,text):
            raw=text.encode('ascii')
            attempt={'attempt_number':len(self.stderr_attempts)+1,'attempted_bytes':raw,'accepted':False}
            self.stderr_attempts.append(attempt)
            if self.faults.get('stderr_write'):
                raise self._error('stderr_write')
            attempt['write_complete']=True
            self.pending_stderr=(attempt,raw)
            return len(text)

        def flush_stderr(self):
            if self.faults.get('stderr_flush'):
                self.file_events.append({'operation':'stderr_flush','status':'failed'})
                raise self._error('stderr_flush')
            self.file_events.append({'operation':'stderr_flush','status':'succeeded'})
            if self.pending_stderr is not None:
                attempt,raw=self.pending_stderr
                attempt['accepted']=True
                self.stderr_accepted.append(raw)
                self.pending_stderr=None

        def open_emergency(self,_unused_fd,destination):
            self.file_events.append({'operation':'open','path_type':type(destination).__module__+'.'+type(destination).__qualname__})
            if self.faults.get('open'):
                raise self._error('open',destination)
            return 7401

        def write_emergency(self,fd,view):
            self.write_count+=1
            self.file_events.append({'operation':'write','fd':fd,'call':self.write_count})
            raw=bytes(view)
            if self.faults.get('write')=='partial_then_fail' and self.write_count==1:
                count=max(1,len(raw)//2)
                self.file_bytes.extend(raw[:count])
                return count
            if self.faults.get('write'):
                raise self._error('write')
            self.file_bytes.extend(raw)
            return len(raw)

        def fsync_emergency(self,fd):
            self.file_events.append({'operation':'fsync','fd':fd})
            if self.faults.get('fsync'):
                raise self._error('fsync')

        def close_emergency(self,fd):
            self.file_events.append({'operation':'close','fd':fd})
            if self.faults.get('close'):
                raise self._error('close')

        def raw_traceback(self,exc,tb):
            self.raw_tracebacks.append(''.join(__import__('traceback').format_exception(type(exc),exc,tb)))

    terminal_specs=[
        {'name':'T1','faults':{'open':True},'expected_failure':'report.emergency.open'},
        {'name':'T2','faults':{'write':'partial_then_fail'},'expected_failure':'report.emergency.write'},
        {'name':'T3','faults':{'fsync':True},'expected_failure':'report.emergency.fsync'},
        {'name':'T4','faults':{'close':True},'expected_failure':'report.emergency.close'},
        {'name':'T5','faults':{'stderr_flush':True},'expected_failure':'report.emergency.stderr'},
        {'name':'T6','faults':{'write':True},'expected_failure':'report.emergency.write'},
        {'name':'T7','faults':{'write':True,'close':True},'expected_failure':'report.emergency.write'},
        {'name':'T8','faults':{'stderr_write':True,'open':True},'expected_failure':None},
        {'name':'T9','faults':{'open':True},'expected_failure':'report.emergency.open','observer_failure':True},
        {'name':'T10','faults':{},'expected_failure':None,'top_level':True},
    ]
    capture_directory=evidence_directory/'raw-terminal-sink-captures'
    capture_directory.mkdir()
    terminal_results=[]

    def decode_emission(raw,*,stderr,check_prefix):
        if stderr:
            marker=b'R46_STRUCTURED_FAILURE='
            require(raw.startswith(marker),check_prefix+'.stderr_structured_prefix')
            raw=raw[len(marker):]
        envelope=json.loads(raw.decode('ascii'))
        require(envelope['evidence_encoding']=='r6a-r40-typed-json',check_prefix+'.typed_json_encoding')
        return decode(envelope['root'])

    def record_primary(recorder,operation_id,description,exception,paths=None,phase='report publication'):
        try:
            raise exception
        except BaseException as exc:
            context=recorder.context(operation_id,phase,description,
                                     paths if paths is not None else
                                     {'json':b'/synthetic/reports/report.json','markdown':b'/synthetic/reports/report.md'})
            return recorder.record(exc,tb=exc.__traceback__,context=context)

    def assert_typed_paths(record,check_prefix,path_values):
        def qualified_type(value):
            return type(value).__module__+'.'+type(value).__qualname__

        for path_label,expected_path in path_values.items():
            path=next((item for item in record.get('input_paths',[])
                       if item.get('label')==path_label),None)
            require(path is not None,check_prefix+'.'+path_label+'.present')
            require(path.get('capture_status')=='ok',check_prefix+'.'+path_label+'.capture_status')
            expected_runtime_type=qualified_type(expected_path)
            expected_fspath=os.fspath(expected_path)
            expected_fspath_type=qualified_type(expected_fspath)
            require(path.get('runtime_type')==expected_runtime_type,
                    check_prefix+'.'+path_label+'.supplied_type')
            require(path.get('fspath_type')==expected_fspath_type,
                    check_prefix+'.'+path_label+'.fspath_type')
            require(path.get('fspath')==expected_fspath,
                    check_prefix+'.'+path_label+'.fspath_value')
            require(path.get('filesystem_bytes')==os.fsencode(expected_fspath),
                    check_prefix+'.'+path_label+'.filesystem_bytes')
            if isinstance(expected_path,os.PathLike):
                expected_value={'adapter':'os.PathLike','runtime_type':expected_runtime_type,
                                'fspath':expected_fspath}
            else:
                expected_value=expected_path
            require(path.get('value')==expected_value,check_prefix+'.'+path_label+'.typed_value')
            summary=record.get('path_types',{}).get(path_label)
            require(summary is not None,check_prefix+'.'+path_label+'.summary_present')
            require(summary.get('capture_status')=='ok',check_prefix+'.'+path_label+'.summary_capture_status')
            require(summary.get('runtime_type')==expected_runtime_type,
                    check_prefix+'.'+path_label+'.summary_supplied_type')
            require(summary.get('fspath_type')==expected_fspath_type,
                    check_prefix+'.'+path_label+'.summary_fspath_type')

    def assert_typed_failure_record(recorder,record,expected_operation,check_prefix,*,
                                    expected_exception_type,expected_errno,expected_strerror,
                                    expected_filenames,path_values,capture_complete=True,
                                    expected_authoritative=False):
        def qualified_type(value):
            return type(value).__module__+'.'+type(value).__qualname__

        require(record is not None,check_prefix+'.failure_record_present')
        require(record.get('operation_id')==expected_operation,check_prefix+'.operation_id')
        require(record.get('authoritative') is expected_authoritative,
                check_prefix+'.authoritative')
        require(record.get('expected_exception') is False,
                check_prefix+'.expected_exception_false')
        require(record.get('handled_by_existing_fallback') is False,
                check_prefix+'.handled_by_existing_fallback_false')
        require(record.get('case_id')=='reporting',check_prefix+'.case_id')
        expected_phase={'report.emergency.stderr':'terminal output',
                        'report.emergency.close':'cleanup',
                        'report.emergency.serialize':'report generation',
                        'report.emergency.secondary_serialize':'report generation',
                        'runner.result.print':'terminal output'}.get(
                            expected_operation,'report publication')
        require(record.get('phase')==expected_phase,check_prefix+'.phase')
        expected_description={
            'report.emergency.stderr':'emit complete structured emergency evidence',
            'report.emergency.open':'open designated no-clobber emergency evidence',
            'report.emergency.write':'write designated no-clobber emergency evidence',
            'report.emergency.fsync':'fsync designated no-clobber emergency evidence',
            'report.emergency.close':'close designated emergency evidence descriptor',
            'report.emergency.serialize':'serialize structured emergency evidence',
            'report.emergency.secondary_serialize':'serialize structured emergency evidence',
            'report.finalize':'ordinary report publication unavailable',
            'runner.result.print':'late result summary write unavailable',
        }[expected_operation]
        require(record.get('description')==expected_description,
                check_prefix+'.description')

        require(record.get('exception_type')==expected_exception_type,
                check_prefix+'.exception_type')
        original_exception=next((exc for exc,item in recorder.original_exceptions
                                 if item.get('failure_id')==record.get('failure_id')),None)
        require(original_exception is not None,check_prefix+'.original_exception_retained')
        require(qualified_type(original_exception)==expected_exception_type,
                check_prefix+'.original_exception_type')
        require(isinstance(original_exception,OSError),check_prefix+'.oserror_family_membership')
        expected_args=(expected_errno,expected_strerror)
        require(type(original_exception.args) is tuple,check_prefix+'.original_args_type')
        require(original_exception.args==expected_args,check_prefix+'.original_args')

        require(record.get('errno_present') is True,check_prefix+'.errno_present')
        require(record.get('errno_capture_status')=='ok',check_prefix+'.errno_capture_status')
        require(type(record.get('errno')) is int,check_prefix+'.errno_type')
        require(record.get('errno')==expected_errno,check_prefix+'.errno')
        require(record.get('strerror_present') is True,check_prefix+'.strerror_present')
        require(record.get('strerror_capture_status')=='ok',check_prefix+'.strerror_capture_status')
        require(type(record.get('strerror')) is str,check_prefix+'.strerror_type')
        require(record.get('strerror')==expected_strerror,check_prefix+'.strerror')

        require(record.get('full_traceback_capture_status')=='ok',
                check_prefix+'.full_traceback_capture_status')
        require(type(record.get('full_traceback')) is str,check_prefix+'.full_traceback_type')
        require(bool(record.get('full_traceback')),check_prefix+'.full_traceback')
        require(type(record.get('traceback_lines')) is list,check_prefix+'.traceback_lines_type')
        require(bool(record.get('traceback_lines')),check_prefix+'.traceback_lines_present')
        require(''.join(record['traceback_lines'])==record.get('full_traceback'),
                check_prefix+'.traceback_lines_match_full_traceback')
        require(record.get('args_repr_capture_status')=='ok',check_prefix+'.args_repr_capture_status')
        require(type(record.get('args')) is tuple,check_prefix+'.args_type')
        require(record.get('args')==expected_args,check_prefix+'.args')
        require(type(record.get('args_repr')) is str,check_prefix+'.args_repr_type')
        require(record.get('args_repr')==repr(expected_args),check_prefix+'.args_repr')
        timestamp=record.get('timestamp')
        require(isinstance(timestamp,dict),check_prefix+'.timestamp_present')
        require(timestamp.get('status')=='ok',check_prefix+'.timestamp_capture_status')
        require(type(timestamp.get('utc')) is str,check_prefix+'.timestamp_utc_type')
        require(type(timestamp.get('unix')) is float,check_prefix+'.timestamp_unix_type')
        require(type(timestamp.get('monotonic_ns')) is int,check_prefix+'.timestamp_monotonic_ns_type')
        capture_errors=record.get('evidence_capture_failures')
        require(type(capture_errors) is list,check_prefix+'.evidence_capture_failures_type')
        if capture_complete:
            require(capture_errors==[],check_prefix+'.evidence_capture_failures_empty')

        for field,expected_value in zip(('filename','filename2'),expected_filenames):
            require(record.get(field+'_present') is True,check_prefix+'.'+field+'.present')
            require(record.get(field+'_capture_status')=='ok',check_prefix+'.'+field+'.capture_status')
            require(record.get(field+'_type')==qualified_type(expected_value),
                    check_prefix+'.'+field+'.type_label')
            require(type(record.get(field)) is type(expected_value),check_prefix+'.'+field+'.runtime_type')
            require(record.get(field)==expected_value,check_prefix+'.'+field+'.value')

        assert_typed_paths(record,check_prefix,path_values)

        pin=record.get('root_pin_identity_if_live')
        require(isinstance(pin,dict),check_prefix+'.root_pin.present')
        require(pin.get('status')=='ok',check_prefix+'.root_pin.status')
        require(pin.get('identity')==(123,1001,stat.S_IFDIR),check_prefix+'.root_pin.identity')
        require(pin.get('fields',{}).get('device')==123,check_prefix+'.root_pin.device')
        require(pin.get('fields',{}).get('inode')==1001,check_prefix+'.root_pin.inode')
        require(pin.get('fields',{}).get('file_type')==stat.S_IFDIR,check_prefix+'.root_pin.file_type')
        helper=record.get('helper_identity_if_live')
        require(isinstance(helper,dict),check_prefix+'.helper.present')
        require(helper.get('status')=='known',check_prefix+'.helper.status')
        require(helper.get('pid')==6201,check_prefix+'.helper.pid')
        require(helper.get('ready_identity',{}).get('device')==123,check_prefix+'.helper.ready_device')
        require(helper.get('ready_identity',{}).get('inode')==1001,check_prefix+'.helper.ready_inode')
        scanner_child=record.get('scanner_child_identity_if_known')
        require(isinstance(scanner_child,dict),check_prefix+'.scanner_child.present')
        require(scanner_child.get('status')=='not_dispatched',
                check_prefix+'.scanner_child.not_dispatched')
        after=record.get('relevant_object_identities_after_or_failure')
        before=record.get('relevant_object_identities_before')
        require(isinstance(after,dict),check_prefix+'.failure_snapshot.present')
        if capture_complete:
            require(after.get('data_origin')=='SYNTHETIC_ONLY',
                    check_prefix+'.failure_snapshot.data_origin')
            require(after.get('stage')=='failure',
                    check_prefix+'.failure_snapshot.stage')
        else:
            require(after.get('status')=='snapshot_failed',
                    check_prefix+'.failure_snapshot.status')
            require(isinstance(before,dict),check_prefix+'.before_snapshot.present')
            require(before.get('data_origin')=='SYNTHETIC_ONLY',
                    check_prefix+'.before_snapshot_preserved')
        require(record.get('evidence_complete') is capture_complete,
                check_prefix+'.record_evidence_complete')
        return record

    def expected_terminal_exception(operation_id,check_prefix,destination):
        if operation_id=='report.emergency.open':
            return ('builtins.PermissionError',13,'R46 injected terminal open failure',
                    (os.fsencode(destination),b'/synthetic/terminal/secondary-open'))
        if operation_id=='report.emergency.write':
            return ('builtins.OSError',28,'R46 injected terminal write failure',
                    (b'/synthetic/terminal/write',b'/synthetic/terminal/secondary-write'))
        if operation_id=='report.emergency.fsync':
            return ('builtins.OSError',5,'R46 injected terminal fsync failure',
                    (b'/synthetic/terminal/fsync',b'/synthetic/terminal/secondary-fsync'))
        if operation_id=='report.emergency.close':
            return ('builtins.OSError',9,'R46 injected terminal close failure',
                    (b'/synthetic/terminal/close',b'/synthetic/terminal/secondary-close'))
        if operation_id=='report.emergency.stderr':
            stage='stderr_write' if check_prefix.startswith('T8.') else 'stderr_flush'
            return ('builtins.OSError',5,'R46 injected terminal '+stage+' failure',
                    (b'/synthetic/terminal/'+stage.encode('ascii'),
                     b'/synthetic/terminal/secondary-'+stage.encode('ascii')))
        if operation_id in {'report.emergency.serialize','report.emergency.secondary_serialize'}:
            return ('builtins.OSError',5,'R46 injected structured serialization failure',
                    (b'/synthetic/serialization/payload',b'/synthetic/serialization/secondary'))
        if operation_id=='report.finalize':
            return ('builtins.OSError',28,'R46 synthetic ordinary report publication failure',
                    (b'/synthetic/reports/report.json',b'/synthetic/reports/report.md'))
        if operation_id=='runner.result.print':
            return ('builtins.OSError',5,'R46 synthetic late summary failure',
                    (b'/synthetic/summary/result',b'/synthetic/summary/secondary'))
        raise AssertionError('no synthetic exception expectation for '+operation_id)

    for spec in terminal_specs:
        name=spec['name']
        check_results=[]
        active_terminal_assertions=check_results
        state['active_terminal_sequence']=name
        state['active_terminal_assertions']=check_results
        test_report={'schema_version':3,'result':'SYNTHETIC_TERMINAL_FAILURE','data_origin':'SYNTHETIC_ONLY',
                     'cases':[],'first_failure':None,'terminal_sequence':name}
        recorder=FailureRecorder(test_report,run_id+'-'+name.lower())
        recorder.set_case('reporting',suite_root=b'/synthetic/suite',
                           case_root=b'/synthetic/suite/case_8',
                           child_path=b'/synthetic/suite/case_8/child\nline.bin')
        observer_state={'remaining':0,'exception':None}
        recorder.last_snapshot=_synthetic_snapshot('before')
        def synthetic_terminal_snapshot(stage):
            if observer_state['remaining'] and stage=='failure':
                observer_state['remaining']-=1
                observer_state['exception']=OSError(5,'R46 injected terminal evidence observer failure',
                                                    b'/synthetic/observer/path',None,b'/synthetic/observer/path2')
                raise observer_state['exception']
            return _synthetic_snapshot(stage),[]
        recorder.snapshot=synthetic_terminal_snapshot

        if spec.get('top_level'):
            primary=record_primary(recorder,'runner.entrypoint','unanticipated entrypoint failure',
                                   RuntimeError('R46 synthetic unexpected top-level exception'),
                                   phase='unexpected generic boundary')
            normal=record_primary(recorder,'report.finalize','ordinary report publication unavailable',
                                  OSError(28,'R46 synthetic ordinary report publication failure',
                                          b'/synthetic/reports/report.json',None,b'/synthetic/reports/report.md'))
            require(test_report['primary_failure'] is primary,name+'.top_level_primary_identity')
            require(normal['authoritative'] is False,name+'.report_failure_non_authoritative')
        else:
            primary=record_primary(recorder,'report.finalize','ordinary report publication unavailable',
                                   OSError(28,'R46 synthetic ordinary report publication failure',
                                           b'/synthetic/reports/report.json',None,b'/synthetic/reports/report.md'))
        primary_id=primary['failure_id']
        if not spec.get('top_level'):
            primary_type,primary_errno,primary_strerror,primary_filenames=expected_terminal_exception(
                'report.finalize',name+'.primary',None)
            primary_paths={'json':b'/synthetic/reports/report.json',
                           'markdown':b'/synthetic/reports/report.md'}
            assert_typed_failure_record(recorder,primary,'report.finalize',name+'.primary',
                                        expected_exception_type=primary_type,expected_errno=primary_errno,
                                        expected_strerror=primary_strerror,expected_filenames=primary_filenames,
                                        path_values=primary_paths,expected_authoritative=True)
            require(primary is test_report['primary_failure'],name+'.primary_record_identity')
            require(primary in test_report['failure_records'],name+'.primary_record_in_failure_records')
            require(primary not in test_report['secondary_failures'],name+'.primary_not_secondary')
        if spec.get('observer_failure'):
            require(primary['evidence_complete'] is True,
                    'T9.primary_complete_before_observer_injection')
            require(observer_state['exception'] is None,
                    'T9.observer_not_injected_during_primary_record')
            observer_state['remaining']=1
            original_emergency_payload=recorder.emergency_payload
            serialization_faults={'remaining':2}
            def injected_emergency_payload(*,terminal_publication=None):
                if (terminal_publication and terminal_publication.get('emission_generation')==2 and
                        serialization_faults['remaining']):
                    serialization_faults['remaining']-=1
                    raise OSError(5,'R46 injected structured serialization failure',
                                  b'/synthetic/serialization/payload',None,
                                  b'/synthetic/serialization/secondary')
                return original_emergency_payload(terminal_publication=terminal_publication)
            recorder.emergency_payload=injected_emergency_payload

        terminal_io=SyntheticTerminalIO(spec['faults'])
        terminal_state=terminal_state_factory()
        terminal_reporter(recorder=recorder,run_id=recorder.run_id,
                          destination_directory=output_directory,
                          terminal_io=terminal_io,terminal_state=terminal_state)
        later_boundary=None
        if spec.get('top_level'):
            # The actual runner can discover a result/summary failure after
            # the first terminal publication. Reuse the same run-scoped state
            # and fake sinks to prove a later generation still reaches stderr.
            later_boundary=record_primary(recorder,'runner.result.print','late result summary write unavailable',
                                          OSError(5,'R46 synthetic late summary failure',
                                                  b'/synthetic/summary/result',None,
                                                  b'/synthetic/summary/secondary'),
                                          paths={'stdout':b'/synthetic/summary/result'},
                                          phase='terminal output')
            terminal_reporter(recorder=recorder,run_id=recorder.run_id,
                              destination_directory=output_directory,
                              terminal_io=terminal_io,terminal_state=terminal_state)
        terminal_state=test_report['terminal_publication']
        require(terminal_state['schema']=='r6a-r46-terminal-publication-v1',name+'.terminal_state_schema')
        require(test_report['primary_failure'] is primary,name+'.primary_record_identity')
        require(primary['authoritative'] is True,name+'.primary_authoritative')
        require(not any(guards.values()),name+'.no_scanner_or_subprocess_dispatch')

        raw_files=[]
        for sink_name,emissions in [('stderr',terminal_io.stderr_accepted)]:
            for index,raw in enumerate(emissions,1):
                path=capture_directory/f'{name}-{sink_name}-{index:02d}.bin'
                path.write_bytes(raw)
                raw_files.append({'sink':sink_name,'relative_path':path.relative_to(evidence_directory).as_posix(),
                                  'size_bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'accepted':True})
        file_raw=bytes(terminal_io.file_bytes)
        file_path=capture_directory/f'{name}-emergency-file.bin'
        file_path.write_bytes(file_raw)
        raw_files.append({'sink':'emergency_file','relative_path':file_path.relative_to(evidence_directory).as_posix(),
                          'size_bytes':len(file_raw),'sha256':hashlib.sha256(file_raw).hexdigest(),
                          'accepted':terminal_state['terminal_sink_results']['emergency_file']['status']=='succeeded'})
        for index,attempt in enumerate(terminal_io.stderr_attempts,1):
            if not attempt['accepted']:
                path=capture_directory/f'{name}-stderr-attempt-{index:02d}.bin'
                path.write_bytes(attempt['attempted_bytes'])
                raw_files.append({'sink':'stderr_attempt','relative_path':path.relative_to(evidence_directory).as_posix(),
                                  'size_bytes':len(attempt['attempted_bytes']),
                                  'sha256':hashlib.sha256(attempt['attempted_bytes']).hexdigest(),'accepted':False})

        delivered=[]
        for index,raw in enumerate(terminal_io.stderr_accepted,1):
            payload_raw=raw[len(b'R46_STRUCTURED_FAILURE='):]
            delivered.append({'sink':'stderr','raw_sha256':hashlib.sha256(payload_raw).hexdigest(),
                              'sink_capture_sha256':hashlib.sha256(raw).hexdigest(),
                              'payload':decode_emission(raw,stderr=True,
                                  check_prefix=f'{name}.stderr_payload_{index}')})
        if terminal_state['terminal_sink_results']['emergency_file']['status']=='succeeded':
            delivered.append({'sink':'emergency_file','raw_sha256':hashlib.sha256(file_raw).hexdigest(),
                              'sink_capture_sha256':hashlib.sha256(file_raw).hexdigest(),
                              'payload':decode_emission(file_raw,stderr=False,
                                  check_prefix=f'{name}.emergency_file_payload')})
        latest_generation=terminal_state['emission_generation']
        latest=[item for item in delivered
                if item['payload']['terminal_publication']['emission_generation']==latest_generation]

        history_by_generation={item['emission_generation']:item for item in terminal_state['generation_history']}
        for item in delivered:
            generation=item['payload']['terminal_publication']['emission_generation']
            require(generation in history_by_generation,
                    f'{name}.generation_{generation}.history_entry_present')
            history=history_by_generation[generation]
            require(item['raw_sha256']==history['payload_sha256'],
                    f'{name}.generation_{generation}.payload_hash')
            require(item['payload']['terminal_publication']['known_failure_ids']==history['known_failure_ids'],
                    f'{name}.generation_{generation}.known_failure_chronology')

        if name in {'T1','T2','T3','T4','T5','T6','T7'}:
            expected_op=spec['expected_failure']
            sink_record=next((item for item in test_report['failure_records']
                              if item.get('operation_id')==expected_op),None)
            emergency_path=Path(output_directory)/f'R6-A-R46-EMERGENCY-FAILURE-{recorder.run_id}.json'
            expected_type,expected_errno,expected_strerror,expected_filenames=expected_terminal_exception(
                expected_op,name+'.sink',emergency_path)
            assert_typed_failure_record(recorder,sink_record,expected_op,name,
                                   expected_exception_type=expected_type,expected_errno=expected_errno,
                                   expected_strerror=expected_strerror,expected_filenames=expected_filenames,
                                   path_values={'emergency_file':emergency_path})
            require(sink_record['failure_id']!=primary_id,name+'.secondary_not_primary')
            require(sink_record in test_report['secondary_failures'],name+'.secondary_failure_present')
            require(terminal_state['latest_generation_delivered']==latest_generation,
                    name+'.latest_generation_delivered')
            require(bool(latest),name+'.latest_payload_present')
            require(latest_generation==2,name+'.emission_generation')
            newest=latest[-1]['payload']
            payload_sink_record=next((item for item in newest['failure_records']
                                      if item.get('failure_id')==sink_record['failure_id']),None)
            require(payload_sink_record is not None,name+'.latest_payload_contains_secondary')
            require(any(item.get('failure_id')==sink_record['failure_id']
                        for item in newest.get('secondary_failures',[])),
                    name+'.latest_payload_secondary_owner')
            payload_check_prefix='T1.latest_payload' if name=='T1' else name+'.latest_payload'
            assert_typed_failure_record(recorder,payload_sink_record,expected_op,payload_check_prefix,
                                   expected_exception_type=expected_type,expected_errno=expected_errno,
                                   expected_strerror=expected_strerror,expected_filenames=expected_filenames,
                                   path_values={'emergency_file':emergency_path})

            first_generation=next(item for item in terminal_state['generation_history']
                                  if item['emission_generation']==1)
            require(first_generation['known_failure_ids']==[primary_id],name+'.generation_1_primary_only')
            require([item['emission_generation'] for item in terminal_state['generation_history']]==[1,2],
                    name+'.generation_history_sequence')
            require(sink_record['failure_id'] in terminal_state['generation_history'][-1]['known_failure_ids'],
                    name+'.latest_generation_contains_secondary')
            require(terminal_state['evidence_status']=='EVIDENCE_COMPLETE',name+'.evidence_status')
            require(terminal_state['evidence_complete'] is True,
                    'T1.evidence_complete' if name=='T1' else name+'.evidence_complete')

            if name=='T5':
                expected_attempts=[('stderr',1,'failed'),('emergency_file',2,'succeeded')]
                require(terminal_state['terminal_sink_results']['stderr']['status']=='failed',
                        'T5.stderr_sink_failed')
                require(terminal_state['terminal_sink_results']['emergency_file']['status']=='succeeded',
                        'T5.emergency_file_sink_succeeded')
            else:
                expected_attempts=[('stderr',1,'succeeded'),
                                   ('emergency_file',1,'failed'),
                                   ('stderr',2,'succeeded')]
                require(terminal_state['terminal_sink_results']['stderr']['status']=='available',
                        name+'.stderr_sink_remains_available')
                require(terminal_state['terminal_sink_results']['emergency_file']['status']=='failed',
                        name+'.emergency_file_sink_failed')
            require(len(terminal_state['terminal_sink_attempts'])==len(expected_attempts),
                    name+'.terminal_attempt_count')
            for index,(attempt,expected_attempt) in enumerate(
                    zip(terminal_state['terminal_sink_attempts'],expected_attempts),1):
                require(attempt.get('sink')==expected_attempt[0],
                        f'{name}.terminal_attempt_{index}.sink')
                require(attempt.get('emission_generation')==expected_attempt[1],
                        f'{name}.terminal_attempt_{index}.generation')
                require(attempt.get('status')==expected_attempt[2],
                        f'{name}.terminal_attempt_{index}.status')

            expected_accepted={'T5':{'stderr':[],'emergency_file':[2]}}
            expected_accepted.setdefault(name,{'stderr':[1,2],'emergency_file':[]})
            for sink_name in ('stderr','emergency_file'):
                accepted_generations=[item['payload']['terminal_publication']['emission_generation']
                                      for item in delivered if item['sink']==sink_name]
                require(accepted_generations==expected_accepted[name][sink_name],
                        name+'.'+sink_name+'.accepted_generations')
                expected_latest=expected_accepted[name][sink_name][-1] if expected_accepted[name][sink_name] else None
                require(terminal_state['terminal_sink_results'][sink_name]['latest_generation_received']==expected_latest,
                        name+'.'+sink_name+'.latest_generation_received')
            if name=='T1':
                require('stderr' in [item['sink'] for item in latest],
                        'T1.latest_generation_received_by_stderr')
                require(terminal_state['terminal_sink_results']['stderr']['latest_generation_received']==2,
                        'T1.stderr_reemission_succeeded')
            elif name in {'T2','T3','T4','T6','T7'}:
                require('stderr' in [item['sink'] for item in latest],name+'.stderr_reemission_succeeded')
            else:
                require('emergency_file' in [item['sink'] for item in latest],
                        name+'.emergency_file_updated_generation_succeeded')
            if name=='T7':
                close_record=next((item for item in test_report['failure_records']
                                   if item.get('operation_id')=='report.emergency.close'),None)
                close_type,close_errno,close_strerror,close_filenames=expected_terminal_exception(
                    'report.emergency.close','T7.close',emergency_path)
                assert_typed_failure_record(recorder,close_record,'report.emergency.close','T7.close',
                                       expected_exception_type=close_type,expected_errno=close_errno,
                                       expected_strerror=close_strerror,expected_filenames=close_filenames,
                                       path_values={'emergency_file':emergency_path})
                require(close_record in test_report['secondary_failures'],'T7.close.secondary_failure_present')
                require(close_record['failure_id'] in newest['terminal_publication']['known_failure_ids'],
                        'T7.latest_payload_contains_close')
                file_attempt=next((item for item in terminal_state['terminal_sink_attempts']
                                   if item['sink']=='emergency_file'),None)
                require(file_attempt is not None,'T7.emergency_file_attempt_present')
                require(file_attempt['failure_count']==2,'T7.emergency_file_failure_count')
                require([stage['operation_id'] for stage in file_attempt['stages'] if stage['status']=='failed']==
                        ['report.emergency.write','report.emergency.close'],'T7.failed_stage_order')
        elif name=='T8':
            require(terminal_state['evidence_status']=='EVIDENCE_DEGRADED_ALL_SINKS_FAILED',
                    'T8.evidence_status')
            require(terminal_state['evidence_complete'] is False,'T8.evidence_complete')
            require(terminal_state['terminal_sink_results']['stderr']['status']=='failed',
                    'T8.stderr_sink_failed')
            require(terminal_state['terminal_sink_results']['emergency_file']['status']=='failed',
                    'T8.emergency_file_sink_failed')
            require(len(terminal_state['terminal_sink_attempts'])==2,'T8.terminal_attempt_count')
            require([item['sink'] for item in terminal_state['terminal_sink_attempts']]==
                    ['stderr','emergency_file'],'T8.terminal_attempt_order')
            require([item['emission_generation'] for item in terminal_state['terminal_sink_attempts']]==
                    [1,2],'T8.attempted_generation_order')
            require(terminal_state['terminal_sink_attempts'][0].get('status')=='failed',
                    'T8.stderr_attempt_failed')
            require(terminal_state['terminal_sink_attempts'][0].get('failure_operation_id')==
                    'report.emergency.stderr','T8.stderr_attempt_failure_operation_id')
            require(terminal_state['terminal_sink_attempts'][1].get('status')=='failed',
                    'T8.emergency_file_attempt_failed')
            require(latest_generation==3,'T8.final_in_memory_generation')
            require(test_report['primary_failure'] is primary,'T8.primary_record_identity')
            require(primary['failure_id']==primary_id,'T8.primary_failure_id')
            require(primary['authoritative'] is True,'T8.primary_authoritative')
            require(len(terminal_state['generation_history'])==3,'T8.generation_history_length')
            require(terminal_state['latest_generation_delivered'] is None,'T8.no_latest_generation_delivered')
            require(not latest,'T8.no_delivered_latest_payload')
            emergency_path=Path(output_directory)/f'R6-A-R46-EMERGENCY-FAILURE-{recorder.run_id}.json'
            failed_records={}
            for operation_id in ('report.emergency.stderr','report.emergency.open'):
                failed=next((item for item in test_report['failure_records']
                             if item.get('operation_id')==operation_id),None)
                prefix='T8.stderr' if operation_id=='report.emergency.stderr' else 'T8.open'
                expected_type,expected_errno,expected_strerror,expected_filenames=expected_terminal_exception(
                    operation_id,prefix,emergency_path)
                assert_typed_failure_record(recorder,failed,operation_id,prefix,
                                       expected_exception_type=expected_type,expected_errno=expected_errno,
                                       expected_strerror=expected_strerror,expected_filenames=expected_filenames,
                                       path_values={'emergency_file':emergency_path})
                require(failed in test_report['secondary_failures'],prefix+'.secondary_failure_present')
                failed_records[operation_id]=failed
            require(terminal_state['generation_history'][0]['known_failure_ids']==[primary_id],
                    'T8.generation_1_primary_only')
            require([item['emission_generation'] for item in terminal_state['generation_history']]==[1,2,3],
                    'T8.generation_history_sequence')
            require(failed_records['report.emergency.stderr']['failure_id'] not in
                    terminal_state['generation_history'][0]['known_failure_ids'],
                    'T8.generation_1_excludes_stderr_failure')
            require(failed_records['report.emergency.open']['failure_id'] not in
                    terminal_state['generation_history'][0]['known_failure_ids'],
                    'T8.generation_1_excludes_open_failure')
            require(failed_records['report.emergency.stderr']['failure_id'] in
                    terminal_state['generation_history'][-1]['known_failure_ids'],
                    'T8.generation_3_contains_stderr_failure')
            require(failed_records['report.emergency.open']['failure_id'] in
                    terminal_state['generation_history'][-1]['known_failure_ids'],
                    'T8.generation_3_contains_open_failure')
            require(test_report['primary_failure'] is primary,'T8.primary_owner_preserved')
            require(primary['authoritative'] is True,'T8.primary_still_authoritative')
            require(terminal_io.stderr_accepted==[],'T8.no_stderr_payload_accepted')
            require(terminal_state['terminal_sink_results']['emergency_file']['latest_generation_received'] is None,
                    'T8.no_emergency_file_generation_accepted')
            require(terminal_state['raw_traceback_attempted'] is True,'T8.raw_traceback_attempted')
            require(len(terminal_io.raw_tracebacks)==1,'T8.raw_traceback_attempt_count')
            require(terminal_state['raw_traceback_status']=='best_effort_attempted','T8.raw_traceback_status')
        elif name=='T9':
            sink_record=next((item for item in test_report['failure_records']
                              if item.get('operation_id')=='report.emergency.open'),None)
            require(sink_record is not None,'T9.open_failure_record_present')
            require(bool(sink_record['evidence_capture_failures']),'T9.nested_observer_failure_present')
            nested=sink_record['evidence_capture_failures'][0]
            require(nested.get('parent_failure_id')==sink_record['failure_id'],'T9.observer.parent_failure_id')
            require(nested.get('full_traceback_capture_status')=='ok','T9.observer.full_traceback_capture_status')
            require(bool(nested.get('full_traceback')),'T9.observer.full_traceback')
            require(nested.get('case_id')=='reporting','T9.observer.case_id')
            require(nested.get('phase')=='evidence capture','T9.observer.phase')
            require(nested.get('operation_id')==sink_record['operation_id']+'.evidence.failure.snapshot',
                    'T9.observer.operation_id')
            observer_exception=observer_state['exception']
            require(type(observer_exception) is OSError,'T9.observer.exception_type')
            require(isinstance(observer_exception,OSError),'T9.observer.oserror_family_membership')
            require(type(observer_exception.args) is tuple,'T9.observer.original_args_type')
            require(observer_exception.args==(5,'R46 injected terminal evidence observer failure'),
                    'T9.observer.original_args')
            require(nested.get('exception_type')=='builtins.OSError','T9.observer.recorded_exception_type')
            require(nested.get('errno_present') is True,'T9.observer.errno_present')
            require(nested.get('errno_capture_status')=='ok','T9.observer.errno_capture_status')
            require(type(nested.get('errno')) is int,'T9.observer.errno_type')
            require(nested.get('errno')==5,'T9.observer.errno')
            require(nested.get('strerror_present') is True,'T9.observer.strerror_present')
            require(nested.get('strerror_capture_status')=='ok','T9.observer.strerror_capture_status')
            require(type(nested.get('strerror')) is str,'T9.observer.strerror_type')
            require(nested.get('strerror')=='R46 injected terminal evidence observer failure',
                    'T9.observer.strerror')
            require(nested.get('args_repr_capture_status')=='ok','T9.observer.args_repr_capture_status')
            require(type(nested.get('args')) is tuple,'T9.observer.args_type')
            require(nested.get('args')==(5,'R46 injected terminal evidence observer failure'),
                    'T9.observer.args')
            require(type(nested.get('args_repr')) is str,'T9.observer.args_repr_type')
            require(nested.get('args_repr')==repr(observer_exception.args),'T9.observer.args_repr')
            observer_timestamp=nested.get('timestamp')
            require(isinstance(observer_timestamp,dict),'T9.observer.timestamp_present')
            require(observer_timestamp.get('status')=='ok','T9.observer.timestamp_status')
            require(type(observer_timestamp.get('utc')) is str,'T9.observer.timestamp_utc_type')
            require(type(observer_timestamp.get('unix')) is float,'T9.observer.timestamp_unix_type')
            require(type(observer_timestamp.get('monotonic_ns')) is int,
                    'T9.observer.timestamp_monotonic_ns_type')
            require(nested.get('filename_present') is True,'T9.observer.filename_present')
            require(nested.get('filename_capture_status')=='ok','T9.observer.filename_capture_status')
            require(nested.get('filename_type')=='builtins.bytes','T9.observer.filename_type')
            require(type(nested.get('filename')) is bytes,'T9.observer.filename_runtime_type')
            require(nested.get('filename')==b'/synthetic/observer/path','T9.observer.filename')
            require(nested.get('filename2_present') is True,'T9.observer.filename2_present')
            require(nested.get('filename2_capture_status')=='ok','T9.observer.filename2_capture_status')
            require(nested.get('filename2_type')=='builtins.bytes','T9.observer.filename2_type')
            require(type(nested.get('filename2')) is bytes,'T9.observer.filename2_runtime_type')
            require(nested.get('filename2')==b'/synthetic/observer/path2','T9.observer.filename2')
            require(nested.get('evidence_complete') is True,'T9.observer.evidence_complete')
            require(sink_record['evidence_complete'] is False,'T9.open_record_evidence_incomplete')

            emergency_path=Path(output_directory)/f'R6-A-R46-EMERGENCY-FAILURE-{recorder.run_id}.json'
            open_failure_type,open_failure_errno,open_failure_strerror,open_failure_filenames=expected_terminal_exception(
                'report.emergency.open','T9.open',emergency_path)
            assert_typed_failure_record(recorder,sink_record,'report.emergency.open','T9.open',
                                   expected_exception_type=open_failure_type,expected_errno=open_failure_errno,
                                   expected_strerror=open_failure_strerror,expected_filenames=open_failure_filenames,
                                   path_values={'emergency_file':emergency_path},capture_complete=False)
            require(sink_record in test_report['secondary_failures'],'T9.open_secondary_failure_present')
            require(sink_record['failure_id']!=primary_id,'T9.open_failure_not_primary')
            require(terminal_state['latest_generation_delivered']==latest_generation,
                    'T9.latest_generation_delivered')
            require(bool(latest),'T9.latest_payload_present')
            require(len(latest)==1,'T9.latest_payload_count')
            require(latest[0]['sink']=='stderr','T9.fallback_sink_is_stderr')
            require(terminal_state['evidence_status']=='EVIDENCE_DEGRADED_CAPTURE_INCOMPLETE',
                    'T9.evidence_status')
            require(terminal_state['evidence_complete'] is False,'T9.evidence_complete')
            require(terminal_state['generation_history'][-1].get('fallback_schema')==
                    'r6a-r46-terminal-failure-minimal-v1','T9.fallback_generation_schema')
            require(bool(terminal_state['reporter_errors']),'T9.reporter_errors_present')
            require(len(terminal_io.stderr_accepted)==2,'T9.stderr_accepted_generation_count')
            require([item['payload']['terminal_publication']['emission_generation']
                     for item in delivered if item['sink']=='stderr']==[1,2],
                    'T9.stderr_generation_sequence')
            require(terminal_state['terminal_sink_results']['stderr']['latest_generation_received']==2,
                    'T9.stderr_latest_generation_received')
            require(terminal_state['terminal_sink_results']['emergency_file']['status']=='failed',
                    'T9.emergency_file_sink_failed')
            require(terminal_state['terminal_sink_results']['stderr']['status']=='available',
                    'T9.stderr_sink_remains_available')
            expected_attempts=[('stderr',1,'succeeded'),('emergency_file',1,'failed'),
                               ('stderr',2,'succeeded')]
            require(len(terminal_state['terminal_sink_attempts'])==len(expected_attempts),
                    'T9.terminal_attempt_count')
            for index,(attempt,expected_attempt) in enumerate(
                    zip(terminal_state['terminal_sink_attempts'],expected_attempts),1):
                require(attempt.get('sink')==expected_attempt[0],
                        f'T9.terminal_attempt_{index}.sink')
                require(attempt.get('emission_generation')==expected_attempt[1],
                        f'T9.terminal_attempt_{index}.generation')
                require(attempt.get('status')==expected_attempt[2],
                        f'T9.terminal_attempt_{index}.status')
            fallback_raw=terminal_io.stderr_accepted[-1][len(b'R46_STRUCTURED_FAILURE='):]
            fallback_envelope=json.loads(fallback_raw.decode('ascii'))
            require(fallback_envelope.get('fallback_schema')=='r6a-r46-terminal-failure-minimal-v1',
                    'T9.fallback_envelope_schema')
            require(latest[0]['payload'].get('fallback_projection_complete') is False,
                    'T9.fallback_projection_complete_false')
            require(latest[0]['payload']['terminal_publication']['evidence_complete'] is False,
                    'T9.fallback_terminal_evidence_complete_false')
            serializer_records=[]
            for operation_id in ('report.emergency.serialize','report.emergency.secondary_serialize'):
                serializer_record=next((item for item in test_report['failure_records']
                                        if item.get('operation_id')==operation_id),None)
                prefix='T9.serialize' if operation_id=='report.emergency.serialize' else 'T9.secondary_serialize'
                serializer_type,serializer_errno,serializer_strerror,serializer_filenames=expected_terminal_exception(
                    operation_id,prefix,emergency_path)
                assert_typed_failure_record(recorder,serializer_record,operation_id,prefix,
                                       expected_exception_type=serializer_type,expected_errno=serializer_errno,
                                       expected_strerror=serializer_strerror,expected_filenames=serializer_filenames,
                                       path_values={'emergency_file':emergency_path})
                require(serializer_record in test_report['secondary_failures'],prefix+'.secondary_failure_present')
                serializer_records.append(serializer_record)
            latest_parent=next((record for record in latest[0]['payload']['failure_records']
                                if record.get('failure_id')==sink_record['failure_id']),None)
            require(latest_parent is not None,'T9.fallback_parent_record_present')
            require(latest_parent.get('operation_id')=='report.emergency.open','T9.fallback_parent_operation_id')
            require(latest_parent.get('exception_type')=='builtins.PermissionError','T9.fallback_parent_exception_type')
            require(latest_parent.get('errno')==13,'T9.fallback_parent_errno')
            require(latest_parent.get('filename')==open_failure_filenames[0],'T9.fallback_parent_filename')
            require(latest_parent.get('filename2')==open_failure_filenames[1],'T9.fallback_parent_filename2')
            require(latest_parent.get('full_traceback')==sink_record['full_traceback'],
                    'T9.fallback_parent_full_traceback')
            require(latest_parent.get('evidence_complete') is False,'T9.fallback_parent_evidence_complete')
            omissions=latest_parent.get('fallback_projection_omissions')
            require(type(omissions) is list,'T9.fallback_projection_omissions_type')
            require(omissions==[],
                    'T9.fallback_projection_omissions_empty')
            fallback_nested=next((item for item in latest_parent.get('evidence_capture_failures',[])
                                  if item.get('failure_id')==nested['failure_id']),None)
            require(fallback_nested is not None,'T9.fallback_nested_observer_present')
            require(fallback_nested.get('parent_failure_id')==sink_record['failure_id'],
                    'T9.fallback_nested_parent_failure_id')
            require(fallback_nested.get('exception_type')=='builtins.OSError',
                    'T9.fallback_nested_exception_type')
            require(fallback_nested.get('errno')==5,'T9.fallback_nested_errno')
            require(fallback_nested.get('filename')==b'/synthetic/observer/path',
                    'T9.fallback_nested_filename')
            require(fallback_nested.get('filename2')==b'/synthetic/observer/path2',
                    'T9.fallback_nested_filename2')
            require(fallback_nested.get('evidence_complete') is True,'T9.fallback_nested_evidence_complete')
            for serializer_record in serializer_records:
                payload_serializer=next((item for item in latest[0]['payload']['failure_records']
                                         if item.get('failure_id')==serializer_record['failure_id']),None)
                prefix='T9.fallback_'+serializer_record['operation_id'].rsplit('.',1)[-1]
                require(payload_serializer is not None,prefix+'.record_present')
                require(payload_serializer.get('operation_id')==serializer_record['operation_id'],
                        prefix+'.operation_id')
                require(payload_serializer.get('exception_type')=='builtins.OSError',prefix+'.exception_type')
                require(payload_serializer.get('errno')==5,prefix+'.errno')
                require(payload_serializer.get('filename')==b'/synthetic/serialization/payload',
                        prefix+'.filename')
                require(payload_serializer.get('filename2')==b'/synthetic/serialization/secondary',
                        prefix+'.filename2')
                require(bool(payload_serializer.get('full_traceback')),prefix+'.full_traceback')
                require(payload_serializer.get('evidence_complete') is True,prefix+'.evidence_complete')
            pin=latest_parent['root_pin_identity_if_live']
            require(pin.get('identity')==(123,1001,stat.S_IFDIR),'T9.fallback_root_pin_identity')
            require(pin.get('fields',{}).get('inode')==1001,'T9.fallback_root_pin_inode')
            helper=latest_parent['helper_identity_if_live']
            require(helper.get('pid')==6201,'T9.fallback_helper_pid')
            require(helper.get('ready_identity',{}).get('inode')==1001,'T9.fallback_helper_inode')
            require(latest_parent['scanner_child_identity_if_known'].get('status')=='not_dispatched',
                    'T9.fallback_scanner_child_not_dispatched')
            require(latest_parent['relevant_object_identities_before'].get('data_origin')=='SYNTHETIC_ONLY',
                    'T9.fallback_before_snapshot_preserved')
            require(latest_parent['relevant_object_identities_after_or_failure'].get('status')=='snapshot_failed',
                    'T9.fallback_after_snapshot_failure_retained')
            require(test_report['primary_failure'] is primary,'T9.primary_record_identity')
            require(primary['authoritative'] is True,'T9.primary_authoritative')
            require(terminal_state['raw_traceback_attempted'] is True,'T9.raw_traceback_attempted')
            require(len(terminal_io.raw_tracebacks)==1,'T9.raw_traceback_attempt_count')
            require(terminal_state['raw_traceback_status']=='best_effort_attempted','T9.raw_traceback_status')
        else:
            require(name=='T10','T10.sequence_name')
            require(primary['operation_id']=='runner.entrypoint','T10.primary_operation_id')
            require(primary['exception_type']=='builtins.RuntimeError','T10.primary_exception_type')
            require(primary.get('case_id')=='reporting','T10.primary_case_id')
            require(primary.get('phase')=='unexpected generic boundary','T10.primary_phase')
            require(primary.get('description')=='unanticipated entrypoint failure',
                    'T10.primary_description')
            primary_exception=next((exc for exc,item in recorder.original_exceptions
                                    if item.get('failure_id')==primary['failure_id']),None)
            require(primary_exception is not None,'T10.primary_original_exception_present')
            require(type(primary_exception) is RuntimeError,'T10.primary_original_exception_type')
            require(not isinstance(primary_exception,OSError),'T10.primary_not_oserror_family')
            require(type(primary_exception.args) is tuple,'T10.primary_original_args_type')
            require(primary_exception.args==('R46 synthetic unexpected top-level exception',),
                    'T10.primary_original_args')
            require(primary['authoritative'] is True,'T10.primary_authoritative')
            require(test_report['primary_failure'] is primary,'T10.primary_record_identity')
            require(primary in test_report['failure_records'],'T10.primary_record_in_failure_records')
            require(primary not in test_report['secondary_failures'],'T10.primary_not_secondary')
            require(primary.get('expected_exception') is False,'T10.primary.expected_exception_false')
            require(primary.get('handled_by_existing_fallback') is False,
                    'T10.primary.handled_by_existing_fallback_false')
            require(primary.get('full_traceback_capture_status')=='ok','T10.primary_full_traceback_capture_status')
            require(type(primary.get('full_traceback')) is str,'T10.primary_full_traceback_type')
            require(bool(primary.get('full_traceback')),'T10.primary_full_traceback')
            require(type(primary.get('traceback_lines')) is list,'T10.primary_traceback_lines_type')
            require(bool(primary.get('traceback_lines')),'T10.primary_traceback_lines_present')
            require(''.join(primary['traceback_lines'])==primary.get('full_traceback'),
                    'T10.primary_traceback_lines_match_full_traceback')
            require(primary.get('args_repr_capture_status')=='ok','T10.primary_args_repr_capture_status')
            require(type(primary.get('args')) is tuple,'T10.primary_args_type')
            require(primary.get('args')==('R46 synthetic unexpected top-level exception',),
                    'T10.primary_args')
            require(type(primary.get('args_repr')) is str,'T10.primary_args_repr_type')
            require(primary.get('args_repr')==repr(primary_exception.args),'T10.primary_args_repr')
            primary_timestamp=primary.get('timestamp')
            require(isinstance(primary_timestamp,dict),'T10.primary_timestamp_present')
            require(primary_timestamp.get('status')=='ok','T10.primary_timestamp_status')
            require(type(primary_timestamp.get('utc')) is str,'T10.primary_timestamp_utc_type')
            require(type(primary_timestamp.get('unix')) is float,'T10.primary_timestamp_unix_type')
            require(type(primary_timestamp.get('monotonic_ns')) is int,
                    'T10.primary_timestamp_monotonic_ns_type')
            require(primary.get('evidence_complete') is True,'T10.primary.evidence_complete')
            assert_typed_paths(primary,'T10.primary',
                               {'json':b'/synthetic/reports/report.json',
                                'markdown':b'/synthetic/reports/report.md'})
            for field in ('errno','strerror','filename','filename2'):
                require(primary.get(field+'_present') is False,'T10.primary.'+field+'_absent')
                require(primary.get(field+'_capture_status')=='absent',
                        'T10.primary.'+field+'_capture_status')
                require(primary.get(field+'_type') is None,'T10.primary.'+field+'_type_none')
                require(primary.get(field) is None,'T10.primary.'+field+'_value_none')
            ordinary=next((item for item in test_report['failure_records']
                           if item.get('operation_id')=='report.finalize'),None)
            require(ordinary is not None,'T10.report_finalize_record_present')
            require(ordinary['failure_id'] in terminal_state['generation_history'][0]['known_failure_ids'],
                    'T10.generation_1_contains_report_failure')
            require(ordinary in test_report['secondary_failures'],'T10.report_finalize_secondary_present')
            require(ordinary['authoritative'] is False,'T10.report_finalize_non_authoritative')
            ordinary_type,ordinary_errno,ordinary_strerror,ordinary_filenames=expected_terminal_exception(
                'report.finalize','T10.report_finalize',None)
            report_paths={'json':b'/synthetic/reports/report.json',
                          'markdown':b'/synthetic/reports/report.md'}
            assert_typed_failure_record(recorder,ordinary,'report.finalize','T10.report_finalize',
                                   expected_exception_type=ordinary_type,expected_errno=ordinary_errno,
                                   expected_strerror=ordinary_strerror,expected_filenames=ordinary_filenames,
                                   path_values=report_paths)
            require(later_boundary is not None,'T10.result_print_record_present')
            require(later_boundary in test_report['secondary_failures'],'T10.result_print_secondary_present')
            require(later_boundary['operation_id']=='runner.result.print','T10.result_print_operation_id')
            require(later_boundary['authoritative'] is False,'T10.result_print_non_authoritative')
            later_type,later_errno,later_strerror,later_filenames=expected_terminal_exception(
                'runner.result.print','T10.result_print',None)
            assert_typed_failure_record(recorder,later_boundary,'runner.result.print','T10.result_print',
                                   expected_exception_type=later_type,expected_errno=later_errno,
                                   expected_strerror=later_strerror,expected_filenames=later_filenames,
                                   path_values={'stdout':b'/synthetic/summary/result'})
            require(terminal_state['emission_generation']==2,'T10.emission_generation')
            require(terminal_state['latest_generation_delivered']==latest_generation,
                    'T10.latest_generation_delivered')
            require(latest_generation==2,'T10.latest_generation')
            require(len(latest)==1,'T10.latest_payload_count')
            require(latest[0]['sink']=='stderr','T10.latest_generation_sink_is_stderr')
            require(terminal_state['terminal_sink_results']['emergency_file']['latest_generation_received']==1,
                    'T10.emergency_file_received_generation_1')
            require(terminal_state['terminal_sink_results']['stderr']['status']=='available',
                    'T10.stderr_sink_remains_available')
            require(terminal_state['terminal_sink_results']['emergency_file']['status']=='succeeded',
                    'T10.emergency_file_sink_succeeded')
            require(len([item for item in terminal_io.file_events if item['operation']=='open'])==1,
                    'T10.emergency_file_open_count_one')
            expected_attempts=[('stderr',1,'succeeded'),('emergency_file',1,'succeeded'),
                               ('stderr',2,'succeeded')]
            require(len(terminal_state['terminal_sink_attempts'])==len(expected_attempts),
                    'T10.terminal_attempt_count')
            for index,(attempt,expected_attempt) in enumerate(
                    zip(terminal_state['terminal_sink_attempts'],expected_attempts),1):
                require(attempt.get('sink')==expected_attempt[0],
                        f'T10.terminal_attempt_{index}.sink')
                require(attempt.get('emission_generation')==expected_attempt[1],
                        f'T10.terminal_attempt_{index}.generation')
                require(attempt.get('status')==expected_attempt[2],
                        f'T10.terminal_attempt_{index}.status')
            accepted_generations={sink_name:[item['payload']['terminal_publication']['emission_generation']
                                             for item in delivered if item['sink']==sink_name]
                                  for sink_name in ('stderr','emergency_file')}
            require(accepted_generations['stderr']==[1,2],'T10.stderr_accepted_generations')
            require(accepted_generations['emergency_file']==[1],'T10.emergency_file_accepted_generations')
            first_generation=next(item for item in terminal_state['generation_history']
                                  if item['emission_generation']==1)
            second_generation=next(item for item in terminal_state['generation_history']
                                   if item['emission_generation']==2)
            require(ordinary['failure_id'] in first_generation['known_failure_ids'],
                    'T10.generation_1_report_failure_chronology')
            require(later_boundary['failure_id'] not in first_generation['known_failure_ids'],
                    'T10.generation_1_excludes_later_summary_failure')
            require(later_boundary['failure_id'] in second_generation['known_failure_ids'],
                    'T10.generation_2_includes_later_summary_failure')
            require(primary_id in second_generation['known_failure_ids'],
                    'T10.generation_2_includes_top_level_failure')
            require(ordinary['failure_id'] in second_generation['known_failure_ids'],
                    'T10.generation_2_includes_report_failure')
            newest=latest[0]['payload']
            latest_primary=next((item for item in newest['failure_records']
                                 if item.get('failure_id')==primary_id),None)
            latest_ordinary=next((item for item in newest['failure_records']
                                  if item.get('failure_id')==ordinary['failure_id']),None)
            latest_boundary=next((item for item in newest['failure_records']
                                  if item.get('failure_id')==later_boundary['failure_id']),None)
            require(latest_primary is not None,'T10.latest_payload_contains_top_level_failure')
            require(latest_primary.get('operation_id')=='runner.entrypoint',
                    'T10.latest_payload_top_level_operation_id')
            require(latest_ordinary is not None,'T10.latest_payload_contains_report_failure')
            require(latest_boundary is not None,'T10.latest_payload_contains_summary_failure')
            require(any(item.get('failure_id')==ordinary['failure_id']
                        for item in newest.get('secondary_failures',[])),
                    'T10.latest_payload_report_failure_secondary_owner')
            require(any(item.get('failure_id')==later_boundary['failure_id']
                        for item in newest.get('secondary_failures',[])),
                    'T10.latest_payload_summary_failure_secondary_owner')
            require(terminal_state['evidence_status']=='EVIDENCE_COMPLETE','T10.evidence_status')
            require(terminal_state['evidence_complete'] is True,'T10.evidence_complete')

        terminal_result={'name':name,'result':'PASS','faults':spec['faults'],
                         'expected_failure_operation_id':spec.get('expected_failure'),
                         'primary_failure_id':primary_id,'terminal_publication':terminal_state,
                         'assertions':check_results,
                         'late_boundary_failure_id':later_boundary['failure_id'] if later_boundary else None,
                         'stderr_attempts':terminal_io.stderr_attempts,
                         'file_events':terminal_io.file_events,'raw_sink_captures':raw_files,
                         'delivered_generations':[item['payload']['terminal_publication']['emission_generation']
                                                  for item in delivered],
                         'delivered_generation_hashes':[{'sink':item['sink'],
                             'emission_generation':item['payload']['terminal_publication']['emission_generation'],
                             'payload_sha256':item['raw_sha256'],'sink_capture_sha256':item['sink_capture_sha256']}
                             for item in delivered],
                         'raw_traceback_count':len(terminal_io.raw_tracebacks)}
        frozen_terminal_evidence=freeze_evidence(terminal_result)
        test_report['terminal_sequence_evidence']=restore_evidence(frozen_terminal_evidence)
        publication_assertions=[]
        active_terminal_assertions=publication_assertions
        state['active_terminal_assertions']=publication_assertions
        artifacts,reconstructed=publish_test('terminal-'+name.lower(),test_report,recorder.run_id,
                                             check_prefix=name+'.publication')
        round_trip=reconstructed['terminal_sequence_evidence']
        round_trip_state=round_trip['terminal_publication']
        require(round_trip_state['evidence_status']==terminal_state['evidence_status'],
                name+'.publication.evidence_status_round_trip')
        require(round_trip_state['evidence_complete'] is terminal_state['evidence_complete'],
                name+'.publication.evidence_complete_round_trip')
        require(round_trip_state['emission_generation']==terminal_state['emission_generation'],
                name+'.publication.emission_generation_round_trip')
        require(len(round_trip_state['generation_history'])==len(terminal_state['generation_history']),
                name+'.publication.generation_history_length_round_trip')
        for expected_history in terminal_state['generation_history']:
            generation=expected_history['emission_generation']
            restored_history=next((item for item in round_trip_state['generation_history']
                                   if item.get('emission_generation')==generation),None)
            require(restored_history is not None,
                    f'{name}.publication.generation_{generation}.history_present')
            require(restored_history.get('known_failure_ids')==expected_history['known_failure_ids'],
                    f'{name}.publication.generation_{generation}.known_failure_ids')
            require(restored_history.get('payload_sha256')==expected_history['payload_sha256'],
                    f'{name}.publication.generation_{generation}.payload_sha256')
        restored_primary=reconstructed.get('primary_failure')
        require(restored_primary is not None,name+'.publication.primary_record_present')
        require(restored_primary.get('failure_id')==primary_id,
                name+'.publication.primary_failure_id')
        require(restored_primary.get('authoritative') is True,
                name+'.publication.primary_authoritative')
        restored_records=reconstructed.get('failure_records',[])
        require(len(restored_records)==len(test_report['failure_records']),
                name+'.publication.failure_record_count')
        for expected_record in test_report['failure_records']:
            failure_id=expected_record['failure_id']
            restored_record=next((item for item in restored_records
                                  if item.get('failure_id')==failure_id),None)
            require(restored_record is not None,
                    f'{name}.publication.{failure_id}.record_present')
            for field in ('operation_id','exception_type','errno_present','errno_capture_status',
                          'errno','strerror_present','strerror_capture_status','strerror',
                          'filename_present','filename_capture_status','filename_type','filename',
                          'filename2_present','filename2_capture_status','filename2_type','filename2',
                          'full_traceback_capture_status','full_traceback','evidence_complete',
                          'authoritative'):
                require(restored_record.get(field)==expected_record.get(field),
                        f'{name}.publication.{failure_id}.{field}_round_trip')
            if failure_id!=primary_id:
                require(failure_id in [item.get('failure_id')
                                       for item in reconstructed.get('secondary_failures',[])],
                        f'{name}.publication.{failure_id}.secondary_owner')
            expected_paths={item.get('label'):item for item in expected_record.get('input_paths',[])}
            restored_paths={item.get('label'):item for item in restored_record.get('input_paths',[])}
            require(set(restored_paths)==set(expected_paths),
                    f'{name}.publication.{failure_id}.typed_path_labels')
            for path_label,expected_path in expected_paths.items():
                restored_path=restored_paths[path_label]
                for field in ('capture_status','runtime_type','fspath_type','fspath',
                              'filesystem_bytes','value'):
                    require(restored_path.get(field)==expected_path.get(field),
                            f'{name}.publication.{failure_id}.{path_label}.{field}_round_trip')
                expected_summary=expected_record.get('path_types',{}).get(path_label)
                restored_summary=restored_record.get('path_types',{}).get(path_label)
                require(restored_summary==expected_summary,
                        f'{name}.publication.{failure_id}.{path_label}.path_type_summary_round_trip')
        terminal_results.append({'terminal_sequence_evidence':restore_evidence(frozen_terminal_evidence),
                                 'publication':artifacts,
                                 'validation_assertions':publication_assertions})
        active_terminal_assertions=None
        state.pop('active_terminal_sequence',None)
        state.pop('active_terminal_assertions',None)

    require(len(terminal_results)==10,'all ten R46 terminal sink sequences are present')
    for terminal_envelope in terminal_results:
        terminal_evidence=terminal_envelope['terminal_sequence_evidence']
        require(terminal_evidence['result']=='PASS',
                terminal_evidence['name']+'.sequence_result_pass')

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
                  "terminal_sink_sequences": terminal_results,
                  "scanner_dispatch_guards": dict(guards)})
    payload = {"schema_version": 3, "result": "PASS", "validation_kind": "SYNTHETIC_OBSERVABILITY_ONLY",
               "run_id": run_id, "runner": runner_info, "scanner": scanner_info,
               "historical_case8_root_cause": "UNRESOLVED", "real_scanner_suite_executed": False,
               "real_scanner_calls": 0, "real_subprocess_dispatches": 0,
               "failure_injections": results, "terminal_sink_sequences": terminal_results,
               "case8_success_schema": state["success_schema"]}
    payload['semantic_checks']=semantic_checks
    prefix = output_directory / f"R6-A-R46-OBSERVABILITY-PREFLIGHT-{run_id}"
    j = prefix.with_suffix(".json")
    m = prefix.with_suffix(".md")
    c = output_directory / (prefix.name + "-COMPLETE.json")
    inc = output_directory / (prefix.name + "-INCOMPLETE.json")
    artifacts = publish(payload, j, m, c, inc, run_id)
    verify(j, m, c, inc)
    restored_payload = decode(json.loads(j.read_text(encoding="ascii"))["root"])
    require(restored_payload["result"] == "PASS" and len(restored_payload["failure_injections"]) == len(specifications) and
            len(restored_payload['terminal_sink_sequences'])==10 and
            restored_payload['case8_success_schema']['result']=='PASS',
            "complete aggregate publication")
    state["publication"] = artifacts
    return artifacts

