"""Read-only failure evidence for the disposable R45 scanner fixture.

This module never dispatches a scanner, opens a diagnostic target descriptor,
or publishes a report.  The runner retains its R35 typed report publisher.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import os
import select
import stat
import sys
import time
import traceback


class ObservationCaptureError(RuntimeError):
    pass


def runtime_type(value):
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def stat_fields(info):
    return {
        "device": info.st_dev, "inode": info.st_ino, "mode": info.st_mode,
        "file_type": stat.S_IFMT(info.st_mode), "size": info.st_size,
        "nlink": info.st_nlink, "uid": info.st_uid, "gid": info.st_gid,
        "is_directory": stat.S_ISDIR(info.st_mode),
        "is_regular": stat.S_ISREG(info.st_mode),
    }


def _timestamp():
    return {"status": "ok", "utc": datetime.now(timezone.utc).isoformat(),
            "unix": time.time(), "monotonic_ns": time.monotonic_ns()}


def _guard(errors, field, call, default=None):
    try:
        return call()
    except BaseException as exc:
        # Keep the original object until its independently guarded fields have
        # been captured.  Do not recurse into identity sampling here.
        errors.append({"field": field, "exception": exc,
                       "traceback_object": exc.__traceback__})
        return default


def _supported(value, errors, field):
    """An explicit adapter, never a lossy serializer fallback."""
    if value is None or type(value) in (str, bytes, bool, int, float):
        return value
    if type(value) in (tuple, list):
        items = [_supported(item, errors, f"{field}[{i}]")
                 for i, item in enumerate(value)]
        return tuple(items) if type(value) is tuple else items
    if type(value) is dict:
        return {_supported(key, errors, f"{field}.key"): _supported(item, errors, f"{field}.value")
                for key, item in value.items()}
    if isinstance(value, os.PathLike):
        return {"adapter": "os.PathLike", "runtime_type": runtime_type(value),
                "fspath": _supported(os.fspath(value), errors, field + ".fspath")}
    try:
        raise TypeError(f"unsupported authoritative evidence at {field}: {runtime_type(value)}")
    except TypeError as exc:
        errors.append({"field": field, "exception": exc,
                       "traceback_object": exc.__traceback__})
    return {"status": "unsupported", "runtime_type": runtime_type(value),
            "diagnostic_repr": _guard(errors, field + ".repr", lambda: repr(value))}


def exception_fields(exc, tb=None):
    """Capture each field separately so a broken observer cannot mask exc."""
    errors = []
    result = {"exception_type": _guard(errors, "exception_type", lambda: runtime_type(exc), "<unknown>"),
              "exception_type_short": _guard(errors, "exception_type_short", lambda: type(exc).__name__, "<unknown>")}
    for field in ("errno", "strerror", "filename", "filename2"):
        marker = object()
        prior_errors = len(errors)
        value = _guard(errors, field, lambda name=field: getattr(exc, name, marker), marker)
        capture_failed = len(errors) != prior_errors
        present = value is not marker and not capture_failed
        result[field + "_present"] = present
        result[field + "_capture_status"] = "error" if capture_failed else ("ok" if present else "absent")
        result[field] = _guard(errors, field + ".adapt", lambda v=value: _supported(v, errors, field), None) if present else None
        result[field + "_type"] = _guard(errors, field + ".type", lambda v=value: runtime_type(v)) if present else None
    args = _guard(errors, "args", lambda: exc.args, ())
    result["args"] = _guard(errors, "args.adapt", lambda: _supported(args, errors, "args"), ())
    result["args_repr"] = _guard(errors, "args_repr", lambda: repr(args))
    result["args_repr_capture_status"] = "ok" if result["args_repr"] is not None else "error"
    result["exception_message"] = _guard(errors, "exception_message", lambda: str(exc))
    original_tb = tb if tb is not None else _guard(errors, "traceback_object", lambda: exc.__traceback__)
    lines = _guard(errors, "full_traceback", lambda: traceback.format_exception(type(exc), exc, original_tb), [])
    result["traceback_lines"] = lines
    result["full_traceback"] = _guard(errors, "traceback_join", lambda: "".join(lines), "")
    result["full_traceback_capture_status"] = "ok" if result["full_traceback"] else "error"
    result["traceback"] = result["full_traceback"]
    # Wrapper errors can carry a filename-bearing cause.  Keep its fields,
    # separately from the complete chained traceback, without recursive chains.
    chain = []
    seen = {id(exc)}
    cursor = exc
    for _ in range(16):
        cause = _guard(errors, "exception_chain", lambda: cursor.__cause__ or cursor.__context__)
        if cause is None or id(cause) in seen:
            break
        seen.add(id(cause))
        cause_errors = []
        item = {"exception_type": _guard(cause_errors, "type", lambda: runtime_type(cause), "<unknown>")}
        for field in ("errno", "strerror", "filename", "filename2", "args"):
            item[field] = _guard(cause_errors, field, lambda name=field: _supported(getattr(cause, name, None), cause_errors, name))
            item[field + "_type"] = _guard(cause_errors, field + ".type", lambda name=field: runtime_type(getattr(cause, name, None)))
        item["full_traceback"] = _guard(cause_errors, "traceback", lambda: "".join(traceback.format_exception(type(cause), cause, cause.__traceback__)), "")
        item["capture_error_count"] = len(cause_errors)
        errors.extend(cause_errors)
        chain.append(item)
        cursor = cause
    result["exception_chain"] = chain
    return result, errors


class FailureRecorder:
    """One context/resource registry for local and unexpected failures."""

    def __init__(self, report, run_id="bootstrap"):
        self.report = report
        self.run_id = run_id
        self.current = {"case_id": "bootstrap", "phase": "bootstrap",
                        "operation_id": "runner.bootstrap", "description": "runner initialization",
                        "input_paths": {}, "stack": []}
        self.resources = {"suite_parent": None, "suite_root": None, "case_root": None, "child_path": None,
                          "root_fd": None, "root_fd_live": False,
                          "child_fd": None, "child_fd_live": False,
                          "helper": None, "helper_identity": None,
                          "helper_stderr_raw": b"", "scanner_capture": None}
        self.last_snapshot = None
        self.last_failure_context = None
        self.original_exceptions = []
        self.expected_depth = 0
        self.sequence = 0
        self.snapshot_hook = None  # synthetic observer-failure injection only
        for key, default in (("operations", []), ("failure_records", []),
                             ("secondary_failures", []), ("expected_exception_records", []),
                             ("primary_failure", None), ("identity_samples", [])):
            report.setdefault(key, default)

    def set_case(self, case_id, *, suite_root=None, case_root=None, child_path=None):
        self.current = {"case_id": str(case_id), "phase": "case entry",
                        "operation_id": f"case{case_id}.entry", "description": "enter disposable case",
                        "input_paths": {}, "stack": []}
        self.resources.update({"case_root": case_root, "child_path": child_path,
                               "root_fd": None, "root_fd_live": False,
                               "child_fd": None, "child_fd_live": False,
                               "helper": None, "helper_identity": None,
                               "helper_stderr_raw": b"", "scanner_capture": None})
        if suite_root is not None:
            self.resources["suite_root"] = suite_root
        self.last_snapshot = None

    def register(self, **resources):
        self.resources.update(resources)

    def operation_id(self, suffix):
        if suffix.startswith(("runner.", "suite.", "report.", "case")):
            return suffix
        return f"case{self.current['case_id']}.{suffix}"

    def context(self, operation_id, phase, description, paths=None):
        inherited = dict(self.current.get("input_paths", {}))
        inherited.update(paths or {})
        return {"case_id": self.current["case_id"], "phase": phase,
                "operation_id": self.operation_id(operation_id), "description": description,
                "input_paths": inherited,
                "stack": self.current.get("stack", []) + [self.current["operation_id"]]}

    def _path_argument(self, name, path, errors):
        item = {"label": name, "runtime_type": _guard(errors, name + ".type", lambda: runtime_type(path)),
                "value": _guard(errors, name + ".value", lambda: _supported(path, errors, name)),
                "repr": _guard(errors, name + ".repr", lambda: repr(path))}
        fspath = _guard(errors, name + ".fspath", lambda: os.fspath(path))
        item["fspath"] = fspath
        item["fspath_type"] = _guard(errors, name + ".fspath_type", lambda: runtime_type(fspath))
        item["filesystem_bytes"] = _guard(errors, name + ".fsencode", lambda: os.fsencode(fspath)) if fspath is not None else None
        item["parent_path"] = _guard(errors, name + ".parent", lambda: os.path.dirname(fspath)) if fspath is not None else None
        item["capture_status"] = "ok" if fspath is not None else "error"
        return item

    def _capture_error(self, error, parent_id, context):
        exc = error["exception"]
        fields, nested = exception_fields(exc, error.get("traceback_object"))
        return {"schema": "r6a-r45-failure-record-v1", "failure_id": parent_id + ".capture." + error["field"],
                "parent_failure_id": parent_id, "run_id": self.run_id,
                "operation_id": context["operation_id"] + ".evidence." + error["field"],
                "case_id": context["case_id"], "phase": "evidence capture",
                "description": "secondary evidence capture failure: " + error["field"],
                "timestamp": _guard([], "timestamp", _timestamp, {"status": "error"}),
                **fields, "cwd": {"status": "not_sampled_in_bounded_secondary_capture"},
                "input_paths": [], "path_types": {},
                "relevant_object_identities_before": {"status": "see_parent_failure"},
                "relevant_object_identities_after_or_failure": {"status": "see_parent_failure"},
                "root_pin_identity_if_live": {"status": "see_parent_failure"},
                "helper_identity_if_live": {"status": "see_parent_failure"},
                "scanner_child_identity_if_known": {"status": "see_parent_failure"},
                "execution_context": {key: value for key, value in context.items() if key != "input_paths"},
                "evidence_capture_failures": [], "bounded_secondary_capture_errors": [item["field"] for item in nested],
                "evidence_complete": not nested, "authoritative": False}

    def _observe_path(self, path, label, errors):
        if path is None:
            return {"status": "not_yet_known", "label": label}
        result = {"status": "observed", "label": label, "path_type": runtime_type(path),
                  "path_repr": _guard(errors, label + ".repr", lambda: repr(path)), "exists": None,
                  "lstat": None, "stat": None, "canonical_path": None, "canonical_stat": None}
        fs_path = _guard(errors, label + ".fspath", lambda: os.fspath(path))
        result["path_fspath"] = {"type": runtime_type(fs_path), "value": fs_path,
                                  "repr": _guard(errors, label + ".fspath_repr", lambda: repr(fs_path))}
        if fs_path is None:
            result["status"] = "path_conversion_error"
            return result
        result["path_bytes"] = _guard(errors, label + ".fsencode", lambda: os.fsencode(fs_path))

        def sample(field, call):
            try:
                info = call()
                result[field] = {"status": "ok", "fields": stat_fields(info),
                                 "identity": (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)),
                                 "timestamp": _timestamp()}
                if field == "lstat":
                    result["exists"] = True
            except FileNotFoundError as exc:
                fields, field_errors = exception_fields(exc)
                errors.extend(field_errors)
                result[field] = {"status": "missing", "exception": fields}
                if field == "lstat":
                    result["exists"] = False
            except BaseException as exc:
                errors.append({"field": label + "." + field, "exception": exc,
                               "traceback_object": exc.__traceback__})
                fields, field_errors = exception_fields(exc)
                errors.extend(field_errors)
                result[field] = {"status": "error", "exception": fields}

        sample("lstat", lambda: os.lstat(fs_path))
        sample("stat", lambda: os.stat(fs_path, follow_symlinks=True))
        try:
            canonical = os.path.realpath(fs_path, strict=True)
            result["canonical_path"] = canonical
            result["canonical_path_bytes"] = os.fsencode(canonical)
            result["canonicalization"] = {"status": "ok", "timestamp": _timestamp()}
            sample("canonical_stat", lambda: os.stat(canonical, follow_symlinks=False))
        except FileNotFoundError as exc:
            fields, field_errors = exception_fields(exc)
            errors.extend(field_errors)
            result["canonicalization"] = {"status": "missing", "exception": fields}
        except BaseException as exc:
            errors.append({"field": label + ".canonicalization", "exception": exc,
                           "traceback_object": exc.__traceback__})
            fields, field_errors = exception_fields(exc)
            errors.extend(field_errors)
            result["canonicalization"] = {"status": "error", "exception": fields}
        return result

    def _observe_fd(self, fd, live, label, errors):
        if fd is None:
            return {"status": "not_open", "label": label}
        if not live:
            return {"status": "closed", "label": label, "fd": fd}
        try:
            info = os.fstat(fd)
            return {"status": "ok", "label": label, "fd": fd, "fields": stat_fields(info),
                    "identity": (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)),
                    "timestamp": _timestamp()}
        except BaseException as exc:
            errors.append({"field": label + ".fstat", "exception": exc,
                           "traceback_object": exc.__traceback__})
            fields, field_errors = exception_fields(exc)
            errors.extend(field_errors)
            return {"status": "error", "label": label, "fd": fd, "exception": fields}

    def _helper_snapshot(self, errors):
        p = self.resources["helper"]
        if p is None:
            return {"status": "not_initialized"}
        result = {"status": "known", "pid": _guard(errors, "helper.pid", lambda: p.pid),
                  "ready_identity": self.resources["helper_identity"]}
        result["returncode"] = _guard(errors, "helper.poll", p.poll)
        result["live"] = result["returncode"] is None
        # Read only the already-owned stderr pipe and only when ready.  This
        # never changes the READY/POST stdin/stdout protocol or helper lifetime.
        pipe = _guard(errors, "helper.stderr", lambda: p.stderr)
        if pipe is not None and not pipe.closed:
            ready = _guard(errors, "helper.stderr.ready", lambda: select.select([pipe], [], [], 0)[0], [])
            if ready:
                raw = _guard(errors, "helper.stderr.read", lambda: os.read(pipe.fileno(), 1024 * 1024), b"")
                self.resources["helper_stderr_raw"] += raw
        result["stderr_raw"] = self.resources["helper_stderr_raw"]
        return result

    def snapshot(self, stage="observation"):
        errors = []
        if self.snapshot_hook is not None:
            _guard(errors, stage + ".injected_observer", lambda: self.snapshot_hook(stage))
        resources = self.resources
        snapshot = {"timestamp": _guard(errors, "snapshot.timestamp", _timestamp),
                    "suite_parent": self._observe_path(resources["suite_parent"], "suite_parent", errors),
                    "suite_root": self._observe_path(resources["suite_root"], "suite_root", errors),
                    "case_root": self._observe_path(resources["case_root"], "case_root", errors),
                    "child": self._observe_path(resources["child_path"], "child", errors),
                    "root_pin": self._observe_fd(resources["root_fd"], resources["root_fd_live"], "root_pin", errors),
                    "child_fd": self._observe_fd(resources["child_fd"], resources["child_fd_live"], "child_fd", errors),
                    "helper": self._helper_snapshot(errors)}
        snapshot["child_parent"] = snapshot["case_root"]
        capture = resources["scanner_capture"]
        snapshot["scanner_child"] = ({"status": "known", "pid": capture.get("child_pid"),
                                      "ppid": capture.get("child_ppid"), "identity": capture.get("child_identity"),
                                      "identity_error": capture.get("child_identity_error")}
                                     if isinstance(capture, dict) else {"status": "not_dispatched_or_not_returned"})
        snapshot["observer_errors"] = []
        return snapshot, errors

    def _best_snapshot(self, stage):
        try:
            return self.snapshot(stage)
        except BaseException as exc:
            return {"status": "snapshot_failed"}, [{"field": stage + ".snapshot",
                    "exception": exc, "traceback_object": exc.__traceback__}]

    def _plain_context(self, context, errors):
        paths = {name: path for name, path in context.get("input_paths", {}).items() if path is not None}
        for name in ("suite_parent", "suite_root", "case_root", "child_path"):
            if self.resources[name] is not None:
                paths.setdefault(name, self.resources[name])
        inputs = [self._path_argument(name, path, errors) for name, path in paths.items()]
        return ({key: value for key, value in context.items() if key != "input_paths"}, inputs,
                {item["label"]: {"runtime_type": item["runtime_type"], "fspath_type": item["fspath_type"],
                                  "capture_status": item["capture_status"]} for item in inputs})

    def record(self, exc, *, tb=None, context=None, before=None, expected=False, handled=False,
               after=None, capture_errors=None):
        """Never propagate an observer error in place of the supplied exc."""
        context = dict(context or self.current)
        try:
            return self._record_core(exc,tb=tb,context=context,before=before,expected=expected,
                                     handled=handled,after=after,capture_errors=capture_errors)
        except BaseException as evidence_exc:
            # The normal path already establishes ownership before observations.
            # Fill a bounded, serializable record if an unanticipated bug in that
            # path interrupts capture, and attach the secondary exception.
            record = next((item for original,item in self.original_exceptions if original is exc),None)
            if record is None:
                self.sequence += 1
                is_expected=bool(expected or handled or self.expected_depth)
                record = {"schema":"r6a-r45-failure-record-v1","failure_id":f"failure-{self.sequence}",
                          "run_id":self.run_id,"operation_id":context["operation_id"],
                          "case_id":context["case_id"],"phase":context["phase"],"description":context["description"],
                          "authoritative":not is_expected and self.report["primary_failure"] is None,
                          "expected_exception":is_expected,"handled_by_existing_fallback":bool(handled),"evidence_capture_failures":[]}
                self.original_exceptions.append((exc,record))
                self.report["failure_records"].append(record)
                if record["authoritative"]:self.report["primary_failure"]=record
                elif is_expected:self.report["expected_exception_records"].append(record)
                else:self.report["secondary_failures"].append(record)
            fields,errors=exception_fields(exc,tb)
            record.update(fields)
            record.setdefault("timestamp",_guard([],"timestamp",_timestamp,{"status":"error"}))
            record.setdefault("cwd",{"status":"not_recovered_after_capture_failure"})
            record.setdefault("input_paths",[]);record.setdefault("path_types",{})
            record.setdefault("execution_context",{key:value for key,value in context.items() if key!="input_paths"})
            record.setdefault("relevant_object_identities_before",before or self.last_snapshot or {"status":"not_yet_sampled"})
            record.setdefault("relevant_object_identities_after_or_failure",after or {"status":"capture_failed"})
            for name in ("root_pin_identity_if_live","helper_identity_if_live","scanner_child_identity_if_known"):
                record.setdefault(name,{"status":"capture_failed"})
            errors.append({"field":"failure_record_assembly","exception":evidence_exc,"traceback_object":evidence_exc.__traceback__})
            record.setdefault("evidence_capture_failures",[]).extend(self._capture_error(error,record["failure_id"],context) for error in errors)
            record["evidence_complete"]=False
            if self.report.get("first_failure") is None and not (expected or handled or self.expected_depth):
                self.report["first_failure"]={"case":context["case_id"],"operation_id":context["operation_id"],
                                              "expected":context["description"],"observed":record}
            return record

    def _record_core(self, exc, *, tb=None, context=None, before=None, expected=False, handled=False,
                     after=None, capture_errors=None):
        context = dict(context or self.current)
        for original, record in self.original_exceptions:
            if original is exc:
                record.setdefault("boundary_contexts", []).append({key: value for key, value in context.items() if key != "input_paths"})
                return record
        self.sequence += 1
        failure_id = f"failure-{self.sequence}"
        is_expected = expected or handled or self.expected_depth > 0
        authoritative = not is_expected and self.report["primary_failure"] is None
        record = {"schema": "r6a-r45-failure-record-v1", "failure_id": failure_id,
                  "run_id": self.run_id, "operation_id": context["operation_id"],
                  "case_id": context["case_id"], "phase": context["phase"],
                  "description": context["description"], "authoritative": authoritative,
                  "expected_exception": bool(is_expected), "handled_by_existing_fallback": bool(handled),
                  "evidence_capture_failures": []}
        # Establish ownership of the original exception before formatting,
        # cwd/path conversion, timestamps, or any failure-time observation.
        self.original_exceptions.append((exc, record))
        self.report["failure_records"].append(record)
        if authoritative:
            self.report["primary_failure"] = record
            self.last_failure_context = context
            if self.report.get("first_failure") is None:
                self.report["first_failure"] = {"case": context["case_id"], "operation_id": context["operation_id"],
                                                "failure_id": failure_id, "expected": context["description"],
                                                "observed": record}
        elif is_expected:
            self.report["expected_exception_records"].append(record)
        else:
            self.report["secondary_failures"].append(record)
        fields, errors = exception_fields(exc, tb)
        record.update(fields)
        record["timestamp"] = _guard(errors, "timestamp", _timestamp, {"status": "error"})
        record["cwd"] = {"path": _guard(errors, "cwd.getcwd", os.getcwd),
                         "bytes": _guard(errors, "cwd.getcwdb", os.getcwdb)}
        record["cwd"]["path_type"] = runtime_type(record["cwd"]["path"])
        record["cwd"]["bytes_type"] = runtime_type(record["cwd"]["bytes"])
        record["cwd"]["status"] = "ok" if record["cwd"]["bytes"] is not None else "error"
        plain_context, inputs, types = self._plain_context(context, errors)
        record["input_paths"] = inputs
        record["path_types"] = types
        record["execution_context"] = plain_context
        if after is None:
            after, after_errors = self._best_snapshot("failure")
            errors.extend(after_errors)
        errors.extend(capture_errors or [])
        record["relevant_object_identities_before"] = before if before is not None else (self.last_snapshot or {"status": "not_yet_sampled"})
        record["relevant_object_identities_after_or_failure"] = after
        record["root_pin_identity_if_live"] = after.get("root_pin", {"status": "unknown"})
        record["helper_identity_if_live"] = after.get("helper", {"status": "unknown"})
        record["scanner_child_identity_if_known"] = after.get("scanner_child", {"status": "unknown"})
        record["evidence_capture_failures"] = [self._capture_error(error, failure_id, context) for error in errors]
        record["evidence_complete"] = not errors and bool(record["full_traceback"])
        return record

    @contextmanager
    def scope(self, operation_id, phase, description, *, paths=None, expected=(), handled=False,
              observe=True, retain_event=True):
        previous = self.current
        context = self.context(operation_id, phase, description, paths)
        self.current = context
        event = {"operation_id": context["operation_id"], "case_id": context["case_id"],
                 "phase": phase, "description": description, "status": "active"}
        if retain_event:
            self.report["operations"].append(event)
        before, errors = self._best_snapshot("before") if observe else (self.last_snapshot or {"status": "not_yet_sampled"}, [])
        event["before"] = before
        try:
            if errors:
                raise ObservationCaptureError("required pre-operation evidence capture failed")
            yield event
        except BaseException as exc:
            # record() first owns and formats the original exception. Its
            # failure-time snapshot happens only after that ownership is fixed.
            record = self.record(exc,tb=exc.__traceback__,context=context,before=before,
                                 expected=isinstance(exc, expected), handled=handled,
                                 capture_errors=errors)
            after=record['relevant_object_identities_after_or_failure']
            event.update({"status": "expected_exception" if record["expected_exception"] else "error",
                          "failure_id": record["failure_id"], "after": after})
            raise
        else:
            after, after_errors = self._best_snapshot("after") if observe else ({"status": "not_applicable"}, [])
            event["after"] = after
            if after_errors:
                try:
                    raise ObservationCaptureError("required post-operation evidence capture failed")
                except ObservationCaptureError as exc:
                    record = self.record(exc, context=context, before=before, after=after,
                                         capture_errors=after_errors)
                    event.update({"status": "observability_error", "failure_id": record["failure_id"]})
                    raise
            event["status"] = "ok"
            if observe:
                self.last_snapshot = after
        finally:
            self.current = previous

    def perform(self, operation_id, phase, description, call, *, paths=None,
                on_success=None, expected=(), handled=False, observe=True, call_with_snapshot=False,
                retain_event=True):
        with self.scope(operation_id, phase, description, paths=paths,
                        expected=expected, handled=handled, observe=observe,retain_event=retain_event) as event:
            value = call(event["before"]) if call_with_snapshot else call()
            if on_success is not None:
                on_success(value)
            event["result_type"] = runtime_type(value)
            return value

    @contextmanager
    def expected_failures(self):
        self.expected_depth += 1
        try:
            yield
        finally:
            self.expected_depth -= 1

    def unexpected(self, exc, *, operation_id=None, phase=None, description=None):
        context = dict(self.current)
        if operation_id is not None:
            context["operation_id"] = operation_id
        if phase is not None:
            context["phase"] = phase
        if description is not None:
            context["description"] = description
        return self.record(exc, context=context)

    def emergency_payload(self):
        return {"schema": "r6a-r45-emergency-failure-v1", "run_id": self.run_id,
                "publication_complete": False, "primary_failure": self.report.get("primary_failure"),
                "secondary_failures": self.report.get("secondary_failures", []),
                "failure_records": self.report.get("failure_records", []),
                "current_execution_context": {key: value for key, value in self.current.items() if key != "input_paths"}}

