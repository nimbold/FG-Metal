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
                   success_schema, runner_info, scanner_info, guards, state,exercise,helper_source,
                   terminal_reporter,terminal_state_factory):
    """Run each specified injection once, stopping at the first failed check."""
    output_directory = Path(output_directory)
    evidence_directory = output_directory / f"R6-A-R46-OBSERVABILITY-PREFLIGHT-{run_id}-EVIDENCE"
    evidence_directory.mkdir()
    results = []
    state.update({"result": "RUNNING", "tests": results, "active_test": None,
                  "evidence_directory": str(evidence_directory), "scanner_suite_executed": False})

    def require(condition, message):
        if not condition:
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

    def decode_emission(raw,*,stderr):
        if stderr:
            marker=b'R46_STRUCTURED_FAILURE='
            require(raw.startswith(marker),'structured stderr prefix')
            raw=raw[len(marker):]
        envelope=json.loads(raw.decode('ascii'))
        require(envelope['evidence_encoding']=='r6a-r40-typed-json','R35 typed JSON envelope retained')
        return decode(envelope['root'])

    def record_primary(recorder,operation_id,description,exception,paths=None,phase='report publication'):
        try:
            raise exception
        except BaseException as exc:
            context=recorder.context(operation_id,phase,description,
                                     paths if paths is not None else
                                     {'json':b'/synthetic/reports/report.json','markdown':b'/synthetic/reports/report.md'})
            return recorder.record(exc,tb=exc.__traceback__,context=context)

    def assert_typed_secondary(record,expected_operation,name,*,capture_complete=True,
                               path_labels=('emergency_file',)):
        require(record is not None,name+' secondary failure record retained')
        require(record['operation_id']==expected_operation and not record['authoritative'],
                name+' secondary operation identity and ownership')
        expected_phase={'report.emergency.stderr':'terminal output',
                        'report.emergency.close':'cleanup',
                        'report.emergency.serialize':'report generation',
                        'report.emergency.secondary_serialize':'report generation'}.get(
                            expected_operation,'report publication')
        require(record['case_id']=='reporting' and record['phase']==expected_phase and record['description'],
                name+' complete case and phase identity')
        require(record['exception_type']=='builtins.OSError' and record['full_traceback'],
                name+' complete exception type and traceback')
        require(record['errno_present'] and record['errno_capture_status']=='ok' and type(record['errno']) is int,
                name+' typed errno')
        require(record['strerror_present'] and record['strerror_capture_status']=='ok' and type(record['strerror']) is str,
                name+' typed strerror')
        for field in ('filename','filename2'):
            require(record[field+'_present'] and record[field+'_capture_status']=='ok' and
                    record[field+'_type'] in {'builtins.bytes','builtins.str'} and
                    type(record[field]) in (bytes,str),name+' typed '+field)
        require(record['args_repr'] and record['timestamp']['status']=='ok',name+' args and timestamp')
        for path_label in path_labels:
            path=next((item for item in record['input_paths'] if item['label']==path_label),None)
            require(path is not None and path['capture_status']=='ok' and path['fspath_type'] in {'builtins.bytes','builtins.str'} and
                    path['filesystem_bytes'] is not None,name+' typed '+path_label+' path')
            require(record['path_types'][path_label]['capture_status']=='ok',name+' '+path_label+' path type summary')
        pin=record['root_pin_identity_if_live']
        require(pin.get('identity')==(123,1001,stat.S_IFDIR) and pin.get('fields',{}).get('inode')==1001,
                name+' available root-pin identity')
        helper=record['helper_identity_if_live']
        require(helper.get('pid')==6201 and helper.get('ready_identity',{}).get('inode')==1001,
                name+' available helper identity')
        require(record['scanner_child_identity_if_known'].get('status')=='not_dispatched',
                name+' explicit no-scanner identity')
        if capture_complete:
            require(record['relevant_object_identities_after_or_failure'].get('data_origin')=='SYNTHETIC_ONLY',
                    name+' synthetic failure identity snapshot')
        else:
            require(record['relevant_object_identities_after_or_failure'].get('status')=='snapshot_failed' and
                    record['relevant_object_identities_before'].get('data_origin')=='SYNTHETIC_ONLY',
                    name+' unavailable post-failure snapshot and preserved pre-failure evidence')
        require(record['evidence_complete'] is capture_complete,name+' evidence capture status')
        return record

    for spec in terminal_specs:
        name=spec['name']
        test_report={'schema_version':3,'result':'SYNTHETIC_TERMINAL_FAILURE','data_origin':'SYNTHETIC_ONLY',
                     'cases':[],'first_failure':None,'terminal_sequence':name}
        recorder=FailureRecorder(test_report,run_id+'-'+name.lower())
        recorder.set_case('reporting',suite_root=b'/synthetic/suite',
                           case_root=b'/synthetic/suite/case_8',
                           child_path=b'/synthetic/suite/case_8/child\nline.bin')
        observer_state={'remaining':1 if spec.get('observer_failure') else 0}
        recorder.last_snapshot=_synthetic_snapshot('before')
        def synthetic_terminal_snapshot(stage):
            if observer_state['remaining'] and stage=='failure':
                observer_state['remaining']-=1
                raise OSError(5,'R46 injected terminal evidence observer failure',
                              b'/synthetic/observer/path',None,b'/synthetic/observer/path2')
            return _synthetic_snapshot(stage),[]
        recorder.snapshot=synthetic_terminal_snapshot

        if spec.get('top_level'):
            primary=record_primary(recorder,'runner.entrypoint','unexpected top-level runner exception',
                                   RuntimeError('R46 synthetic unexpected top-level exception'))
            normal=record_primary(recorder,'report.finalize','ordinary report publication unavailable',
                                  OSError(28,'R46 synthetic ordinary report publication failure',
                                          b'/synthetic/reports/report.json',None,b'/synthetic/reports/report.md'))
            require(test_report['primary_failure'] is primary and not normal['authoritative'],
                    name+' top-level primary ownership')
        else:
            primary=record_primary(recorder,'report.finalize','ordinary report publication unavailable',
                                   OSError(28,'R46 synthetic ordinary report publication failure',
                                           b'/synthetic/reports/report.json',None,b'/synthetic/reports/report.md'))
        primary_id=primary['failure_id']
        if spec.get('observer_failure'):
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
        require(terminal_state['schema']=='r6a-r46-terminal-publication-v1',name+' terminal state schema')
        require(test_report['primary_failure'] is primary and primary['authoritative'],name+' original primary remains authoritative')
        require(not any(guards.values()),name+' no real scanner or subprocess dispatch')

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
        for raw in terminal_io.stderr_accepted:
            payload_raw=raw[len(b'R46_STRUCTURED_FAILURE='):]
            delivered.append({'sink':'stderr','raw_sha256':hashlib.sha256(payload_raw).hexdigest(),
                              'sink_capture_sha256':hashlib.sha256(raw).hexdigest(),
                              'payload':decode_emission(raw,stderr=True)})
        if terminal_state['terminal_sink_results']['emergency_file']['status']=='succeeded':
            delivered.append({'sink':'emergency_file','raw_sha256':hashlib.sha256(file_raw).hexdigest(),
                              'sink_capture_sha256':hashlib.sha256(file_raw).hexdigest(),
                              'payload':decode_emission(file_raw,stderr=False)})
        latest_generation=terminal_state['emission_generation']
        latest=[item for item in delivered
                if item['payload']['terminal_publication']['emission_generation']==latest_generation]

        history_by_generation={item['emission_generation']:item for item in terminal_state['generation_history']}
        for item in delivered:
            generation=item['payload']['terminal_publication']['emission_generation']
            history=history_by_generation[generation]
            require(item['raw_sha256']==history['payload_sha256'],name+' immutable generation hash')
            require(item['payload']['terminal_publication']['known_failure_ids']==history['known_failure_ids'],
                    name+' immutable generation failure chronology')

        if name in {'T1','T2','T3','T4','T5','T6','T7'}:
            expected_op=spec['expected_failure']
            sink_record=next((item for item in test_report['failure_records']
                              if item.get('operation_id')==expected_op),None)
            assert_typed_secondary(sink_record,expected_op,name)
            require(sink_record['failure_id']!=primary_id,name+' secondary did not take primary ownership')
            require(sink_record in test_report['secondary_failures'],name+' failure retained in secondary collection')
            require(terminal_state['latest_generation_delivered']==latest_generation and latest,
                    name+' latest structured generation reached an operational sink')
            newest=latest[-1]['payload']
            require(any(item['failure_id']==sink_record['failure_id']
                        for item in newest['failure_records']),name+' latest payload includes newest sink failure')
            require(terminal_state['evidence_status']=='EVIDENCE_COMPLETE' and terminal_state['evidence_complete'],
                    name+' terminal evidence status complete')
            if name=='T7':
                close_record=next((item for item in test_report['failure_records']
                                   if item.get('operation_id')=='report.emergency.close'),None)
                assert_typed_secondary(close_record,'report.emergency.close',name)
                require(close_record in test_report['secondary_failures'] and
                        close_record['failure_id'] in newest['terminal_publication']['known_failure_ids'],
                        name+' cleanup close failure is a distinct secondary in the latest generation')
                file_attempt=next(item for item in terminal_state['terminal_sink_attempts']
                                  if item['sink']=='emergency_file')
                require(file_attempt['failure_count']==2 and
                        [stage['operation_id'] for stage in file_attempt['stages'] if stage['status']=='failed']==
                        ['report.emergency.write','report.emergency.close'],
                        name+' preserves write failure and attempts one close')
            first_generation=next(item for item in terminal_state['generation_history'] if item['emission_generation']==1)
            require(first_generation['known_failure_ids']==[primary_id],name+' generation 1 chronology')
            if latest_generation>1:
                newest_generation=next(item for item in terminal_state['generation_history']
                                       if item['emission_generation']==latest_generation)
                require(sink_record['failure_id'] in newest_generation['known_failure_ids'],
                        name+' later generation contains its newly known failure')
        elif name=='T8':
            require(terminal_state['evidence_status']=='EVIDENCE_DEGRADED_ALL_SINKS_FAILED' and
                    terminal_state['evidence_complete'] is False,name+' both sinks failed status')
            require(terminal_state['terminal_sink_results']['stderr']['status']=='failed' and
                    terminal_state['terminal_sink_results']['emergency_file']['status']=='failed',
                    name+' both sinks marked failed')
            require(len(terminal_state['terminal_sink_attempts'])==2 and latest_generation==3,
                    name+' finite bounded terminal attempts and final in-memory generation')
            require(test_report['primary_failure'] is primary and primary['failure_id']==primary_id,
                    name+' primary failure ownership retained')
            require(len(terminal_state['generation_history'])==3 and
                    all(terminal_state['generation_history'][i]['emission_generation']==i+1 for i in range(3)),
                    name+' monotonic immutable generation chronology')
            require(terminal_state['latest_generation_delivered'] is None and not latest,
                    name+' failed sinks delivered no stale generation as latest')
            for operation_id in ('report.emergency.stderr','report.emergency.open'):
                failed=next((item for item in test_report['failure_records']
                             if item.get('operation_id')==operation_id),None)
                assert_typed_secondary(failed,operation_id,name)
                require(failed in test_report['secondary_failures'],name+' sink failure remains secondary')
            require(terminal_state['generation_history'][0]['known_failure_ids']==[primary_id],
                    name+' generation 1 excludes later sink failures')
            require(set(item['failure_id'] for item in test_report['failure_records']) <=
                    set(terminal_state['generation_history'][-1]['known_failure_ids']),
                    name+' final in-memory generation contains every known failure')
            require(test_report['primary_failure'] is primary and primary['authoritative'],
                    name+' in-memory primary remains authoritative after both sink failures')
            require(terminal_state['raw_traceback_attempted'] and len(terminal_io.raw_tracebacks)==1,
                    name+' single best-effort raw traceback after structured sinks exhaust')
        elif name=='T9':
            sink_record=next((item for item in test_report['failure_records']
                              if item.get('operation_id')=='report.emergency.open'),None)
            require(sink_record is not None and sink_record['evidence_capture_failures'],
                    name+' sink failure and nested observer failure retained')
            nested=sink_record['evidence_capture_failures'][0]
            require(nested['parent_failure_id']==sink_record['failure_id'] and nested['full_traceback'],
                    name+' observer failure traceback and ownership')
            require(nested['case_id']=='reporting' and nested['phase']=='evidence capture' and
                    nested['operation_id']==sink_record['operation_id']+'.evidence.failure.snapshot',
                    name+' nested observer operation identity')
            assert_typed_secondary(sink_record,'report.emergency.open',name,capture_complete=False)
            require(sink_record in test_report['secondary_failures'] and
                    sink_record['failure_id']!=primary_id,name+' sink failure remains secondary')
            require(nested['exception_type']=='builtins.OSError' and nested['errno_present'] and
                    nested['errno_capture_status']=='ok' and nested['filename_type']=='builtins.bytes' and
                    nested['filename2_type']=='builtins.bytes' and nested['filename'] is not None and
                    nested['filename2'] is not None,name+' nested observer typed exception fields')
            require(nested['evidence_complete'] is True,name+' nested observer record is complete while parent is not')
            require(terminal_state['latest_generation_delivered']==latest_generation and latest,
                    name+' incomplete but newest structured evidence delivered')
            require(terminal_state['evidence_status']=='EVIDENCE_DEGRADED_CAPTURE_INCOMPLETE' and
                    terminal_state['evidence_complete'] is False,name+' incomplete evidence status')
            require(terminal_state['generation_history'][-1].get('fallback_schema')==
                    'r6a-r46-terminal-failure-minimal-v1' and terminal_state['reporter_errors'],
                    name+' bounded typed fallback follows repeated serialization failures')
            fallback_raw=terminal_io.stderr_accepted[-1][len(b'R46_STRUCTURED_FAILURE='):]
            fallback_envelope=json.loads(fallback_raw.decode('ascii'))
            require(fallback_envelope.get('fallback_schema')=='r6a-r46-terminal-failure-minimal-v1',
                    name+' R35 typed fallback envelope delivered to working stderr')
            require(latest[0]['payload']['fallback_projection_complete'] is True,
                    name+' fallback retained safe prior identities without omissions')
            serializer_records=[]
            for operation_id in ('report.emergency.serialize','report.emergency.secondary_serialize'):
                serializer_record=next((item for item in test_report['failure_records']
                                        if item.get('operation_id')==operation_id),None)
                assert_typed_secondary(serializer_record,operation_id,name)
                require(serializer_record in test_report['secondary_failures'],
                        name+' serialization failure remains secondary')
                serializer_records.append(serializer_record)
            latest_parent=next((record for record in latest[0]['payload']['failure_records']
                                if record.get('failure_id')==sink_record['failure_id']),None)
            require(latest_parent is not None and any(
                        child.get('failure_id')==nested['failure_id'] and
                        child.get('parent_failure_id')==sink_record['failure_id']
                        for child in latest_parent.get('evidence_capture_failures',[])),
                    name+' newest payload includes nested capture failure with parent identity')
            require(all(any(item.get('failure_id')==failure['failure_id']
                            for item in latest[0]['payload']['failure_records'])
                        for failure in serializer_records),
                    name+' fallback includes both serialization failures')
            require(latest_parent['root_pin_identity_if_live']['identity']==(123,1001,stat.S_IFDIR) and
                    latest_parent['helper_identity_if_live']['pid']==6201 and
                    latest_parent['scanner_child_identity_if_known']['status']=='not_dispatched' and
                    latest_parent['relevant_object_identities_before']['data_origin']=='SYNTHETIC_ONLY' and
                    latest_parent['relevant_object_identities_after_or_failure']['status']=='snapshot_failed',
                    name+' fallback preserves before and available identities across observer failure')
            require(test_report['primary_failure'] is primary and primary['authoritative'],
                    name+' primary ownership retained with incomplete observer evidence')
            require(terminal_state['raw_traceback_attempted'] and len(terminal_io.raw_tracebacks)==1,
                    name+' raw traceback remains best-effort after structured fallback delivery')
        else:
            require(name=='T10' and primary['operation_id']=='runner.entrypoint',name+' top-level exception boundary')
            ordinary=next((item for item in test_report['failure_records']
                           if item.get('operation_id')=='report.finalize'),None)
            require(ordinary is not None and ordinary['failure_id'] in
                    next(item for item in terminal_state['generation_history']
                         if item['emission_generation']==1)['known_failure_ids'],
                    name+' ordinary publication failure present in first generation')
            require(ordinary in test_report['secondary_failures'] and not ordinary['authoritative'],
                    name+' ordinary report failure remains secondary')
            assert_typed_secondary(ordinary,'report.finalize',name,path_labels=('json','markdown'))
            require(later_boundary is not None and later_boundary in test_report['secondary_failures'] and
                    later_boundary['operation_id']=='runner.result.print' and not later_boundary['authoritative'],
                    name+' later summary failure recorded as secondary')
            assert_typed_secondary(later_boundary,'runner.result.print',name,path_labels=('stdout',))
            require(terminal_state['emission_generation']==2 and
                    terminal_state['latest_generation_delivered']==latest_generation and latest_generation==2 and latest,
                    name+' later reporter invocation routes newest generation to stderr')
            require(terminal_state['terminal_sink_results']['emergency_file']['latest_generation_received']==1 and
                    len([item for item in terminal_io.file_events if item['operation']=='open'])==1,
                    name+' successful emergency file remains exclusive and one-shot')
            first_generation=next(item for item in terminal_state['generation_history']
                                  if item['emission_generation']==1)
            second_generation=next(item for item in terminal_state['generation_history']
                                   if item['emission_generation']==2)
            require(later_boundary['failure_id'] not in first_generation['known_failure_ids'] and
                    later_boundary['failure_id'] in second_generation['known_failure_ids'],
                    name+' second invocation preserves generation chronology')
            newest=latest[0]['payload']
            require(any(item['failure_id']==primary_id and item['operation_id']=='runner.entrypoint'
                        for item in newest['failure_records']) and
                    any(item['failure_id']==ordinary['failure_id'] for item in newest['failure_records']) and
                    any(item['failure_id']==later_boundary['failure_id'] for item in newest['failure_records']),
                    name+' latest generation includes top-level, report, and summary failures')
            require(terminal_state['evidence_status']=='EVIDENCE_COMPLETE' and terminal_state['evidence_complete'],
                    name+' top-level evidence reached latest terminal sink')

        terminal_result={'name':name,'result':'PASS','faults':spec['faults'],
                         'expected_failure_operation_id':spec.get('expected_failure'),
                         'primary_failure_id':primary_id,'terminal_publication':terminal_state,
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
        test_report['terminal_sequence_evidence']=terminal_result
        artifacts,reconstructed=publish_test('terminal-'+name.lower(),test_report,recorder.run_id)
        require(reconstructed['terminal_sequence_evidence']['terminal_publication']['evidence_status']==
                terminal_state['evidence_status'],name+' terminal status round trip')
        terminal_result['publication']=artifacts
        terminal_results.append(terminal_result)

    require(len(terminal_results)==10 and all(item['result']=='PASS' for item in terminal_results),
            'all ten R46 terminal sink sequences completed')

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

