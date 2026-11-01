#!/usr/bin/env python3
"""Fail-fast R46 terminal-evidence repair for the disposable-root scanner regressions."""
import base64, copy, hashlib, importlib.util, json, math, os, select, stat, subprocess, sys, tempfile, time, traceback, uuid
from pathlib import Path
from datetime import datetime, timezone

try:
    from r6a_r45_failure_observability import FailureRecorder, ObservationCaptureError, exception_fields, runtime_type, stat_fields
except BaseException as bootstrap_primary:
    # The helper itself is a runtime dependency. Its import failure still gets
    # the common field names and lossless R35 representation before re-raising.
    bootstrap_tb=bootstrap_primary.__traceback__
    try:
        def bootstrap_typed(value):
            if value is None:return {'tag':'null'}
            if type(value) is bool:return {'tag':'bool','value':value}
            if type(value) is str:return {'tag':'str','value':value}
            if type(value) is int:return {'tag':'int','value':str(value)}
            if type(value) is float:return {'tag':'float','hex':value.hex()}
            if type(value) is bytes:return {'tag':'bytes','encoding':'base64','data':base64.b64encode(value).decode('ascii'),'length':len(value),'sha256':hashlib.sha256(value).hexdigest()}
            if type(value) in (list,tuple):return {'tag':'list' if type(value) is list else 'tuple','items':[bootstrap_typed(item) for item in value]}
            if type(value) is dict:
                pairs=[(bootstrap_typed(key),bootstrap_typed(item)) for key,item in value.items()]
                pairs.sort(key=lambda pair:json.dumps(pair[0],sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii'))
                return {'tag':'dict','entries':[[key,item] for key,item in pairs]}
            raise TypeError('unsupported bootstrap evidence value')
        bootstrap_fields={'schema':'r6a-r45-failure-record-v1','failure_id':'bootstrap-helper-import','run_id':'bootstrap',
                          'operation_id':'runner.bootstrap.diagnostic_helper_import','case_id':'bootstrap','phase':'bootstrap',
                          'description':'import shared failure evidence helper','timestamp':{'utc':datetime.now(timezone.utc).isoformat(),'unix':time.time(),'monotonic_ns':time.monotonic_ns()},
                          'exception_type':type(bootstrap_primary).__module__+'.'+type(bootstrap_primary).__qualname__,
                          'args_repr':repr(bootstrap_primary.args),'args':bootstrap_primary.args,
                          'full_traceback':''.join(traceback.format_exception(type(bootstrap_primary),bootstrap_primary,bootstrap_tb)),
                          'cwd':{'path':os.getcwd(),'bytes':os.getcwdb(),'status':'ok'},
                          'input_paths':[{'label':'helper','value':os.path.join(os.path.dirname(__file__),'r6a_r45_failure_observability.py'),'runtime_type':'builtins.str'}],
                          'path_types':{'helper':'builtins.str'},'execution_context':{'case_id':'bootstrap','phase':'bootstrap','operation_id':'runner.bootstrap.diagnostic_helper_import'},
                          'relevant_object_identities_before':{'status':'not_yet_acquired'},
                          'relevant_object_identities_after_or_failure':{'status':'not_yet_acquired'},
                          'root_pin_identity_if_live':{'status':'not_open'},'helper_identity_if_live':{'status':'not_initialized'},
                          'scanner_child_identity_if_known':{'status':'not_dispatched'},'evidence_capture_failures':[],
                          'evidence_complete':True,'authoritative':True}
        for field in ('errno','strerror','filename','filename2'):
            value=getattr(bootstrap_primary,field,None)
            bootstrap_fields[field]=value
            bootstrap_fields[field+'_type']=type(value).__module__+'.'+type(value).__qualname__
            bootstrap_fields[field+'_present']=hasattr(bootstrap_primary,field)
        bootstrap_text='R46_STRUCTURED_BOOTSTRAP_FAILURE='+json.dumps({'evidence_encoding':'r6a-r40-typed-json','version':1,'root':bootstrap_typed(bootstrap_fields)},sort_keys=True,ensure_ascii=True)+'\n'
        if sys.stderr.write(bootstrap_text)!=len(bootstrap_text):raise OSError('short structured bootstrap stderr write')
        sys.stderr.flush()
    except BaseException as bootstrap_evidence_failure:
        traceback.print_exception(type(bootstrap_primary),bootstrap_primary,bootstrap_tb,file=sys.stderr)
        traceback.print_exception(type(bootstrap_evidence_failure),bootstrap_evidence_failure,bootstrap_evidence_failure.__traceback__,file=sys.stderr)
    raise

# Runtime bootstrap is protected by the same recorder as the disposable cases.
HERE=None
SCANNER_PATH=None
RUN_ID='bootstrap'
REPORT_PREFIX=None
JSON_OUT=MD_OUT=COMPLETE_OUT=INCOMPLETE_OUT=None
TERMINAL_STATE=None
EVIDENCE_ENCODING='r6a-r40-typed-json'
scanner=None
report={'schema_version':3,'scanner':{},'runner':{},'started_at_unix':None,
        'cases':[],'cleanup':[],'first_failure':None,'case8_setup_operations':[],
        'case8_observer_errors':[],'case8_pre_scan_contract':None,'case8_post_scan_contract':None,
        'case8_root_identity_anchor':None,'historical_case8_root_cause':'UNRESOLVED',
        'current_fixture_semantics':'SEMANTICALLY_EQUIVALENT','authorized_change_boundary':'OBSERVABILITY_ONLY'}
OBS=FailureRecorder(report)
_SYNTHETIC_OPERATION_OVERRIDE=None

def _new_terminal_state():
    return {'schema':'r6a-r46-terminal-publication-v1','emission_generation':0,
            'generation_history':[],'terminal_sink_attempts':[],
            'terminal_sink_results':{
                'stderr':{'status':'available','latest_generation_received':None,'attempt_count':0},
                'emergency_file':{'status':'available','latest_generation_received':None,'attempt_count':0}},
            'evidence_status':'EVIDENCE_PENDING','evidence_complete':False,
            'latest_generation_delivered':None,'raw_traceback_attempted':False,
            'reporter_errors':[]}

def _initialize():
    global HERE,SCANNER_PATH,RUN_ID,REPORT_PREFIX,JSON_OUT,MD_OUT,COMPLETE_OUT,INCOMPLETE_OUT,TERMINAL_STATE,scanner
    with OBS.scope('runner.bootstrap.paths','bootstrap','resolve runner and report paths',observe=False):
        HERE=Path(__file__).resolve().parent
        SCANNER_PATH=HERE/'r6a_r33_live_open_capture.py'
        RUN_ID=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:12]
        OBS.run_id=RUN_ID
        TERMINAL_STATE=_new_terminal_state()
        REPORT_PREFIX=f'R6-A-R46-STRUCTURED-LSOF-DISPOSABLE-REGRESSIONS-{RUN_ID}'
        JSON_OUT=HERE/(REPORT_PREFIX+'.json');MD_OUT=HERE/(REPORT_PREFIX+'.md')
        COMPLETE_OUT=HERE/(REPORT_PREFIX+'-COMPLETE.json');INCOMPLETE_OUT=HERE/(REPORT_PREFIX+'-INCOMPLETE.json')
    with OBS.scope('runner.bootstrap.scanner_load','bootstrap','load unchanged scanner module',paths={'scanner':SCANNER_PATH},observe=False):
        spec=importlib.util.spec_from_file_location('r33',SCANNER_PATH)
        scanner=importlib.util.module_from_spec(spec);spec.loader.exec_module(scanner)
    with OBS.scope('runner.bootstrap.source_hashes','bootstrap','hash exact runner and scanner sources',paths={'scanner':SCANNER_PATH,'runner':Path(__file__)},observe=False):
        report['scanner']={'path':str(SCANNER_PATH),'sha256':hashlib.sha256(SCANNER_PATH.read_bytes()).hexdigest(),'argv_field':scanner.LSOF_ARG}
        helper_path=HERE/'r6a_r45_failure_observability.py'
        report['runner']={'path':str(Path(__file__).resolve()),'sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                          'diagnostic_helper':{'path':str(helper_path),'sha256':hashlib.sha256(helper_path.read_bytes()).hexdigest()},
                          'validation_helper':{'path':str(HERE/'r6a_r45_observability_validation.py'),
                                               'sha256':hashlib.sha256((HERE/'r6a_r45_observability_validation.py').read_bytes()).hexdigest()}}
        report['started_at_unix']=time.time()

class ReportSerializationError(Exception): pass
class ReportPublicationError(Exception): pass
class Case8ObservationError(RuntimeError): pass

def _runtime_type(value): return runtime_type(value)
def _case8_stat_fields(value): return stat_fields(value)

def _op(operation_id,phase,description,call,paths=None,on_success=None,expected=(),handled=False,observe=True):
    def invoke():
        if _SYNTHETIC_OPERATION_OVERRIDE is not None:
            return _SYNTHETIC_OPERATION_OVERRIDE(OBS.current['operation_id'],call)
        return call()
    return OBS.perform(operation_id,phase,description,invoke,paths=paths,on_success=on_success,
                       expected=expected,handled=handled,observe=observe,
                       retain_event=not operation_id.startswith('report.'))

def _scope(operation_id,phase,description,paths=None,observe=True):
    return OBS.scope(operation_id,phase,description,paths=paths,observe=observe,
                     retain_event=not operation_id.startswith('report.'))

def _stat_op(operation_id,phase,description,call,paths=None):
    def retain(value):
        report['identity_samples'].append({'operation_id':OBS.operation_id(operation_id),'case_id':OBS.current['case_id'],
                                          'phase':phase,'source':description,'paths':paths or {},
                                          'fields':stat_fields(value),'identity':identity_components(identity_key(value))})
    return _op(operation_id,phase,description,call,paths=paths,on_success=retain)

def _report_boundary(operation_id):
    def decorate(function):
        def invoke(*args,**kwargs):
            paths={f'argument_{i}':arg for i,arg in enumerate(args) if isinstance(arg,(str,bytes,Path)) and
                   (isinstance(arg,Path) or os.path.isabs(arg))}
            return _op('report.'+operation_id,'report generation/publication',function.__name__,
                       lambda:function(*args,**kwargs),paths=paths,observe=False)
        invoke.__name__=function.__name__
        invoke.__doc__=function.__doc__
        return invoke
    return decorate

def _case8_operation(operation_id,function_name,args,call,suite_root,case_root,child_path=None,
                     root_fd=None,child_fd=None,result_kind=None,call_with_snapshot=False):
    OBS.register(suite_root=suite_root,case_root=case_root,child_path=child_path)
    if root_fd is not None: OBS.register(root_fd=root_fd,root_fd_live=True)
    if child_fd is not None: OBS.register(child_fd=child_fd,child_fd_live=True)
    def update(value):
        if result_kind=='child_path': OBS.register(child_path=value)
        if result_kind=='root_fd': OBS.register(root_fd=value,root_fd_live=True)
        if result_kind=='child_fd': OBS.register(child_fd=value,child_fd_live=True)
    paths={'suite_root':suite_root,'case_root':case_root,**({'child':child_path} if child_path is not None else {})}
    if args and isinstance(args[0],(str,bytes,os.PathLike)):paths['path_argument']=args[0]
    with OBS.scope(operation_id,'case-8 fixture setup',function_name,paths=paths) as event:
        # Attach the event before running the operation, so later failures cannot
        # discard earlier successful samples or this operation's pre-identities.
        report['case8_setup_operations'].append(event)
        event['arguments']=[{'type':runtime_type(argument),'value':os.fspath(argument) if isinstance(argument,os.PathLike) else argument}
                            for argument in args]
        OBS.current['operation_arguments']=event['arguments']
        value=call(event['before']) if call_with_snapshot else call()
        update(value)
        event['result_type']=runtime_type(value)
        if isinstance(value,os.stat_result):event['result_identity']=stat_fields(value)
        return value

def _close_fd(fd,kind):
    def closed(_):
        OBS.register(**{kind+'_live':False})
    return _op(kind+'.close','cleanup','os.close retained '+kind,lambda:os.close(fd),on_success=closed)

def _cleanup(actions):
    # Preserve an in-flight original exception while recording every cleanup
    # failure. Successful cleanup ordering remains the original ordering.
    in_flight=sys.exc_info()[1]
    first=None
    for index,action in enumerate(actions):
        try: action()
        except BaseException as exc:
            OBS.record(exc,context=OBS.context('cleanup.action.'+str(index),'cleanup','record cleanup action before continuing original unwind'))
            if first is None: first=exc
    if in_flight is None and first is not None: raise first

def _case8_identity_ok(record,file_type):
    return (isinstance(record,dict) and record.get('status')=='ok' and
            record.get('fields',{}).get('file_type')==file_type)

def _case8_pre_scan_assertions(snapshot,child_path):
    suite=snapshot['suite_root']; root=snapshot['case_root']; child=snapshot['child']
    pin=snapshot['root_pin']; child_fd=snapshot['child_fd']
    expected_basename=bytes.fromhex('63 68 69 6c 64 0a 6c 69 6e 65 2e 62 69 6e')
    actual_basename=os.fsencode(os.path.basename(os.fspath(child_path)))
    suite_identity=suite.get('lstat',{}).get('identity')
    named_root=root.get('lstat',{}).get('identity')
    canonical_root=root.get('canonical_stat',{}).get('identity')
    pinned_root=pin.get('identity')
    named_child=child.get('lstat',{}).get('identity')
    canonical_child=child.get('canonical_stat',{}).get('identity')
    child_fd_identity=child_fd.get('identity')
    anchor=report.get('case8_root_identity_anchor')
    target=root.get('canonical_path')
    output_paths=[JSON_OUT,MD_OUT,COMPLETE_OUT,INCOMPLETE_OUT,HERE]
    output_evidence=[]
    for destination in output_paths:
        resolved=str(Path(destination).resolve(strict=False))
        contained=False
        try: contained=os.path.commonpath((target,resolved))==target
        except (TypeError,ValueError) as exc:
            contained=True
            OBS.record(exc,context=OBS.context('case8.pre_scan.destination.commonpath','pre-scan evidence','retain handled output-containment observation error',{'destination':destination}),handled=True)
        output_evidence.append({'path':str(destination),'resolved_path':resolved,'inside_case_root':contained})
    checks={
        'suite_root_exists_as_directory':_case8_identity_ok(suite.get('lstat'),stat.S_IFDIR),
        'case_root_exists_as_directory':_case8_identity_ok(root.get('lstat'),stat.S_IFDIR),
        'case_root_canonical_path_resolved':isinstance(root.get('canonical_path'),str) and _case8_identity_ok(root.get('canonical_stat'),stat.S_IFDIR),
        'root_pin_is_live_directory':_case8_identity_ok(pin,stat.S_IFDIR),
        'named_root_matches_root_pin':named_root is not None and named_root==pinned_root,
        'canonical_root_matches_named_root':named_root is not None and named_root==canonical_root,
        'root_matches_creation_anchor':named_root is not None and named_root==anchor,
        'child_basename_bytes_exact':actual_basename==expected_basename,
        'child_exists_as_regular_file':_case8_identity_ok(child.get('lstat'),stat.S_IFREG),
        'child_fd_is_regular_file':_case8_identity_ok(child_fd,stat.S_IFREG),
        'named_child_matches_child_fd':named_child is not None and named_child==child_fd_identity,
        'canonical_child_matches_named_child':named_child is not None and named_child==canonical_child,
        'report_and_evidence_destinations_outside_case_root':all(not item['inside_case_root'] for item in output_evidence),
        'no_prior_observer_errors':not report['case8_observer_errors'],
    }
    evidence={'checks':checks,'result':'PASS' if all(checks.values()) else 'FAIL',
              'expected_child_basename_bytes':expected_basename,'expected_child_basename_hex':expected_basename.hex(),
              'actual_child_basename_bytes':actual_basename,'actual_child_basename_hex':actual_basename.hex(),
              'suite_root_identity':suite_identity,'case_root_named_identity':named_root,
              'case_root_canonical_identity':canonical_root,'case_root_pin_identity':pinned_root,
              'case_root_creation_anchor':anchor,'child_named_identity':named_child,
              'child_fd_identity':child_fd_identity,'child_canonical_identity':canonical_child,
              'case_root_canonical_path':root.get('canonical_path'),'child_canonical_path':child.get('canonical_path'),
              'report_and_evidence_destinations':output_evidence,'snapshot':snapshot}
    report['case8_pre_scan_contract']=evidence
    if not all(checks.values()):
        failed=[name for name,passed in checks.items() if not passed]
        raise AssertionError('case-8 pre-scan contract failed: '+', '.join(failed))
    return evidence

def _canonical_json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')

def _freeze_evidence_value(value):
    """Return an immutable canonical typed-JSON snapshot of one evidence value."""
    tree=_normalize_value(value)
    frozen=_canonical_json(tree)
    restored=_decode_value(json.loads(frozen.decode('ascii')))
    if _normalize_value(restored)!=tree:
        raise ValueError('frozen evidence did not reconstruct canonically')
    return frozen

def _restore_frozen_evidence(frozen):
    """Reconstruct a fresh value from an immutable canonical typed-JSON snapshot."""
    if type(frozen) is not bytes: raise TypeError('frozen evidence must be bytes')
    tree=json.loads(frozen.decode('ascii'))
    restored=_decode_value(tree)
    if _canonical_json(_normalize_value(restored))!=frozen:
        raise ValueError('frozen evidence snapshot is not canonical')
    return restored

def _normalize_value(value):
    """Encode arbitrary supported evidence values as an unambiguous typed tree."""
    if value is None: return {'tag':'null'}
    if type(value) is bool: return {'tag':'bool','value':value}
    if type(value) is str: return {'tag':'str','value':value}
    if type(value) is bytes:
        return {'tag':'bytes','encoding':'base64','data':base64.b64encode(value).decode('ascii'),
                'length':len(value),'sha256':hashlib.sha256(value).hexdigest()}
    if type(value) is int: return {'tag':'int','value':str(value)}
    if type(value) is float:
        if math.isnan(value): return {'tag':'float-special','value':'nan'}
        if math.isinf(value): return {'tag':'float-special','value':'-inf' if value<0 else 'inf'}
        return {'tag':'float','hex':value.hex()}
    if type(value) is list: return {'tag':'list','items':[_normalize_value(x) for x in value]}
    if type(value) is tuple: return {'tag':'tuple','items':[_normalize_value(x) for x in value]}
    if type(value) is dict:
        pairs=[(_normalize_value(k),_normalize_value(v)) for k,v in value.items()]
        pairs.sort(key=lambda pair:_canonical_json(pair[0]))
        return {'tag':'dict','entries':[[k,v] for k,v in pairs]}
    raise TypeError(f'unsupported evidence value type: {type(value).__module__}.{type(value).__qualname__}')

def _assert_json_native(value):
    if type(value) in (str,int,bool) or value is None: return
    if type(value) is list:
        for item in value: _assert_json_native(item)
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value): raise TypeError('normalized JSON object has a non-string key')
        for item in value.values(): _assert_json_native(item)
        return
    raise TypeError(f'normalized tree contains non-JSON runtime value: {type(value).__qualname__}')

def _decode_value(node):
    if type(node) is not dict or type(node.get('tag')) is not str: raise ValueError('malformed typed evidence node')
    tag=node['tag']
    if tag=='null' and set(node)=={'tag'}: return None
    if tag=='bool' and set(node)=={'tag','value'} and type(node['value']) is bool: return node['value']
    if tag=='str' and set(node)=={'tag','value'} and type(node['value']) is str: return node['value']
    if tag=='int' and set(node)=={'tag','value'} and type(node['value']) is str:
        result=int(node['value'])
        if str(result)!=node['value']: raise ValueError('non-canonical integer evidence')
        return result
    if tag=='float' and set(node)=={'tag','hex'} and type(node['hex']) is str:
        result=float.fromhex(node['hex'])
        if not math.isfinite(result) or result.hex()!=node['hex']: raise ValueError('non-canonical float evidence')
        return result
    if tag=='float-special' and set(node)=={'tag','value'}:
        value=node['value']
        if value=='nan': return float('nan')
        if value=='inf': return float('inf')
        if value=='-inf': return float('-inf')
        raise ValueError('unknown special float evidence')
    if tag=='bytes' and set(node)=={'tag','encoding','data','length','sha256'}:
        if node['encoding']!='base64' or type(node['data']) is not str or type(node['length']) is not int or type(node['sha256']) is not str:
            raise ValueError('malformed bytes evidence')
        if node['length']<0 or len(node['sha256'])!=64 or any(c not in '0123456789abcdef' for c in node['sha256']):
            raise ValueError('invalid bytes evidence length or digest')
        raw=base64.b64decode(node['data'].encode('ascii'),validate=True)
        if base64.b64encode(raw).decode('ascii')!=node['data']: raise ValueError('non-canonical Base64 evidence')
        if len(raw)!=node['length']: raise ValueError('bytes evidence length mismatch')
        if hashlib.sha256(raw).hexdigest()!=node['sha256']: raise ValueError('bytes evidence digest mismatch')
        return raw
    if tag in ('list','tuple') and set(node)=={'tag','items'} and type(node['items']) is list:
        values=[_decode_value(x) for x in node['items']]
        return tuple(values) if tag=='tuple' else values
    if tag=='dict' and set(node)=={'tag','entries'} and type(node['entries']) is list:
        result={}
        for pair in node['entries']:
            if type(pair) is not list or len(pair)!=2: raise ValueError('malformed typed mapping entry')
            key=_decode_value(pair[0]); value=_decode_value(pair[1])
            if key in result: raise ValueError('duplicate typed mapping key')
            result[key]=value
        return result
    raise ValueError(f'malformed or unsupported typed evidence tag: {tag}')

def _markdown_diagnostic(value):
    if type(value) is bytes:
        return {'type':'bytes','length':len(value),'sha256':hashlib.sha256(value).hexdigest(),'escaped':repr(value)}
    if type(value) is dict:
        pairs=[{'key':_markdown_diagnostic(k),'value':_markdown_diagnostic(v)} for k,v in value.items()]
        pairs.sort(key=lambda pair:_canonical_json(_normalize_value(pair['key'])))
        return {'type':'dict','entries':pairs}
    if type(value) is tuple: return {'type':'tuple','items':[_markdown_diagnostic(x) for x in value]}
    if type(value) is list: return [_markdown_diagnostic(x) for x in value]
    if value is None or type(value) in (str,int,bool): return value
    if type(value) is float:
        return {'type':'float','hex':value.hex()} if math.isfinite(value) else {'type':'float-special','value':'nan' if math.isnan(value) else ('-inf' if value<0 else 'inf')}
    raise TypeError(f'unsupported Markdown evidence value: {type(value).__qualname__}')

@_report_boundary('markdown')
def _render_markdown(payload,run_id):
    diagnostic=_markdown_diagnostic(payload)
    result=payload.get('result','SYNTHETIC') if type(payload) is dict else 'SYNTHETIC'
    scanner_hash=payload.get('scanner',{}).get('sha256','not-applicable') if type(payload) is dict and type(payload.get('scanner')) is dict else 'not-applicable'
    lines=['# R6-A R46 Observable Structured lsof Scanner Regressions','',f'Run ID: `{run_id}`',f'Result: `{result}`',f'Scanner SHA-256: `{scanner_hash}`','',
           '| Case | Result | Expected | Observed |','|---|---|---|---|']
    cases=payload.get('cases',[]) if type(payload) is dict else []
    for item in cases if type(cases) is list else []:
        if type(item) is not dict: continue
        cells=[]
        for key in ('case','result','expected','observed'):
            rendered=json.dumps(_markdown_diagnostic(item.get(key)),sort_keys=True,ensure_ascii=True,separators=(',',':'))
            cells.append(rendered.replace('|','\\|').replace('\r','\\r').replace('\n','\\n'))
        lines.append('| '+' | '.join(cells)+' |')
    lines+=['','Diagnostic summary (binary values show escaped form, byte length, and SHA-256):','',
            '```json',json.dumps(diagnostic,indent=2,sort_keys=True,ensure_ascii=True,allow_nan=False),'```','']
    return ('\n'.join(lines)).encode('utf-8')

@_report_boundary('prepare')
def _prepare_report(payload,run_id,json_path,markdown_path,complete_path):
    """Finish every fallible serialization and validation step before touching final paths."""
    try:
        tree={'evidence_encoding':EVIDENCE_ENCODING,'version':1,'run_id':run_id,'root':_normalize_value(payload)}
        _assert_json_native(tree)
        json_bytes=(json.dumps(tree,indent=2,sort_keys=True,ensure_ascii=True,allow_nan=False)+'\n').encode('ascii')
        parsed=json.loads(json_bytes.decode('ascii'))
        if parsed!=tree: raise ValueError('JSON parse-back differs from normalized evidence')
        reconstructed=_decode_value(parsed['root'])
        if _normalize_value(reconstructed)!=parsed['root']: raise ValueError('typed evidence did not reconstruct canonically')
        markdown_bytes=_render_markdown(payload,run_id)
        if not json_bytes or not markdown_bytes: raise ValueError('serialized report artifact is empty')
    except Exception as e:
        OBS.record(e,context=OBS.context('report.serialization','report generation','original report serialization/validation exception'))
        raise ReportSerializationError(f'report serialization/validation failed: {type(e).__name__}: {e}') from e
    manifest={'schema':'r6a-r40-report-pair-v1','run_id':run_id,'status':'COMPLETE',
              'json':{'path':json_path.name,'size_bytes':len(json_bytes),'sha256':hashlib.sha256(json_bytes).hexdigest()},
              'markdown':{'path':markdown_path.name,'size_bytes':len(markdown_bytes),'sha256':hashlib.sha256(markdown_bytes).hexdigest()}}
    manifest_bytes=(json.dumps(manifest,indent=2,sort_keys=True,ensure_ascii=True,allow_nan=False)+'\n').encode('ascii')
    if json.loads(manifest_bytes.decode('ascii'))!=manifest: raise ReportSerializationError('completion manifest failed JSON parse-back')
    return {'json':json_bytes,'markdown':markdown_bytes,'manifest':manifest_bytes,'manifest_object':manifest}

@_report_boundary('signature')
def _file_signature(path):
    st=os.lstat(path)
    if not stat.S_ISREG(st.st_mode): raise ReportPublicationError(f'publication path is not a regular file: {path}')
    return (st.st_dev,st.st_ino)

@_report_boundary('directory_fsync')
def _fsync_directory(path):
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:
        try:os.fsync(fd)
        except BaseException as exc:
            OBS.record(exc,context=OBS.context('report.directory.fsync','report publication','fsync publication directory',{'directory':path}))
            raise
    finally:_cleanup([lambda:_op('report.directory.close','cleanup','close publication directory descriptor',lambda:os.close(fd),paths={'directory':path},observe=False)])

@_report_boundary('stage_write')
def _write_stage(path,data,on_created=None):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode): raise OSError('staging path is not a regular file')
        if on_created is not None: on_created(path,(st.st_dev,st.st_ino))
        view=memoryview(data)
        while view:
            count=os.write(fd,view)
            if count<=0: raise OSError('short write while staging report artifact')
            view=view[count:]
        os.fsync(fd)
    except BaseException as exc:
        OBS.record(exc,context=OBS.context('report.stage.write','report publication','original staging write/fsync exception',{'stage':path}))
        raise
    finally:_cleanup([lambda:_op('report.stage.close','cleanup','close original staging descriptor',lambda:os.close(fd),paths={'stage':path},observe=False)])
    actual=Path(path).read_bytes()
    if len(actual)!=len(data) or hashlib.sha256(actual).digest()!=hashlib.sha256(data).digest():
        raise OSError(f'staged artifact verification failed: {path}')

@_report_boundary('verify_complete')
def _verify_complete_pair(json_path,markdown_path,complete_path,incomplete_path):
    if Path(incomplete_path).exists(): raise ValueError('incomplete marker exists')
    manifest=json.loads(Path(complete_path).read_text(encoding='ascii'))
    if (type(manifest) is not dict or manifest.get('schema')!='r6a-r40-report-pair-v1' or
        manifest.get('status')!='COMPLETE' or type(manifest.get('run_id')) is not str):
        raise ValueError('invalid completion manifest')
    for name,path in (('json',Path(json_path)),('markdown',Path(markdown_path))):
        st=os.lstat(path)
        if not stat.S_ISREG(st.st_mode): raise ValueError(f'{name} artifact is not a regular file')
        data=path.read_bytes(); expected=manifest[name]
        if expected.get('path')!=path.name or len(data)!=expected.get('size_bytes') or hashlib.sha256(data).hexdigest()!=expected.get('sha256'):
            raise ValueError(f'{name} artifact does not match completion manifest')
    parsed=json.loads(Path(json_path).read_text(encoding='ascii'))
    if (type(parsed) is not dict or set(parsed)!={'evidence_encoding','version','run_id','root'} or
        parsed.get('evidence_encoding')!=EVIDENCE_ENCODING or type(parsed.get('version')) is not int or parsed.get('version')!=1 or
        parsed.get('run_id')!=manifest.get('run_id')):
        raise ValueError('JSON transcript identity/schema does not match manifest')
    reconstructed=_decode_value(parsed['root'])
    if _normalize_value(reconstructed)!=parsed['root']:
        raise ValueError('JSON transcript typed evidence is not canonical')
    if Path(markdown_path).stat().st_size<=0: raise ValueError('Markdown artifact is empty')
    return manifest

@_report_boundary('incomplete_marker')
def _create_incomplete_marker(path,run_id,phase,remaining):
    data=(json.dumps({'schema':'r6a-r40-report-pair-incomplete-v1','run_id':run_id,'phase':phase,
                      'remaining_artifacts':[str(p) for p in remaining]},indent=2,sort_keys=True)+'\n').encode('ascii')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        view=memoryview(data)
        while view:
            count=os.write(fd,view)
            if count<=0: raise OSError('short write while creating incomplete marker')
            view=view[count:]
        os.fsync(fd)
    except BaseException as exc:
        OBS.record(exc,context=OBS.context('report.incomplete.write','report publication','write incomplete marker',{'incomplete_marker':path}))
        raise
    finally:_cleanup([lambda:_op('report.incomplete.close','cleanup','close original incomplete-marker descriptor',lambda:os.close(fd),paths={'incomplete_marker':path},observe=False)])
    _fsync_directory(Path(path).parent)

def _trigger_fault(fault_inject,phase):
    if fault_inject is not None:
        _op('report.fault.'+phase,'report publication','controlled publication-fault boundary '+phase,
            lambda:fault_inject(phase),observe=False)

@_report_boundary('publish_pair')
def _publish_pair(payload,json_path,markdown_path,complete_path,incomplete_path,run_id,fault_inject=None):
    json_path=Path(json_path); markdown_path=Path(markdown_path); complete_path=Path(complete_path); incomplete_path=Path(incomplete_path)
    parents={p.parent.resolve() for p in (json_path,markdown_path,complete_path,incomplete_path)}
    if len(parents)!=1: raise ReportPublicationError('all report artifacts must share one destination directory')
    if len({p.name for p in (json_path,markdown_path,complete_path,incomplete_path)})!=4: raise ReportPublicationError('report artifact paths must be distinct')
    prepared=_prepare_report(payload,run_id,json_path,markdown_path,complete_path)
    payloads={'json':(json_path,prepared['json']),'markdown':(markdown_path,prepared['markdown']),'manifest':(complete_path,prepared['manifest'])}
    staged={}; stage_sigs={}; published={}; phase='staging'
    try:
        for name,(final,data) in payloads.items():
            phase='stage_'+name
            _trigger_fault(fault_inject,phase)
            stage=final.parent/('.'+final.name+'.'+uuid.uuid4().hex+'.tmp')
            staged[name]=stage
            _write_stage(stage,data,on_created=lambda p,s:stage_sigs.__setitem__(p,s))
        phase='verify_staging'
        _trigger_fault(fault_inject,phase)
        for name,(final,data) in payloads.items():
            stage=staged[name]
            raw=stage.read_bytes()
            if len(raw)!=len(data) or hashlib.sha256(raw).digest()!=hashlib.sha256(data).digest():
                raise OSError(f'staging verification mismatch for {name}')
        phase='publish_json'
        _trigger_fault(fault_inject,phase)
        os.link(staged['json'],json_path,follow_symlinks=False); published[json_path]=stage_sigs[staged['json']]
        os.unlink(staged['json']); staged.pop('json')
        phase='publish_markdown'
        _trigger_fault(fault_inject,phase)
        os.link(staged['markdown'],markdown_path,follow_symlinks=False); published[markdown_path]=stage_sigs[staged['markdown']]
        os.unlink(staged['markdown']); staged.pop('markdown')
        phase='sync_artifacts'
        _trigger_fault(fault_inject,phase); _fsync_directory(json_path.parent)
        phase='publish_manifest'
        _trigger_fault(fault_inject,phase)
        os.link(staged['manifest'],complete_path,follow_symlinks=False); published[complete_path]=stage_sigs[staged['manifest']]
        os.unlink(staged['manifest']); staged.pop('manifest')
        phase='sync_manifest'
        _trigger_fault(fault_inject,phase); _fsync_directory(json_path.parent)
        manifest=_verify_complete_pair(json_path,markdown_path,complete_path,incomplete_path)
        return {'run_id':run_id,'json_path':str(json_path),'json_size_bytes':len(prepared['json']),'json_sha256':manifest['json']['sha256'],
                'markdown_path':str(markdown_path),'markdown_size_bytes':len(prepared['markdown']),'markdown_sha256':manifest['markdown']['sha256'],
                'complete_path':str(complete_path),'complete_size_bytes':len(prepared['manifest']),
                'complete_sha256':hashlib.sha256(prepared['manifest']).hexdigest()}
    except Exception as e:
        failure_record=OBS.record(e,context=OBS.context('report.'+phase,'report publication','original publication failure before rollback',
                                                       {'json':json_path,'markdown':markdown_path,'complete':complete_path,'incomplete':incomplete_path}))
        failure_record['publication_state_before_rollback']={'phase':phase,
            'published_owned_signatures':{str(path):sig for path,sig in published.items()},
            'stage_owned_signatures':{str(path):sig for path,sig in stage_sigs.items()},
            'staged_paths':{name:str(path) for name,path in staged.items()}}
        failure_record['publication_rollback_observations']=[]
        cleanup_errors=[]
        for path,sig in list(published.items()):
            try:
                observed_sig=_file_signature(path)
                failure_record['publication_rollback_observations'].append({'path':str(path),'expected_signature':sig,'observed_signature':observed_sig,'kind':'final'})
                if observed_sig==sig: os.unlink(path)
                else: cleanup_errors.append(f'ownership changed for final path {path}')
            except FileNotFoundError: pass
            except Exception as cleanup_error:
                OBS.record(cleanup_error,context=OBS.context('report.rollback.final','cleanup','owned final-file rollback',{'path':path}))
                cleanup_errors.append(f'{path}: {type(cleanup_error).__name__}: {cleanup_error}')
        for path,sig in list(stage_sigs.items()):
            try:
                if Path(path).exists():
                    observed_sig=_file_signature(path)
                    failure_record['publication_rollback_observations'].append({'path':str(path),'expected_signature':sig,'observed_signature':observed_sig,'kind':'stage'})
                    if observed_sig==sig:os.unlink(path)
            except Exception as cleanup_error:
                OBS.record(cleanup_error,context=OBS.context('report.rollback.stage','cleanup','owned staging-file rollback',{'path':path}))
                cleanup_errors.append(f'{path}: {type(cleanup_error).__name__}: {cleanup_error}')
        try: _fsync_directory(json_path.parent)
        except Exception as sync_error:
            OBS.record(sync_error,context=OBS.context('report.rollback.fsync','cleanup','rollback directory fsync',{'directory':json_path.parent}))
            cleanup_errors.append(f'cleanup directory fsync failed: {type(sync_error).__name__}: {sync_error}')
        if cleanup_errors:
            try: _create_incomplete_marker(incomplete_path,run_id,phase,cleanup_errors)
            except Exception as marker_error:
                OBS.record(marker_error,context=OBS.context('report.incomplete.failure','report publication','incomplete marker failure',{'incomplete':incomplete_path}))
                cleanup_errors.append(f'incomplete marker creation failed: {type(marker_error).__name__}: {marker_error}')
            try: _fsync_directory(json_path.parent)
            except Exception as sync_error:
                OBS.record(sync_error,context=OBS.context('report.incomplete.fsync','report publication','incomplete marker directory fsync',{'directory':json_path.parent}))
                cleanup_errors.append(f'incomplete marker directory fsync failed: {type(sync_error).__name__}: {sync_error}')
        detail=f'{type(e).__name__}: {e}'
        if cleanup_errors: detail+='; cleanup='+' | '.join(cleanup_errors)
        raise ReportPublicationError(f'report publication failed during {phase}: {detail}') from e

def _reporting_preflight(run_id=None,output_directory=None):
    run_id=RUN_ID if run_id is None else run_id
    output_directory=HERE if output_directory is None else output_directory
    """Exercise serialization and publication without invoking lsof or scanner cases."""
    checks=[]
    def passed(name,condition=True):
        if not condition: raise AssertionError(f'synthetic reporting check failed: {name}')
        checks.append({'name':name,'result':'PASS'})
    sample_bytes=b'\x00\xff\x80\x81\xf5\n'
    synthetic={'schema_version':1,'result':'SYNTHETIC','plain':{'text':'scalar','integer':7,'boolean':True,'none':None},
               'nested':{'raw':sample_bytes,'empty':b'','arbitrary_non_utf8':b'\xff\xfe\xc0\xaf',
                         'fd_raw':b'17','list':[{'inner':[b'\x00',{'deep':b'\x80'}]}]},
               'cases':[{'case':'synthetic bytes','result':'PASS','expected':b'|pipe\nfield','observed':{'raw':sample_bytes}}]}
    tree1={'evidence_encoding':EVIDENCE_ENCODING,'version':1,'run_id':run_id,'root':_normalize_value(synthetic)}
    tree2={'evidence_encoding':EVIDENCE_ENCODING,'version':1,'run_id':run_id,'root':_normalize_value(synthetic)}
    scalar_payload={'result':'SYNTHETIC_SCALAR','text':'plain scalar','integer':7,'boolean':True,'none':None}
    scalar_tree=_normalize_value(scalar_payload)
    _assert_json_native(scalar_tree)
    scalar_json=json.dumps(scalar_tree,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)
    scalar_parsed=json.loads(scalar_json)
    passed('plain_scalar_report_serializes',scalar_parsed==scalar_tree and _decode_value(scalar_parsed)==scalar_payload)
    passed('nested_bytes_base64_reconstruction',_decode_value(_normalize_value({'bytes':sample_bytes}))=={'bytes':sample_bytes})
    passed('empty_bytes_preserved',_decode_value(_normalize_value(b''))==b'')
    passed('arbitrary_non_utf8_bytes_preserved',_decode_value(_normalize_value(b'\xff\xfe\xc0\xaf'))==b'\xff\xfe\xc0\xaf')
    passed('byte_fd_raw_preserved',_decode_value(_normalize_value({'fd_raw':b'17'}))['fd_raw']==b'17')
    passed('nested_list_dict_bytes_preserved',_decode_value(tree1['root'])['nested']['list'][0]['inner'][1]['deep']==b'\x80')
    _assert_json_native(tree1); passed('normalized_tree_has_only_json_native_values')
    passed('repeated_normalization_is_deterministic',_canonical_json(tree1)==_canonical_json(tree2))
    parsed=json.loads((json.dumps(tree1,sort_keys=True,ensure_ascii=True,allow_nan=False)+'\n'))
    passed('serialized_json_parses',parsed==tree1)
    roundtrip=_decode_value(parsed['root'])
    bytes_node=parsed['root']['entries'] if parsed['root']['tag']=='dict' else []
    # Every byte node validates its own length and SHA-256 in _decode_value.
    passed('reconstructed_byte_hashes_match',roundtrip==synthetic and bytes_node is not None)
    md=_render_markdown(synthetic,run_id)
    passed('markdown_handles_binary_fields',b'bytes' in md and hashlib.sha256(sample_bytes).hexdigest().encode() in md)
    with tempfile.TemporaryDirectory(prefix='r40-serialization-failure-') as td:
        base=Path(td); j=base/'bad.json'; m=base/'bad.md'; c=base/'bad-complete.json'; inc=base/'bad-incomplete.json'
        try:
            with OBS.expected_failures():_publish_pair({'unsupported':object()},j,m,c,inc,run_id)
        except ReportSerializationError: serialization_rejected=True
        except Exception: serialization_rejected=True
        else: serialization_rejected=False
        passed('serialization_failure_creates_no_final_artifacts',serialization_rejected and not any(p.exists() for p in (j,m,c,inc)))
    def inject_publish_failure(phase):
        if phase=='publish_markdown': raise OSError('synthetic second-artifact publication failure')
    with tempfile.TemporaryDirectory(prefix='r40-publication-failure-') as td:
        base=Path(td); j=base/'bad.json'; m=base/'bad.md'; c=base/'bad-complete.json'; inc=base/'bad-incomplete.json'
        try:
            with OBS.expected_failures():_publish_pair(synthetic,j,m,c,inc,run_id,fault_inject=inject_publish_failure)
        except ReportPublicationError as e: publication_error='publish_markdown' in str(e) and 'synthetic second-artifact' in str(e)
        else: publication_error=False
        passed('publication_failure_explicit_and_fail_closed',publication_error and not c.exists() and not j.exists() and not m.exists())
    # A successful end-to-end synthetic publication provides durable, hash-addressed preflight evidence.
    preflight_payload={'schema_version':1,'result':'SYNTHETIC_PREFLIGHT_PASS','run_id':run_id,'serialization_checks':checks,
                       'binary_roundtrip':synthetic['nested'],'scanner_cases_executed':False,
                       'expected_failure_records':list(report['expected_exception_records'])}
    prefix=f'R6-A-R46-SYNTHETIC-REPORT-PREFLIGHT-{run_id}'
    base=Path(output_directory)
    j=base/(prefix+'.json'); m=base/(prefix+'.md'); c=base/(prefix+'-COMPLETE.json'); inc=base/(prefix+'-INCOMPLETE.json')
    artifacts=_publish_pair(preflight_payload,j,m,c,inc,run_id)
    verified=_verify_complete_pair(j,m,c,inc)
    passed('synthetic_complete_report_artifacts_nonempty_and_hash_recorded',
           artifacts['json_size_bytes']>0 and artifacts['markdown_size_bytes']>0 and
           len(verified['json']['sha256'])==64 and len(verified['markdown']['sha256'])==64)
    artifacts['checks']=checks
    _op('report.preflight.print','terminal output','print synthetic reporting receipt',lambda:print('R46_REPORT_PREFLIGHT='+json.dumps(artifacts,sort_keys=True)),observe=False)
    return artifacts

def capview(c):
    with _scope('capture.serialize','result decoding','retain complete capture and raw parsed rows'):
        raw=c.get('stdout_raw',b'');err=c.get('stderr_raw',b'')
        return {'argv':c.get('argv'),'child_pid':c.get('child_pid'),'child_ppid':c.get('child_ppid'),'child_identity':c.get('child_identity'),'child_identity_error':c.get('child_identity_error'),'returncode':c.get('returncode'),'stdout_bytes':c.get('stdout_total_bytes'),'stdout_captured_bytes':c.get('stdout_bytes_captured'),'stdout_sha256':c.get('stdout_sha256'),'stdout_captured_sha256':c.get('stdout_captured_sha256'),'stdout_base64':c.get('stdout_base64'),'stdout_repr':repr(raw),'stderr_bytes':c.get('stderr_total_bytes'),'stderr_sha256':c.get('stderr_sha256'),'stderr_base64':c.get('stderr_base64'),'stderr_repr':repr(err),'timed_out':c.get('timed_out'),'stdout_capped':c.get('stdout_capped'),'stderr_capped':c.get('stderr_capped'),'scan_complete':c.get('scan_complete'),'stream':c.get('stream'),'rows':[{'pid':r.get('pid'),'ppid':r.get('ppid'),'fd_raw':repr(r.get('fd_raw')),'fd_number':r.get('fd_number'),'type_raw':repr(r.get('fd_type_raw')),'device':r.get('device'),'inode':r.get('inode'),'name_raw':repr(r.get('name_raw')),'name_base64':base64.b64encode(r.get('name_raw',b'')).decode(),'name_decoded':repr(r.get('name_decoded')),'name_decoded_base64':base64.b64encode(r.get('name_decoded',b'')).decode(),'parser_status':r.get('parser_status')} for r in c.get('rows',[])],
                'stdout_raw':raw,'stderr_raw':err,'rows_raw':c.get('rows',[])}

def add(name,expected,observed,ok,evidence=None):
    with _scope('result.record','case result','record original case predicate and evidence'):
        x={'case':name,'expected':expected,'observed':observed,'result':'PASS' if ok else 'FAIL'}
        if evidence is not None:x['evidence']=evidence
        report['cases'].append(x)
        if not ok and report['first_failure'] is None:
            report['first_failure']={'case':name,'expected':expected,'observed':observed}
            try:raise AssertionError('original case predicate failed: '+name)
            except AssertionError as exc:
                record=OBS.record(exc)
                record['case_predicate']={'case':name,'expected':expected,'observed':observed}
        return ok

def scan(root):
    target=_op('scan.target.canonicalize','canonicalization','os.path.realpath before unchanged scanner call',
               lambda:os.path.realpath(root,strict=True),paths={'root':root})
    return _op('scan.invoke','scanner invocation','scanner.scan_lsof_path before real subprocess dispatch',
               lambda:scanner.scan_lsof_path(target),paths={'root':root,'canonical_root':target},
               on_success=lambda value:OBS.register(scanner_capture=value))

def scan_case3_with_scanner_cwd(root,fd,scanner_cwd):
    with _scope('scan.cwd_override','scanner process isolation','preserve existing Case-3-only scanner cwd override',paths={'root':root,'scanner_cwd':scanner_cwd}):
        expected_argv=[scanner.LSOF,scanner.LSOF_ARG,'+D',os.path.realpath(root,strict=True)]
        original_popen=scanner.subprocess.Popen
        override_matches=0
        def popen_with_case3_cwd(argv,*args,**kwargs):
            nonlocal override_matches
            if argv==expected_argv:
                kwargs['cwd']=scanner_cwd
                override_matches+=1
            return _op('scan.child.spawn','scanner invocation','delegate original Popen with existing isolation override',
                       lambda:original_popen(argv,*args,**kwargs),paths={'root':root,'scanner_cwd':scanner_cwd},
                       on_success=lambda p:OBS.register(scanner_capture={'child_pid':p.pid,'child_ppid':os.getpid(),'child_identity':None}),observe=False)
        scanner.subprocess.Popen=popen_with_case3_cwd
        try:
            result=scan_with_checker_pin(root,fd)
        finally:
            scanner.subprocess.Popen=original_popen
        return (*result,override_matches)

def opendir(path):
    return _op('root_pin.open','root-pin acquisition','os.open read-only directory with original flags',
               lambda:os.open(path,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)),paths={'root':path},
               on_success=lambda fd:OBS.register(root_fd=fd,root_fd_live=True))

def canonical_path(raw_path):
    with _scope('path.canonicalize','canonicalization','strict canonical path bytes',paths={'path':raw_path}):
        raw=os.fsencode(raw_path) if isinstance(raw_path,str) else raw_path
        if not isinstance(raw,bytes) or not os.path.isabs(raw):raise ValueError('path is not absolute')
        canonical=os.path.realpath(raw,strict=True)
        if not os.path.isabs(canonical):raise ValueError('canonical path is not absolute')
        return canonical

def lsof_type(mode):
    if stat.S_ISDIR(mode):return b'DIR'
    if stat.S_ISREG(mode):return b'REG'
    if stat.S_ISLNK(mode):return b'LINK'
    if stat.S_ISCHR(mode):return b'CHR'
    if stat.S_ISBLK(mode):return b'BLK'
    if stat.S_ISFIFO(mode):return b'FIFO'
    if stat.S_ISSOCK(mode):return b'unix'
    return b'UNKNOWN'

def path_evidence(raw_path):
    with _scope('path.evidence','result decoding','retain raw and canonical path spellings',paths={'path':raw_path}):
        raw=os.fsencode(raw_path) if isinstance(raw_path,str) else raw_path
        canonical=canonical_path(raw)
        return {'raw_path':os.fsdecode(raw),'raw_path_repr':repr(raw),'raw_path_base64':base64.b64encode(raw).decode(),
                'canonical_path':os.fsdecode(canonical),'canonical_path_repr':repr(canonical),
                'canonical_path_base64':base64.b64encode(canonical).decode(),'canonicalization_status':'RESOLVED',
                'raw_path_bytes':raw,'canonical_path_bytes':canonical}

def object_identity(info):
    return {'device':info.st_dev,'inode':info.st_ino,'type_raw':lsof_type(info.st_mode).decode(),
            'mode_type':stat.S_IFMT(info.st_mode)}

def identity_key(info):
    return (info.st_dev,info.st_ino,stat.S_IFMT(info.st_mode))

def helper_identity_key(identity):
    return (identity['device'],identity['inode'],identity['mode_type'])

def identity_components(identity):
    return {'device':identity[0],'inode':identity[1],'file_type':identity[2]}

def emitted_path_evidence(row):
    with _scope('row.path_evidence','result decoding','decode lsof name then strictly canonicalize and stat the exact path',paths={'row_name_raw':row['name_raw'],'row_name_decoded':row.get('name_decoded')}):
        raw=row['name_raw'];decoded=scanner.decode_lsof_name(raw)
        if decoded!=row.get('name_decoded'):raise Case8ObservationError('parsed lsof name decode differs from the preserved raw field')
        canonical=canonical_path(decoded)
        canonical_stat=_stat_op('row.path.canonical_stat','canonical path identity','os.stat strict canonical result',
                                lambda:os.stat(canonical,follow_symlinks=True),
                                {'row_name_raw':raw,'row_name_decoded':decoded,'canonical_path':canonical})
        canonical_identity=identity_key(canonical_stat)
        row_file_type={b'DIR':stat.S_IFDIR,b'REG':stat.S_IFREG,b'LINK':stat.S_IFLNK,b'CHR':stat.S_IFCHR,
                       b'BLK':stat.S_IFBLK,b'FIFO':stat.S_IFIFO,b'unix':stat.S_IFSOCK}.get(row['fd_type_raw'])
        row_identity=(row['device'],row['inode'],row_file_type)
        return {'name_raw_repr':repr(raw),'name_raw_base64':base64.b64encode(raw).decode(),
                'name_raw_bytes':raw,'name_decoded_repr':repr(decoded),
                'name_decoded_base64':base64.b64encode(decoded).decode(),
                'name_decoded_bytes':decoded,
                'name_canonical':os.fsdecode(canonical),'name_canonical_repr':repr(canonical),
                'name_canonical_base64':base64.b64encode(canonical).decode(),
                'name_canonical_bytes':canonical,
                'canonical_stat_identity':identity_components(canonical_identity),
                'lsof_row_identity':{'device':row['device'],'inode':row['inode'],'file_type':row_file_type},
                'canonical_stat_matches_row_identity':canonical_identity==row_identity}

def pinrows(c,fd,root):
    with _scope('scan.pinrow.classify','row classification','original exact root-pin row matcher',paths={'root':root}):
        expected=canonical_path(root)
        descriptor=_op('scan.pinrow.root.fstat','root-pin stat','os.fstat exact retained pin',lambda:os.fstat(fd),paths={'root':root})
        named=_op('scan.pinrow.root.named_stat','named root stat','os.stat no-follow root',lambda:os.stat(root,follow_symlinks=False),paths={'root':root})
        canonical_stat=_op('scan.pinrow.root.canonical_stat','canonical root identity','os.stat strict canonical root',
                           lambda:os.stat(expected,follow_symlinks=True),paths={'root':root,'canonical_root':expected})
        expected_identity=object_identity(descriptor)
        if expected_identity!=object_identity(named) or expected_identity!=object_identity(canonical_stat):return []
        return [r for r in c['rows'] if r['pid']==os.getpid() and r['fd_number']==fd and r['fd_raw']==str(fd).encode()
                and r['fd_type_raw']==b'DIR' and r['device']==descriptor.st_dev and r['inode']==descriptor.st_ino
                and r['name_decoded']==expected]

def checker_root_snapshot(root,fd,stage):
    with _scope('scan.'+stage+'.root.snapshot',stage+' root identity','retain every actual root observation incrementally',paths={'root':root}):
        sample={'case_id':OBS.current['case_id'],'stage':stage,'source':'checker_root_snapshot','root':root,'pin_fd':fd,'observations':{}}
        report['identity_samples'].append(sample)
        canonical=_op('scan.'+stage+'.root.canonicalize','canonicalization','strict root canonicalization',lambda:canonical_path(root),paths={'root':root})
        sample['canonical_root_bytes']=canonical
        named=_stat_op('scan.'+stage+'.root.named_stat',stage+' named-root identity','os.stat no-follow root',lambda:os.stat(root,follow_symlinks=False),paths={'root':root})
        sample['observations']['named_root']=identity_components(identity_key(named))
        pinned=_stat_op('scan.'+stage+'.root.pin_fstat',stage+' pinned-root identity','os.fstat original root pin',lambda:os.fstat(fd),paths={'root':root})
        sample['observations']['pinned_root']=identity_components(identity_key(pinned))
        canonical_named=_stat_op('scan.'+stage+'.root.canonical_stat',stage+' canonical-root identity','os.stat resolved root',lambda:os.stat(canonical,follow_symlinks=True),paths={'canonical_root':canonical})
        sample['observations']['canonical_root']=identity_components(identity_key(canonical_named))
        identities={'named_root':identity_key(named),'pinned_root':identity_key(pinned),'canonical_root':identity_key(canonical_named)}
        return {'raw_root':os.fsdecode(os.fsencode(root)),'raw_root_bytes':os.fsencode(root),
                'canonical_root':os.fsdecode(canonical),'canonical_root_bytes':canonical,'identities':identities}

def live_scan_contract(c):
    with _scope('scan.result_contract','result decoding','evaluate original complete status-zero scan checks'):
        rows=c.get('rows');stream=c.get('stream');stdout=c.get('stdout_raw');stderr=c.get('stderr_raw')
        rows=rows if type(rows) is list else []
        stream=stream if type(stream) is dict else {}
        stdout=stdout if type(stdout) is bytes else b''
        stderr=stderr if type(stderr) is bytes else b''
        checks={
            'returncode_zero':c.get('returncode')==0,
            'stderr_empty':c.get('stderr_total_bytes')==0 and c.get('stderr_bytes_captured')==0 and not stderr,
            'framing_complete':stream.get('complete') is True,
            'framing_row_count_matches':stream.get('row_count')==len(rows),
            'framing_bytes_match':stream.get('bytes_consumed')==c.get('stdout_total_bytes')==len(stdout),
            'stdout_uncapped_and_fully_captured':c.get('stdout_capped') is False and c.get('stdout_bytes_captured')==c.get('stdout_total_bytes'),
            'stderr_uncapped':c.get('stderr_capped') is False,
            'not_timed_out':c.get('timed_out') is False,
            'child_identity_valid':'child_identity_error' not in c,
            'all_rows_parse':all(type(row) is dict and row.get('parser_status')=='PASS' for row in rows),
            'stdout_digest_matches':c.get('stdout_sha256')==c.get('stdout_captured_sha256')==hashlib.sha256(stdout).hexdigest(),
            'stderr_digest_matches':c.get('stderr_sha256')==hashlib.sha256(stderr).hexdigest(),
            'scanner_complete':c.get('scan_complete') is True,
        }
        return {'ok':all(checks.values()),'checks':checks,'rows':len(rows),'stdout_bytes':len(stdout),'stderr_bytes':len(stderr)}

def scan_with_checker_pin(root,fd):
    with _scope('scan.boundary','scanner boundary','original pinned scan and root revalidation',paths={'root':root}):
        before=checker_root_snapshot(root,fd,'pre_scan')
        c=scan(root)
        after=checker_root_snapshot(root,fd,'post_scan')
        with _scope('scan.root_contract.decode','result decoding','build original root-stability comparisons',paths={'root':root}):
            before_ids=before['identities'];after_ids=after['identities']
            comparisons={
                'named_equals_pinned_before':before_ids['named_root']==before_ids['pinned_root'],
                'canonical_equals_named_before':before_ids['canonical_root']==before_ids['named_root'],
                'named_stable_before_after':before_ids['named_root']==after_ids['named_root'],
                'pinned_stable_before_after':before_ids['pinned_root']==after_ids['pinned_root'],
                'named_equals_pinned_after':after_ids['named_root']==after_ids['pinned_root'],
                'canonical_equals_named_after':after_ids['canonical_root']==after_ids['named_root'],
                'canonical_path_stable':before['canonical_root']==after['canonical_root'],
                'all_root_identities_equal':len(set(before_ids.values())|set(after_ids.values()))==1,
            }
            matches=pinrows(c,fd,root)
            contract={'identity_definition':['device','inode','file_type_stat.S_IFMT'],
                      'pin_fd':fd,'raw_root':before['raw_root'],'canonical_root':before['canonical_root'],
                      'raw_root_bytes':before['raw_root_bytes'],'canonical_root_before_bytes':before['canonical_root_bytes'],
                      'canonical_root_after':after['canonical_root'],'canonical_root_after_bytes':after['canonical_root_bytes'],
                      'before':{name:identity_components(key) for name,key in before_ids.items()},
                      'after':{name:identity_components(key) for name,key in after_ids.items()},
                      'comparisons':comparisons,'stable':all(comparisons.values()),'exact_pin_row_count':len(matches),
                      'raw_pin_rows':matches}
            return c,matches,contract

def runscan_case(name,root,check):
    with _scope('runscan_case','scanner boundary','legacy helper with unified structured fallback',paths={'root':root}):
        try:
            c=scan(root);ok,obs=check(c)
            return add(name,obs['expected'],obs['observed'],ok,{'capture':capview(c),**obs.get('extra',{})})
        except Exception as exc:
            record=OBS.record(exc)
            return add(name,'scanner operation completes under stated contract',record,False)

def helper(mode,root,child=None):
    with _scope('helper.initialize','helper initialization','prepare and initialize original foreign holder',paths={'root':root,**({'child':child} if child else {})}):
        return _helper_initialize(mode,root,child)

def _helper_source():
    # The original holder operations/protocol remain verbatim. The outer child
    # boundary only writes structured failure evidence to the existing stderr.
    body=("m=sys.argv[1]; r=sys.argv[2]; c=sys.argv[3]\n"
          "operation='helper.child.open'\n"
          "fd=os.open(r,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)) if m=='root' else (os.open(c,os.O_RDONLY) if m=='child' else None)\n"
          "operation='helper.child.stat'\n"
          "st=os.fstat(fd) if fd is not None else os.stat(r,follow_symlinks=False)\n"
          "def kind(mode): return 'DIR' if stat.S_ISDIR(mode) else ('REG' if stat.S_ISREG(mode) else 'UNKNOWN')\n"
          "operation='helper.child.ready'\n"
          "print('READY',fd if fd is not None else 'cwd',st.st_dev,st.st_ino,kind(st.st_mode),stat.S_IFMT(st.st_mode),flush=True)\n"
          "operation='helper.child.command'\n"
          "command=sys.stdin.readline().strip()\n"
          "if command=='POST_SCAN_IDENTITY':\n"
          " operation='helper.child.post.stat'\n"
          " st=os.fstat(fd) if fd is not None else os.stat(r,follow_symlinks=False)\n"
          " operation='helper.child.post.write'\n"
          " print('POST',fd if fd is not None else 'cwd',st.st_dev,st.st_ino,kind(st.st_mode),stat.S_IFMT(st.st_mode),flush=True)\n"
          " operation='helper.child.post.command'\n"
          " sys.stdin.readline()\n"
          "operation='helper.child.close'\n"
          "os.close(fd) if fd is not None else None\n")
    code=("import os,sys,stat,traceback,json,base64,hashlib,math\n"
          "def typed(v):\n"
          " if v is None:return {'tag':'null'}\n"
          " if type(v) is bool:return {'tag':'bool','value':v}\n"
          " if type(v) is str:return {'tag':'str','value':v}\n"
          " if type(v) is int:return {'tag':'int','value':str(v)}\n"
          " if type(v) is float:return {'tag':'float','hex':v.hex()} if math.isfinite(v) else {'tag':'float-special','value':'nan' if math.isnan(v) else ('-inf' if v<0 else 'inf')}\n"
          " if type(v) is bytes:return {'tag':'bytes','encoding':'base64','data':base64.b64encode(v).decode('ascii'),'length':len(v),'sha256':hashlib.sha256(v).hexdigest()}\n"
          " if type(v) in (list,tuple):return {'tag':'tuple' if type(v) is tuple else 'list','items':[typed(x) for x in v]}\n"
          " if type(v) is dict:\n"
          "  pairs=[(typed(k),typed(x)) for k,x in v.items()];pairs.sort(key=lambda pair:json.dumps(pair[0],sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii'));return {'tag':'dict','entries':[[k,x] for k,x in pairs]}\n"
          " raise TypeError('unsupported helper evidence type')\n"
          "operation='helper.child.bootstrap'\ntry:\n"+''.join(' '+line+'\n' for line in body.splitlines())+
          "except BaseException as original:\n"
          " original_tb=original.__traceback__\n"
          " try:\n"
          "  sys.path.insert(0,"+repr(str(HERE))+")\n"
          "  from r6a_r45_failure_observability import FailureRecorder\n"
          "  evidence_report={'cases':[],'cleanup':[],'first_failure':None}\n"
          "  recorder=FailureRecorder(evidence_report,"+repr(RUN_ID)+")\n"
          "  recorder.set_case("+repr(OBS.current['case_id'])+",suite_root="+repr(OBS.resources['suite_root'])+",case_root=sys.argv[2],child_path=sys.argv[3] or None)\n"
          "  recorder.current.update(operation_id='case'+"+repr(OBS.current['case_id'])+"+'.'+operation,phase='helper child operation',description=operation,input_paths={'root':sys.argv[2],'child':sys.argv[3]})\n"
          "  failure=recorder.record(original,tb=original_tb)\n"
          "  failure['root_pin_identity_if_live']={'status':'checker_pin_not_owned_by_helper_process'}\n"
          "  failure['helper_identity_if_live']={'status':'self','pid':os.getpid(),'ppid':os.getppid(),'held_fd':locals().get('fd')}\n"
          "  if locals().get('fd') is not None:\n"
          "   try:\n"
          "    held=os.fstat(fd);failure['helper_identity_if_live']['held_identity']={'device':held.st_dev,'inode':held.st_ino,'file_type':stat.S_IFMT(held.st_mode)}\n"
          "   except BaseException as evidence_error:\n"
          "    secondary=recorder.record(evidence_error,context=recorder.context(operation+'.failure_fstat','evidence capture','observe original helper descriptor after failure'));secondary['parent_failure_id']=failure['failure_id'];failure['evidence_capture_failures'].append(secondary);failure['evidence_complete']=False\n"
          "  sys.stderr.write('R46_HELPER_FAILURE='+json.dumps({'evidence_encoding':'r6a-r40-typed-json','version':1,'root':typed(recorder.emergency_payload())},sort_keys=True,ensure_ascii=True)+'\\n');sys.stderr.flush()\n"
          " except BaseException as evidence_error:\n"
          "  traceback.print_exception(type(original),original,original_tb,file=sys.stderr)\n"
          "  traceback.print_exception(type(evidence_error),evidence_error,evidence_error.__traceback__,file=sys.stderr)\n"
          " raise\n")
    return code

def _helper_initialize(mode,root,child=None):
    code=_helper_source()
    p=_op('helper.spawn','helper initialization','original foreign helper subprocess dispatch',
          lambda:subprocess.Popen([sys.executable,'-c',code,mode,root,child or ''],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=root if mode=='cwd' else None),
          paths={'root':root,**({'child':child} if child else {})},on_success=lambda value:OBS.register(helper=value))
    try:
        ready,_,_=_op('helper.ready.wait','helper initialization','select existing helper stdout',lambda:select.select([p.stdout],[],[],5))
        with _scope('helper.ready.decode','helper initialization','decode unchanged READY identity protocol'):
            if not ready:raise RuntimeError('foreign helper readiness failed')
            fields=p.stdout.readline().split()
            if len(fields)!=6 or fields[0]!='READY':raise RuntimeError('foreign helper identity handshake failed')
            descriptor=None if fields[1]=='cwd' else int(fields[1])
            identity={'fd_raw':b'cwd' if descriptor is None else str(descriptor).encode(),
                      'fd_number':descriptor,'device':int(fields[2]),'inode':int(fields[3]),'type_raw':fields[4].encode(),
                      'mode_type':int(fields[5])}
            OBS.register(helper_identity=identity)
            return p,identity
    except BaseException:
        def kill_if_live():
            if p.poll() is None:_op('helper.initialize.kill','cleanup','kill helper after original initialization failure',p.kill)
        _cleanup([kill_if_live,lambda:_op('helper.initialize.wait','cleanup','wait helper after initialization failure',lambda:p.wait(timeout=5))])
        raise

def helper_post_scan_identity(p,expected_fd):
    with _scope('helper.post.identity','post-scan helper identity','same retained helper FD POST_SCAN_IDENTITY protocol'):
        if _op('helper.post.poll','post-scan helper identity','poll existing helper',p.poll) is not None:raise RuntimeError('foreign helper exited before post-scan identity capture')
        _op('helper.post.request.write','post-scan helper identity','write original POST request',lambda:p.stdin.write('POST_SCAN_IDENTITY\n'))
        _op('helper.post.request.flush','post-scan helper identity','flush original POST request',p.stdin.flush)
        ready,_,_=_op('helper.post.wait','post-scan helper identity','select original POST response',lambda:select.select([p.stdout],[],[],5))
        with _scope('helper.post.decode','post-scan helper identity','decode original POST identity response'):
            if not ready:raise RuntimeError('foreign helper post-scan identity timed out')
            fields=p.stdout.readline().split()
            if len(fields)!=6 or fields[0]!='POST':raise RuntimeError('foreign helper post-scan identity handshake failed')
            descriptor=None if fields[1]=='cwd' else int(fields[1])
            if descriptor!=expected_fd:raise RuntimeError('foreign helper post-scan identity came from a different descriptor')
            return {'fd_number':descriptor,'device':int(fields[2]),'inode':int(fields[3]),'type_raw':fields[4].encode(),'mode_type':int(fields[5])}

def stop(p):
    with _scope('helper.stop','helper shutdown','preserve original graceful-then-forced helper shutdown'):
        if _op('helper.stop.poll','helper shutdown','poll original helper',p.poll) is None:
            try:
                _op('helper.stop.write','helper shutdown','write original empty shutdown command',lambda:p.stdin.write('\n'),handled=True)
                _op('helper.stop.flush','helper shutdown','flush original shutdown command',p.stdin.flush,handled=True)
                _op('helper.stop.wait','helper shutdown','wait original graceful shutdown',lambda:p.wait(timeout=5),handled=True)
            except Exception:
                _op('helper.stop.kill','helper shutdown','original fallback helper kill',p.kill)
                _op('helper.stop.kill_wait','helper shutdown','original fallback helper wait',lambda:p.wait(timeout=5))
        report['cleanup'].append({'pid':p.pid,'returncode':p.returncode,'stderr_raw':OBS.resources['helper_stderr_raw']})

def _case8_success_schema(*,root_before,root_after,child_before,child_after,path_bytes,basename_bytes,
                          pin_fd,child_fd,checker_pid,scanner_rows,root_pin_rows,child_rows,scanner_child):
    """Pure shared schema builder used by actual samples and synthetic checks."""
    def identity(value):
        return {'device':value['device'],'inode':value['inode'],
                'file_type':value['file_type'] if 'file_type' in value else value['mode_type']}
    roots_before={name:identity(value) for name,value in root_before.items()}
    roots_after={name:identity(value) for name,value in root_after.items()}
    children_before={name:identity(value) for name,value in child_before.items()}
    children_after={name:identity(value) for name,value in child_after.items()}
    def row_comparison(row,expected,fd,name):
        expected_row={'pid':checker_pid,'fd_raw':str(fd).encode(),'fd_number':fd,'device':expected['device'],
                      'inode':expected['inode'],'file_type':expected['file_type'],'name_decoded':name}
        observed={'pid':row['pid'],'fd_raw':row['fd_raw'],'fd_number':row['fd_number'],'device':row['device'],
                  'inode':row['inode'],'file_type':{b'DIR':stat.S_IFDIR,b'REG':stat.S_IFREG}.get(row['fd_type_raw']),
                  'type_raw':row['fd_type_raw'],'name_decoded':row.get('name_decoded',row['name_raw']),
                  'name_raw':row['name_raw']}
        comparisons={key:observed[key]==value for key,value in expected_row.items()}
        return {'expected':expected_row,'observed':observed,'comparisons':comparisons,'matches':all(comparisons.values())}
    root_row_comparisons=[row_comparison(row,roots_after['pinned_root'],pin_fd,path_bytes['root_canonical_after']) for row in root_pin_rows]
    child_row_comparisons=[row_comparison(row,children_after['descriptor_child'],child_fd,path_bytes['child_canonical_after']) for row in child_rows]
    comparisons={
        'named_root_equals_pinned_before':roots_before['named_root']==roots_before['pinned_root'],
        'canonical_root_equals_named_before':roots_before['canonical_root']==roots_before['named_root'],
        'named_root_equals_pinned_after':roots_after['named_root']==roots_after['pinned_root'],
        'canonical_root_equals_named_after':roots_after['canonical_root']==roots_after['named_root'],
        'named_root_stable':roots_before['named_root']==roots_after['named_root'],
        'pinned_root_stable':roots_before['pinned_root']==roots_after['pinned_root'],
        'canonical_root_stable':roots_before['canonical_root']==roots_after['canonical_root'],
        'named_child_equals_descriptor_before':children_before['named_child']==children_before['descriptor_child'],
        'canonical_child_equals_named_before':children_before['canonical_child']==children_before['named_child'],
        'named_child_equals_descriptor_after':children_after['named_child']==children_after['descriptor_child'],
        'canonical_child_equals_named_after':children_after['canonical_child']==children_after['named_child'],
        'named_child_stable':children_before['named_child']==children_after['named_child'],
        'descriptor_child_stable':children_before['descriptor_child']==children_after['descriptor_child'],
        'canonical_child_stable':children_before['canonical_child']==children_after['canonical_child'],
        'canonical_root_path_stable':path_bytes['root_canonical_before']==path_bytes['root_canonical_after'],
        'canonical_child_path_stable':path_bytes['child_canonical_before']==path_bytes['child_canonical_after'],
        'basename_bytes_exact':basename_bytes==b'child\nline.bin',
        'basename_exactly_one_lf':basename_bytes.count(b'\n')==1,
        'root_pin_row_count_exactly_one':len(root_pin_rows)==1,
        'child_row_count_exactly_one':len(child_rows)==1,
        'root_pin_rows_match':len(root_pin_rows)==1 and all(item['matches'] for item in root_row_comparisons),
        'child_rows_match':len(child_rows)==1 and all(item['matches'] for item in child_row_comparisons),
        'all_rows_accounted_for':len(scanner_rows)==len(root_pin_rows)+len(child_rows)==2,
        'scanner_child_absent':all(row['pid']!=scanner_child.get('pid') for row in scanner_rows),
    }
    return {'schema':'r6a-r45-case8-success-evidence-v1',
            'root':{'before':roots_before,'after':roots_after,'pin_fd':pin_fd},
            'child':{'before':children_before,'after':children_after,'descriptor_fd':child_fd},
            'paths':path_bytes,'basename':{'expected_bytes':b'child\nline.bin','actual_bytes':basename_bytes,
                                         'byte_length':len(basename_bytes),'lf_count':basename_bytes.count(b'\n')},
            'scanner_rows':{'all':scanner_rows,'root_pin':root_pin_rows,'fixture_child':child_rows,'scanner_child':scanner_child},
            'root_pin_row_comparisons':root_row_comparisons,'child_row_comparisons':child_row_comparisons,
            'comparisons':comparisons,'identity_comparisons_pass':all(comparisons.values())}

def _case8_post_scan_evidence(root,child,rf,cf,named_before,fd_before,named_after,fd_after,root_contract,c,pin_matches,candidates):
    with _scope('case8.post_scan.evidence','post-scan identity','retain actual Case-8 post-scan identities and raw rows',paths={'root':root,'child':child}):
        # Attach successful samples before attempting another observation.
        evidence={'named_child_before':object_identity(named_before),'descriptor_child_before':object_identity(fd_before),
                  'named_child_after':object_identity(named_after),'descriptor_child_after':object_identity(fd_after),
                  'root_before':root_contract['before'],'root_after':root_contract['after'],
                  'canonical_root_before_bytes':root_contract['canonical_root_before_bytes'],
                  'canonical_root_after_bytes':root_contract['canonical_root_after_bytes'],
                  'pin_fd':rf,'child_fd':cf,'root_pin_rows':pin_matches,'fixture_child_rows':candidates,
                  'all_scanner_rows':c['rows'],'scanner_child':{'pid':c.get('child_pid'),'ppid':c.get('child_ppid'),'identity':c.get('child_identity')},
                  'basename_bytes':os.fsencode(os.path.basename(child))}
        report['case8_post_scan_contract']=evidence
        canonical_child=_op('case8.post_scan.child.canonicalize','post-scan identity','strict canonical child resolution',lambda:canonical_path(child),paths={'child':child})
        evidence['canonical_child_after_bytes']=canonical_child
        canonical_stat=_stat_op('case8.post_scan.child.canonical_stat','post-scan child identity','os.stat actual resolved child after scan',lambda:os.stat(canonical_child,follow_symlinks=False),{'canonical_child':canonical_child})
        evidence['canonical_child_after']=object_identity(canonical_stat)
        pre=report['case8_pre_scan_contract']
        evidence.update(_case8_success_schema(
            root_before=root_contract['before'],root_after=root_contract['after'],
            child_before={'named_child':object_identity(named_before),'descriptor_child':object_identity(fd_before),
                          'canonical_child':identity_components(pre['child_canonical_identity'])},
            child_after={'named_child':object_identity(named_after),'descriptor_child':object_identity(fd_after),
                         'canonical_child':object_identity(canonical_stat)},
            path_bytes={'root_raw':os.fsencode(root),'child_raw':os.fsencode(child),
                        'root_canonical_before':root_contract['canonical_root_before_bytes'],
                        'root_canonical_after':root_contract['canonical_root_after_bytes'],
                        'child_canonical_before':os.fsencode(pre['child_canonical_path']),
                        'child_canonical_after':canonical_child},basename_bytes=evidence['basename_bytes'],
            pin_fd=rf,child_fd=cf,checker_pid=os.getpid(),scanner_rows=c['rows'],root_pin_rows=pin_matches,
            child_rows=candidates,scanner_child=evidence['scanner_child']))
        return evidence

def write_report():
    with _scope('report.finalize','report generation','finalize complete failure/success evidence',observe=False):
        report['finished_at_unix']=time.time()
        report['result']='PASS' if report['first_failure'] is None else 'FAIL'
        return _publish_pair(report,JSON_OUT,MD_OUT,COMPLETE_OUT,INCOMPLETE_OUT,RUN_ID)

def _synthetic_exercise_boundary(specification,recorder,test_report,injected_original_operation):
    """Exercise actual runner boundaries while prohibiting original OS calls."""
    global OBS,report,_SYNTHETIC_OPERATION_OVERRIDE
    slug,case_id,target,phase,parent_only,generic=specification
    prior_observer=OBS;prior_report=report;prior_override=_SYNTHETIC_OPERATION_OVERRIDE
    OBS=recorder;report=test_report
    root='/synthetic/suite/case_8';child=root+'/child\nline.bin'
    directory_stat=os.stat_result((stat.S_IFDIR|0o700,1001,123,1,501,20,0,0,0,0))
    child_stat=os.stat_result((stat.S_IFREG|0o600,1002,123,1,501,20,1,0,0,0))
    def substitute(operation_id,original_call):
        if operation_id==target:return injected_original_operation()
        if operation_id.endswith('.root.canonicalize'):return os.fsencode(root)
        if operation_id.endswith('.root.named_stat') or operation_id.endswith('.root.pin_fstat') or operation_id.endswith('.root.canonical_stat'):return directory_stat
        if operation_id.endswith('.scan.target.canonicalize'):return root
        if operation_id=='case8.post_scan.child.canonicalize':return os.fsencode(child)
        if operation_id=='case4.helper.stop.poll':return None
        if operation_id in ('case4.helper.stop.write','case4.helper.stop.flush','case4.helper.stop.kill'):return None
        if operation_id=='case4.helper.stop.wait':raise BrokenPipeError(32,'synthetic existing graceful-shutdown fallback')
        raise AssertionError('unplanned original operation prohibited during synthetic boundary exercise: '+operation_id)
    _SYNTHETIC_OPERATION_OVERRIDE=substitute
    try:
        def invoke_boundary():
            if generic:
                recorder.current=recorder.context(target,phase,'unanticipated generic runner fallback',{'path_argument':child})
                recorder.last_snapshot=recorder.snapshot('before')[0]
                return injected_original_operation()
            if slug.startswith('01-'):
                return _op('suite.root.create','parent creation','construct original TemporaryDirectory',injected_original_operation,paths={'parent':'/synthetic/suite'})
            if slug.startswith(('02-','02b-','03-')):
                root_fd=None if slug.startswith('02-') else 91
                child_fd=92 if slug.startswith('03-') else None
                return _case8_operation(target,'synthetic injected original fixture operation',[child],injected_original_operation,
                                        '/synthetic/suite',root,child,root_fd,child_fd)
            if slug.startswith(('04-','06-','07-')):
                return checker_root_snapshot(root,91,'pre_scan' if slug.startswith('04-') else 'post_scan')
            if slug.startswith(('05-','11-')):return scan(root)
            if slug.startswith('08-'):
                identities={'named_root':identity_components(identity_key(directory_stat)),
                            'pinned_root':identity_components(identity_key(directory_stat)),
                            'canonical_root':identity_components(identity_key(directory_stat))}
                contract={'before':identities,'after':identities,'canonical_root_before_bytes':os.fsencode(root),'canonical_root_after_bytes':os.fsencode(root)}
                return _case8_post_scan_evidence(root,child,91,92,child_stat,child_stat,child_stat,child_stat,
                                                 contract,{'rows':[],'child_pid':6301,'child_ppid':os.getpid()},[],[])
            if slug.startswith('09-'):
                class SyntheticHelper:
                    pid=6201
                    returncode=None
                    stdin=None
                    def poll(self):raise AssertionError('synthetic helper poll must be substituted')
                    def kill(self):raise AssertionError('synthetic helper kill must be substituted')
                    def wait(self,timeout=None):raise AssertionError('synthetic helper wait must be substituted')
                return stop(SyntheticHelper())
            raise AssertionError('unknown synthetic boundary specification: '+slug)
        try:
            invoke_boundary()
        except OSError as original:
            if generic:recorder.unexpected(original)
            return test_report['primary_failure'],original
        raise AssertionError('synthetic boundary did not raise original exception: '+slug)
    finally:
        OBS=prior_observer;report=prior_report;_SYNTHETIC_OPERATION_OVERRIDE=prior_override

def _observability_preflight():
    with _scope('runner.observability_preflight','synthetic observability validation','injected failures and pure successful Case-8 schema',observe=False):
        from r6a_r45_observability_validation import run_validation
        guards={'scan_lsof_path':0,'run_lsof_bounded':0,'Popen':0}
        original_scan=scanner.scan_lsof_path
        original_bounded=scanner.run_lsof_bounded
        original_popen=scanner.subprocess.Popen
        def forbidden(name):
            def call(*args,**kwargs):
                guards[name]+=1
                raise AssertionError('real scanner/subprocess dispatch attempted during synthetic validation: '+name)
            return call
        scanner.scan_lsof_path=forbidden('scan_lsof_path')
        scanner.run_lsof_bounded=forbidden('run_lsof_bounded')
        scanner.subprocess.Popen=forbidden('Popen')
        report['observability_validation']={}
        try:
            artifacts=run_validation(run_id=RUN_ID,output_directory=HERE,publish=_publish_pair,
                                     verify=_verify_complete_pair,decode=_decode_value,
                                     freeze_evidence=_freeze_evidence_value,
                                     restore_evidence=_restore_frozen_evidence,
                                     success_schema=_case8_success_schema,runner_info=report['runner'],
                                     scanner_info=report['scanner'],guards=guards,state=report['observability_validation'],
                                     exercise=_synthetic_exercise_boundary,helper_source=_helper_source,
                                     terminal_reporter=_emergency_failure_output,
                                     terminal_state_factory=_new_terminal_state)
        finally:
            scanner.scan_lsof_path=original_scan
            scanner.run_lsof_bounded=original_bounded
            scanner.subprocess.Popen=original_popen
        _op('report.observability_preflight.print','terminal output','print synthetic observability receipt',lambda:print('R46_OBSERVABILITY_PREFLIGHT='+json.dumps(artifacts,sort_keys=True)),observe=False)
        return artifacts

def _path_representation_preflight():
    """Exercise only the proven byte decoder and parser; never dispatch lsof."""
    checks=[]
    original_popen=scanner.subprocess.Popen
    popen_calls=[]
    def forbidden_popen(*args,**kwargs):
        popen_calls.append({'args_repr':repr(args),'kwargs_repr':repr(kwargs)})
        raise AssertionError('live subprocess dispatch attempted during path decoder preflight')
    scanner.subprocess.Popen=forbidden_popen

    def record(name,passed,evidence=None):
        checks.append({'name':name,'result':'PASS' if passed else 'FAIL','evidence':evidence or {}})

    def encode_fixture_name(path):
        encoded=bytearray()
        for value in path:
            if value==0x5c:encoded.extend(b'\\\\')
            elif value==0x0a:encoded.extend(b'\\n')
            elif value==0x09:encoded.extend(b'\\t')
            else:encoded.append(value)
        return bytes(encoded)

    def parser_stream(encoded_name):
        return (b'p123\0R1\0cPython\0\nf3\0tREG\0D0x100000d\0i7\0n'
                +encoded_name+b'\0\n')

    samples=[
        ('ordinary_ascii',b'/tmp/ordinary.bin'),
        ('embedded_lf',b'/tmp/line\nbreak.bin'),
        ('literal_backslash_n',b'/tmp/line\\nbreak.bin'),
        ('literal_backslash',b'/tmp/back\\slash.bin'),
        ('space',b'/tmp/with space.bin'),
        ('tab',b'/tmp/with\ttab.bin'),
    ]
    try:
        encoded_samples=[]
        decoded_samples=[]
        parser_samples_ok=True
        sample_evidence=[]
        for label,filesystem_path in samples:
            encoded=encode_fixture_name(filesystem_path)
            decoded=scanner.decode_lsof_name(encoded)
            rows,stream=scanner.parse_lsof(parser_stream(encoded))
            ok=(decoded==filesystem_path and len(rows)==1 and stream.get('complete') is True
                and rows[0].get('name_raw')==encoded and rows[0].get('name_decoded')==filesystem_path)
            parser_samples_ok=parser_samples_ok and ok
            encoded_samples.append(encoded);decoded_samples.append(decoded)
            sample_evidence.append({'case':label,'filesystem_path_repr':repr(filesystem_path),
                                    'filesystem_path_hex':filesystem_path.hex(),'lsof_name_repr':repr(encoded),
                                    'lsof_name_hex':encoded.hex(),'decoded_repr':repr(decoded),
                                    'decoded_hex':decoded.hex(),'parser_status':rows[0]['parser_status'] if rows else None,
                                    'parser_stream_complete':stream.get('complete')})
        record('parser_and_decoder_round_trip_for_required_names',parser_samples_ok,{'samples':sample_evidence})
        record('distinct_lf_and_literal_backslash_n_have_distinct_spellings_and_paths',
               encoded_samples[1]!=encoded_samples[2] and decoded_samples[1]!=decoded_samples[2],
               {'lf_lsof_name_hex':encoded_samples[1].hex(),'literal_backslash_n_lsof_name_hex':encoded_samples[2].hex(),
                'lf_decoded_hex':decoded_samples[1].hex(),'literal_backslash_n_decoded_hex':decoded_samples[2].hex()})
        record('no_collision_across_required_path_samples',
               len(set(encoded_samples))==len(encoded_samples) and len(set(decoded_samples))==len(decoded_samples),
               {'filesystem_name_count':len(samples),'encoded_name_count':len(set(encoded_samples)),
                'decoded_path_count':len(set(decoded_samples))})

        malformed=[]
        for label,value in [('incomplete_escape',b'/tmp/incomplete\\'),
                            ('unknown_escape',b'/tmp/unknown\\q'),
                            ('unproven_octal_escape',b'/tmp/octal\\012')]:
            try:scanner.decode_lsof_name(value)
            except scanner.CaptureError as error:malformed.append({'case':label,'rejected':True,'error':str(error)})
            else:malformed.append({'case':label,'rejected':False})
        record('malformed_and_unknown_escapes_fail_closed',all(item['rejected'] for item in malformed),{'cases':malformed})
        parser_rejections=[]
        for label,value in [('incomplete_escape',b'/tmp/incomplete\\'),('unknown_escape',b'/tmp/unknown\\q')]:
            try:scanner.parse_lsof(parser_stream(value))
            except scanner.LsofParseError as error:parser_rejections.append({'case':label,'rejected':True,'error':str(error)})
            else:parser_rejections.append({'case':label,'rejected':False})
        record('parser_rejects_incomplete_and_unknown_name_escapes',
               all(item['rejected'] for item in parser_rejections),{'cases':parser_rejections})

        try:scanner.decode_lsof_name(b'/tmp/embedded\0nul')
        except scanner.CaptureError:nul_rejected=True
        else:nul_rejected=False
        record('embedded_nul_rejected',nul_rejected)

        try:scanner.decode_lsof_name(b'x'*(scanner.MAX_LSOF_NAME_BYTES+1))
        except scanner.CaptureError as error:length_rejected='bounded stdout limit' in str(error)
        else:length_rejected=False
        record('oversized_name_rejected_without_truncation',length_rejected,
               {'maximum_bytes':scanner.MAX_LSOF_NAME_BYTES,'tested_bytes':scanner.MAX_LSOF_NAME_BYTES+1})
        record('no_lsof_or_subprocess_invocation',not popen_calls,{'popen_calls':popen_calls})
    finally:
        scanner.subprocess.Popen=original_popen
    return {'schema':'r6a-r50-path-representation-preflight-v1',
            'result':'PASS' if all(item['result']=='PASS' for item in checks) else 'FAIL',
            'live_lsof_invocations':0,'checks':checks}

def _run_disposable_cases(temp):
    roots=_op('suite.case_paths','case-root creation','construct original sixteen case-root paths',lambda:{i:str(Path(temp)/f'case_{i}') for i in range(1,17)},paths={'suite_root':temp})
    for i,p in roots.items():
        OBS.set_case(i,suite_root=temp,case_root=p)
        if i==8:
            _case8_operation('case8.root.mkdir','os.mkdir',[p],lambda:os.mkdir(p),temp,p)
            report['case8_root_identity_anchor']=_op('root.anchor','case-root creation','retain Case-8 creation identity anchor',lambda:report['case8_setup_operations'][-1]['after']['case_root'].get('lstat',{}).get('identity'))
        else:
            _op('root.mkdir','case-root creation','os.mkdir original case root',lambda:os.mkdir(p),paths={'case_root':p})
    OBS.set_case(1,suite_root=temp,case_root=roots[1])
    # 1 root pin only
    fd=opendir(roots[1])
    try:
        with _scope('execution','case setup/scan/result','original case execution and result decoding'):
            c,m,root_contract=scan_with_checker_pin(roots[1],fd); scan_contract=live_scan_contract(c)
            ok=len(c['rows'])==1 and len(m)==1 and root_contract['stable'] and scan_contract['ok']
            observed={'rows':len(c['rows']),'pin_matches':len(m),'root_identity_stable':root_contract['stable'],'scan_contract':scan_contract,'rc':c['returncode'],'complete':c['scan_complete']}
            if not add('1_root_pin_only','exactly one checker root pin row and stable named/pinned/canonical identity in a complete rc0 scan',observed,ok,{'capture':capview(c),'pin_fd':fd,'checker_root_identity':root_contract}): return 1
    finally:_cleanup([lambda:_close_fd(fd,'root_fd')])
    OBS.set_case(2,suite_root=temp,case_root=roots[2])
    # 2 same checker pin + child; preserve raw spellings and compare strict canonical forms.
    child2=_op('child.path_construct','child creation','construct original child path',lambda:str(Path(roots[2])/'child.bin'),on_success=lambda value:OBS.register(child_path=value))
    _op('child.write_bytes','child creation','Path.write_bytes original payload',lambda:Path(child2).write_bytes(b'x'),paths={'child':child2})
    rf=opendir(roots[2])
    cf=_op('child_fd.open','child open','os.open original read-only child holder',lambda:os.open(child2,os.O_RDONLY),paths={'child':child2},on_success=lambda value:OBS.register(child_fd=value,child_fd_live=True))
    try:
        with _scope('execution','case setup/scan/result','original case execution and result decoding'):
            expected_path=path_evidence(child2); expected_canonical=base64.b64decode(expected_path['canonical_path_base64'])
            named_before=_stat_op('child.named_stat_before','pre-scan child identity','os.stat child no-follow',lambda:os.stat(child2,follow_symlinks=False),{'child':child2});fd_before=_stat_op('child.fd_stat_before','pre-scan child identity','os.fstat retained child',lambda:os.fstat(cf),{'child':child2})
            expected_before_same=object_identity(named_before)==object_identity(fd_before)
            c,m,root_contract=scan_with_checker_pin(roots[2],rf); candidates=[r for r in c['rows'] if r['pid']==os.getpid() and r['fd_number']==cf and r['fd_raw']==str(cf).encode()]
            child_paths=[emitted_path_evidence(r) for r in candidates]
            cm=[r for r in candidates if r['fd_type_raw']==lsof_type(fd_before.st_mode) and r['device']==fd_before.st_dev and r['inode']==fd_before.st_ino
                and base64.b64decode(emitted_path_evidence(r)['name_canonical_base64'])==expected_canonical]
            named_after=_stat_op('child.named_stat_after','post-scan child identity','os.stat child no-follow',lambda:os.stat(child2,follow_symlinks=False),{'child':child2});fd_after=_stat_op('child.fd_stat_after','post-scan child identity','os.fstat retained child',lambda:os.fstat(cf),{'child':child2})
            expected_after_same=object_identity(named_after)==object_identity(fd_after)
            identity_stable=object_identity(fd_before)==object_identity(fd_after)==object_identity(named_before)==object_identity(named_after)
            scan_contract=live_scan_contract(c); scan_ok=scan_contract['ok']
            ok=len(c['rows'])==2 and len(m)==1 and len(cm)==1 and len(candidates)==1 and expected_before_same and expected_after_same and identity_stable and root_contract['stable'] and scan_ok
            observed={'rows':len(c['rows']),'pin_matches':len(m),'child_fd_candidates':len(candidates),'child_matches':len(cm),'expected_identity_before':object_identity(fd_before),'expected_identity_after':object_identity(fd_after),'expected_name_matches_fd_before':expected_before_same,'expected_name_matches_fd_after':expected_after_same,'identity_stable':identity_stable,'checker_root_identity_stable':root_contract['stable'],'scan_contract':scan_contract,'rc':c['returncode'],'stderr_bytes':c['stderr_total_bytes'],'complete':c['scan_complete']}
            evidence={'capture':capview(c),'pin_fd':rf,'checker_root_identity':root_contract,'child_fd':cf,'expected_raw_path':expected_path,'expected_object_before':object_identity(named_before),'child_fd_before':object_identity(fd_before),'child_fd_after':object_identity(fd_after),'expected_object_after':object_identity(named_after),'emitted_child_paths':child_paths}
            if not add('2_checker_pin_plus_child_fd','exact pin and child rows; raw/canonical paths, FD, device, inode, type, rc0, empty stderr and complete framing agree',observed,ok,evidence): return 1
    finally:_cleanup([lambda:_close_fd(cf,'child_fd'),lambda:_close_fd(rf,'root_fd')])
    OBS.set_case(3,suite_root=temp,case_root=roots[3])
    # 3 cwd under root
    cwd=_op('cwd.before','cwd setup','os.getcwd original cwd',os.getcwd);rf=opendir(roots[3])
    try:
        with _scope('execution','case setup/scan/result','original case execution and result decoding'):
            cwd_expected=path_evidence(roots[3]);cwd_info=_stat_op('cwd.named_stat','pre-scan cwd identity','os.stat original checker cwd root',lambda:os.stat(roots[3],follow_symlinks=False),{'root':roots[3]})
            scanner_cwd=_op('scanner_cwd.resolve','scanner process isolation','strict suite root realpath for original scanner cwd override',lambda:os.path.realpath(temp,strict=True),paths={'suite_root':temp})
            _op('cwd.enter','cwd setup','os.chdir original Case-3 root',lambda:os.chdir(roots[3]),paths={'root':roots[3]});c,pm,root_contract,cwd_override_matches=scan_case3_with_scanner_cwd(roots[3],rf,scanner_cwd); same=[r for r in c['rows'] if r['pid']==os.getpid()]
            cr=[r for r in same if r['fd_raw']==b'cwd' and r['fd_type_raw']==lsof_type(cwd_info.st_mode) and r['device']==cwd_info.st_dev and r['inode']==cwd_info.st_ino
                and base64.b64decode(emitted_path_evidence(r)['name_canonical_base64'])==base64.b64decode(cwd_expected['canonical_path_base64'])]
            scan_contract=live_scan_contract(c)
            ok=len(c['rows'])==2 and len(same)==2 and len(cr)==1 and len(pm)==1 and cwd_override_matches==1 and root_contract['stable'] and scan_contract['ok']
            observed={'total_rows':len(c['rows']),'same_pid_rows':len(same),'cwd_rows':len(cr),'pin_rows':len(pm),'scanner_cwd':scanner_cwd,'scanner_cwd_override_matches':cwd_override_matches,'root_identity_stable':root_contract['stable'],'scan_contract':scan_contract,'rc':c['returncode'],'complete':c['scan_complete']}
            if not add('3_checker_cwd_under_root','cwd and exact pin rows visible with matching object identities and stable root identity',observed,ok,{'capture':capview(c),'pin_fd':rf,'checker_root_identity':root_contract,'cwd_expected_path':cwd_expected,'cwd_expected_identity':object_identity(cwd_info),'cwd_emitted_path':emitted_path_evidence(cr[0]) if cr else None}): return 1
    finally:_cleanup([lambda:_op('cwd.restore','cleanup','os.chdir original saved cwd',lambda:os.chdir(cwd),paths={'saved_cwd':cwd}),lambda:_close_fd(rf,'root_fd')])
    # 4-6 foreign holders
    for n,mode in ((4,'root'),(5,'child'),(6,'cwd')):
        OBS.set_case(n,suite_root=temp,case_root=roots[n])
        child=_op('child.path_construct','child creation','construct original optional foreign child path',lambda:str(Path(roots[n])/'foreign-child.bin') if mode=='child' else None,on_success=lambda value:OBS.register(child_path=value))
        if child:_op('child.write_bytes','child creation','Path.write_bytes original foreign-child payload',lambda:Path(child).write_bytes(b'x'),paths={'child':child})
        rf=opendir(roots[n]); p=None
        try:
            with _scope('execution','case setup/scan/result','original case execution and result decoding'):
                p,helper_identity=helper(mode,roots[n],child)
                expected_target=child if mode=='child' else roots[n]; expected_path=path_evidence(expected_target)
                named_before=_stat_op('target.named_stat_before','pre-scan target identity','os.stat original foreign target no-follow',lambda:os.stat(expected_target,follow_symlinks=False),{'target':expected_target})
                c,pin_matches,checker_root_contract=scan_with_checker_pin(roots[n],rf);rr=[r for r in c['rows'] if r['pid']==p.pid]
                expected_canonical=base64.b64decode(expected_path['canonical_path_base64'])
                expected_fd=helper_identity['fd_number']; expected_fd_raw=helper_identity['fd_raw']
                helper_after_identity=helper_post_scan_identity(p,expected_fd) if n==4 else None
                named_after=_stat_op('target.named_stat_after','post-scan target identity','os.stat original foreign target no-follow',lambda:os.stat(expected_target,follow_symlinks=False),{'target':expected_target})
                candidates=[r for r in rr if r['fd_raw']==expected_fd_raw and r['fd_number']==expected_fd]
                path_rows=[emitted_path_evidence(r) for r in candidates]
                matches=[r for r in candidates if r['fd_type_raw']==helper_identity['type_raw'] and r['device']==helper_identity['device'] and r['inode']==helper_identity['inode']
                         and base64.b64decode(emitted_path_evidence(r)['name_canonical_base64'])==expected_canonical]
                scan_contract=live_scan_contract(c); scan_ok=scan_contract['ok']
                if n==4:
                    named_root_before_key=identity_key(named_before)
                    pinned_root_before_key=helper_identity_key(helper_identity)
                    named_root_after_key=identity_key(named_after)
                    pinned_root_after_key=helper_identity_key(helper_after_identity)
                    canonical_root_after=_stat_op('root.canonical_stat_after','post-scan canonical-root identity','os.stat original expected canonical root',lambda:os.stat(expected_canonical),{'canonical_root':expected_canonical})
                    canonical_root_after_key=identity_key(canonical_root_after)
                    root_comparisons={
                        'named_root_before_equals_pinned_root_before':named_root_before_key==pinned_root_before_key,
                        'named_root_before_equals_named_root_after':named_root_before_key==named_root_after_key,
                        'pinned_root_before_equals_pinned_root_after':pinned_root_before_key==pinned_root_after_key,
                        'named_root_after_equals_pinned_root_after':named_root_after_key==pinned_root_after_key,
                        'expected_canonical_path_resolves_to_named_root_after':canonical_root_after_key==named_root_after_key,
                    }
                    identity_stable=all(root_comparisons.values())
                    root_identity_contract={
                        'identity_definition':['device','inode','file_type_stat.S_IFMT'],
                        'expected_canonical_path':expected_path,
                        'identities':{
                            'named_root_before':{'source':{'operation':'os.stat','path':expected_path['raw_path'],'follow_symlinks':False},'value':identity_components(named_root_before_key)},
                            'pinned_root_before':{'source':{'operation':'helper os.fstat from READY handshake','pid':p.pid,'fd_number':expected_fd,'fd_raw':helper_identity['fd_raw']},'value':identity_components(pinned_root_before_key)},
                            'named_root_after':{'source':{'operation':'os.stat','path':expected_path['raw_path'],'follow_symlinks':False},'value':identity_components(named_root_after_key)},
                            'pinned_root_after':{'source':{'operation':'helper os.fstat from POST_SCAN_IDENTITY on the same still-open FD','pid':p.pid,'fd_number':helper_after_identity['fd_number'],'fd_raw':helper_identity['fd_raw']},'value':identity_components(pinned_root_after_key)},
                            'expected_canonical_path_after_scan':{'source':{'operation':'os.stat','path':expected_path['canonical_path'],'path_base64':expected_path['canonical_path_base64'],'follow_symlinks':True},'value':identity_components(canonical_root_after_key)},
                        },
                        'helper_identity_before':helper_identity,
                        'helper_identity_after':helper_after_identity,
                        'comparisons':root_comparisons,
                    }
                else:
                    named_before_key=identity_key(named_before)
                    helper_before_key=helper_identity_key(helper_identity)
                    named_after_key=identity_key(named_after)
                    identity_stable=named_before_key==named_after_key==helper_before_key
                    root_identity_contract=None
                ok=len(c['rows'])==2 and len(pin_matches)==1 and len(rr)==1 and len(candidates)==1 and len(matches)==1 and rr[0]['ppid']==os.getpid() and p.poll() is None and scan_ok and identity_stable and checker_root_contract['stable'] and helper_identity['type_raw']==lsof_type(named_before.st_mode)
                observed={'total_rows':len(c['rows']),'checker_pin_rows':len(pin_matches),'foreign_pid':p.pid,'foreign_ppid':rr[0]['ppid'] if rr else None,'foreign_rows':len(rr),'fd_candidates':len(candidates),'identity_matches':len(matches),'helper_identity':helper_identity,'path_identity_stable':identity_stable,'checker_root_identity_stable':checker_root_contract['stable'],'scan_contract':scan_contract,'rc':c['returncode'],'stderr_bytes':c['stderr_total_bytes'],'complete':c['scan_complete']}
                if n==4: observed['root_identity_comparisons']=root_comparisons
                evidence={'capture':capview(c),'pin_fd':rf,'checker_root_identity':checker_root_contract,'expected_path':expected_path,'emitted_paths':path_rows}
                if root_identity_contract is not None: evidence['root_identity_contract']=root_identity_contract
                if not add(f'{n}_foreign_{mode}_holder',f'exact checker pin plus one foreign {mode} row with matching descriptor/object identity',observed,ok,evidence): return 1
        finally:_cleanup([lambda:stop(p) if p is not None else None,lambda:_close_fd(rf,'root_fd')])
    OBS.set_case(7,suite_root=temp,case_root=roots[7])
    # 7 no additional holder: the checker pin alone must produce a conclusive rc0 row.
    rf=opendir(roots[7])
    try:
        with _scope('execution','case setup/scan/result','original case execution and result decoding'):
            c,pin_matches,root_contract=scan_with_checker_pin(roots[7],rf); scan_contract=live_scan_contract(c)
            ok=len(c['rows'])==1 and len(pin_matches)==1 and root_contract['stable'] and scan_contract['ok']
            observed={'rows':len(c['rows']),'pin_matches':len(pin_matches),'root_identity_stable':root_contract['stable'],'scan_contract':scan_contract,'rc':c['returncode'],'complete':c['scan_complete']}
            if not add('7_pin_only_no_additional_holder','exactly the checker pin row; status 0 and complete structured scan',observed,ok,{'capture':capview(c),'pin_fd':rf,'checker_root_identity':root_contract}): return 1
    finally:_cleanup([lambda:_close_fd(rf,'root_fd')])
    OBS.set_case(8,suite_root=temp,case_root=roots[8])
    # 8 newline-containing filename; embedded NL remains part of the NUL-delimited name value.
    rf=None;cf=None
    try:
        with _scope('execution','case setup/scan/result','original case execution and result decoding'):
            child8=_case8_operation('case8.child.path_construct','str(Path(root) / basename)',
                [roots[8],'child\nline.bin'],lambda:str(Path(roots[8])/'child\nline.bin'),temp,roots[8],
                result_kind='child_path')
            _case8_operation('case8.child.write_bytes','Path.write_bytes',[Path(child8),b'x'],
                lambda:Path(child8).write_bytes(b'x'),temp,roots[8],child8)
            rf=_case8_operation('case8.root_pin.open','opendir',[roots[8]],lambda:opendir(roots[8]),
                temp,roots[8],child8,result_kind='root_fd')
            cf=_case8_operation('case8.child_fd.open','os.open',[child8,os.O_RDONLY],
                lambda:os.open(child8,os.O_RDONLY),temp,roots[8],child8,rf,result_kind='child_fd')
            expected_path=_case8_operation('case8.child.path_evidence','path_evidence',[child8],
                lambda:path_evidence(child8),temp,roots[8],child8,rf,cf)
            expected_canonical=base64.b64decode(expected_path['canonical_path_base64'])
            named_before=_case8_operation('case8.child.named_stat_before','os.stat',[child8,False],
                lambda:os.stat(child8,follow_symlinks=False),temp,roots[8],child8,rf,cf)
            fd_before=_case8_operation('case8.child.fd_stat_before','os.fstat',[cf],
                lambda:os.fstat(cf),temp,roots[8],child8,rf,cf)
            _case8_operation('case8.pre_scan.assertions','case8_pre_scan_assertions',[child8],
                lambda snapshot:_case8_pre_scan_assertions(snapshot,child8),temp,roots[8],child8,rf,cf,
                call_with_snapshot=True)
            c,pin_matches,root_contract=scan_with_checker_pin(roots[8],rf); candidates=[r for r in c['rows'] if r['pid']==os.getpid() and r['fd_number']==cf and r['fd_raw']==str(cf).encode()]
            path_rows=[emitted_path_evidence(r) for r in candidates]
            rr=[(r,evidence) for r,evidence in zip(candidates,path_rows)
                if r['fd_type_raw']==lsof_type(fd_before.st_mode) and r['device']==fd_before.st_dev and r['inode']==fd_before.st_ino
                and evidence['name_canonical_bytes']==expected_canonical and evidence['canonical_stat_matches_row_identity']
                and b'\n' in r['name_decoded']]
            named_after=_stat_op('case8.post_scan.child.named_stat','post-scan child identity','os.stat original newline child no-follow',lambda:os.stat(child8,follow_symlinks=False),{'child':child8});fd_after=_stat_op('case8.post_scan.child.fd_fstat','post-scan child identity','os.fstat original newline child descriptor',lambda:os.fstat(cf),{'child':child8})
            post_scan_evidence=_case8_post_scan_evidence(roots[8],child8,rf,cf,named_before,fd_before,named_after,fd_after,root_contract,c,pin_matches,candidates)
            stable=object_identity(named_before)==object_identity(fd_before)==object_identity(fd_after)==object_identity(named_after)
            scan_contract=live_scan_contract(c)
            newline_rows=[r for r in c['rows'] if b'\n' in r.get('name_decoded',b'')]
            expected_lsof_name=bytearray()
            for value in expected_canonical:
                if value==0x5c:expected_lsof_name.extend(b'\\\\')
                elif value==0x0a:expected_lsof_name.extend(b'\\n')
                elif value==0x09:expected_lsof_name.extend(b'\\t')
                else:expected_lsof_name.append(value)
            newline_framing_ok=(len(newline_rows)==1 and newline_rows[0]['name_decoded']==expected_canonical
                                and newline_rows[0]['name_raw']==bytes(expected_lsof_name) and
                                c.get('stream',{}).get('complete') is True and
                                c.get('stream',{}).get('row_count')==len(c.get('rows',[])) and
                                bytes(expected_lsof_name) in c.get('stdout_raw',b''))
            ok=len(c['rows'])==2 and len(pin_matches)==1 and len(candidates)==1 and len(rr)==1 and stable and root_contract['stable'] and scan_contract['ok'] and newline_framing_ok
            observed={'rows':len(c['rows']),'pin_matches':len(pin_matches),'child_fd_candidates':len(candidates),'matches':len(rr),
                      'newline_preserved':bool(rr and b'\n' in rr[0][0]['name_decoded']),'newline_framing_ok':newline_framing_ok,
                      'canonical_stat_matches_row_identity':bool(rr and rr[0][1]['canonical_stat_matches_row_identity']),
                      'identity_stable':stable,'checker_root_identity_stable':root_contract['stable'],'scan_contract':scan_contract,
                      'rc':c['returncode'],'stderr_bytes':c['stderr_total_bytes'],'complete':c['scan_complete']}
            if not add('8_newline_child_filename','checker pin and exact LF-containing child row remain complete and identity-stable',observed,ok,
                       {'capture':capview(c),'pin_fd':rf,'checker_root_identity':root_contract,'child_fd':cf,
                        'expected_path':expected_path,'expected_identity':object_identity(fd_before),
                        'pre_scan_contract':report['case8_pre_scan_contract'],'post_scan_contract':post_scan_evidence,'newline_framing':{'embedded_lf_row_count':len(newline_rows),
                        'embedded_lf_name_lsof_raw':newline_rows[0]['name_raw'] if newline_rows else None,
                        'embedded_lf_name_decoded':newline_rows[0]['name_decoded'] if newline_rows else None,
                        'expected_canonical_name':expected_canonical,'complete_framing':c.get('stream',{}).get('complete'),
                        'parsed_row_count':len(c.get('rows',[])),'expected_lsof_name':bytes(expected_lsof_name),
                        'stdout_contains_exact_name':bytes(expected_lsof_name) in c.get('stdout_raw',b'')},
                        'emitted_paths':path_rows}): return 1
    finally:_cleanup([lambda:_close_fd(cf,'child_fd') if cf is not None else None,lambda:_close_fd(rf,'root_fd') if rf is not None else None])
    with _scope('suite.synthetic_streams.construct','synthetic parser setup','construct original six invalid byte streams'):
        valid=b'p123\0R1\0cPython\0\nf3\0tDIR\0D0x100000d\0i7\0n/tmp/root\0\n'
        invalid=[
          ('9_malformed_synthetic_stream',b'x123\0R1\0cPython\0\nf3\0tDIR\0D0x100000d\0i7\0n/tmp/root\0\n'),
          ('10_truncated_nul_stream',b'p123\0R1\0cPython\0\nf3\0tDIR\0D0x100000d\0i7\0n/tmp/root'),
          ('11_truncated_set_boundary',valid[:-1]),
          ('12_missing_n_field',b'p123\0R1\0cPython\0\nf3\0tDIR\0D0x100000d\0i7\0\n'),
          ('13_wrong_numeric_field',b'p123\0R1\0cPython\0\nf3\0tDIR\0D0x100000d\0ibad\0n/tmp/root\0\n'),
          ('14_unexpected_field_set_order',b'f3\0tDIR\0D0x100000d\0i7\0n/tmp/root\0\n'),
        ]
    for name,raw in invalid:
        OBS.set_case(int(name.split('_',1)[0]),suite_root=temp,case_root=roots[int(name.split('_',1)[0])])
        with _scope('parser.execution','synthetic parser rejection','original invalid-stream parse/result behavior'):
            try:
                rows,stream=_op('parser.reject','synthetic parser rejection','unchanged parser call for invalid stream',lambda:scanner.parse_lsof(raw),expected=(scanner.LsofParseError,));rejected=False;obs={'accepted':True,'rows':len(rows),'stream':stream}
            except scanner.LsofParseError as e:rejected=True;obs={'accepted':False,'error':str(e),'prefix_rows':len(e.rows),'prefix_rows_raw':e.rows,'exception_structured':OBS.record(e,expected=True)}
            ok=rejected
            if not add(name,'strict parser rejects invalid stream',obs,ok,{'input_bytes':len(raw),'input_sha256':hashlib.sha256(raw).hexdigest(),'input_base64':base64.b64encode(raw).decode(),'input_repr':repr(raw)}): return 1
    OBS.set_case(15,suite_root=temp,case_root=roots[15])
    # 15 lsof child is not a false holder.
    rf=opendir(roots[15])
    try:
        with _scope('execution','case setup/scan/result','original case execution and result decoding'):
            c,pin_matches,root_contract=scan_with_checker_pin(roots[15],rf); lr=[r for r in c['rows'] if r['pid']==c['child_pid']]
            scan_contract=live_scan_contract(c)
            ok=not lr and len(c['rows'])==1 and len(pin_matches)==1 and root_contract['stable'] and scan_contract['ok']
            observed={'lsof_child_pid':c['child_pid'],'child_rows':len(lr),'rows':len(c['rows']),'pin_matches':len(pin_matches),'root_identity_stable':root_contract['stable'],'scan_contract':scan_contract,'rc':c['returncode']}
            if not add('15_scanner_child_not_false_holder','lsof child PID absent; exactly the stable checker pin row in complete output',observed,ok,{'capture':capview(c),'pin_fd':rf,'checker_root_identity':root_contract}): return 1
    finally:_cleanup([lambda:_close_fd(rf,'root_fd')])
    OBS.set_case(16,suite_root=temp,case_root=roots[16])
    # 16 root identity stable around scan; retain the historical tuple and
    # bound-before assertions while recording each name/FD observation.
    rf=opendir(roots[16]);root16_path=path_evidence(roots[16]);n0=_stat_op('root.named_stat_before','pre-scan root identity','os.stat original Case-16 root no-follow',lambda:os.stat(roots[16],follow_symlinks=False),{'root':roots[16]});f0=_stat_op('root.pin_fstat_before','pre-scan root identity','os.fstat original Case-16 pin',lambda:os.fstat(rf),{'root':roots[16]})
    with _scope('setup.decode','case setup','original Case-16 before tuple and pin binding'):
        before=(n0.st_dev,n0.st_ino,stat.S_IFMT(n0.st_mode),f0.st_dev,f0.st_ino,stat.S_IFMT(f0.st_mode))
        bound_before=object_identity(n0)==object_identity(f0)
    try:
        with _scope('execution','case setup/scan/result','original case execution and result decoding'):
            c,pin_matches,checker_root_contract=scan_with_checker_pin(roots[16],rf);n1=_stat_op('root.named_stat_after','post-scan root identity','os.stat original Case-16 root no-follow',lambda:os.stat(roots[16],follow_symlinks=False),{'root':roots[16]});f1=_stat_op('root.pin_fstat_after','post-scan root identity','os.fstat original Case-16 pin',lambda:os.fstat(rf),{'root':roots[16]})
            after=(n1.st_dev,n1.st_ino,stat.S_IFMT(n1.st_mode),f1.st_dev,f1.st_ino,stat.S_IFMT(f1.st_mode))
            named_root_before_key=identity_key(n0);pinned_root_before_key=identity_key(f0)
            named_root_after_key=identity_key(n1);pinned_root_after_key=identity_key(f1)
            canonical_root_after=_stat_op('root.canonical_stat_after','post-scan canonical-root identity','os.stat original canonical Case-16 root',lambda:os.stat(base64.b64decode(root16_path['canonical_path_base64'])),{'root':roots[16]})
            canonical_root_after_key=identity_key(canonical_root_after)
            root_comparisons={
                'named_root_before_equals_pinned_root_before':named_root_before_key==pinned_root_before_key,
                'named_root_before_equals_named_root_after':named_root_before_key==named_root_after_key,
                'pinned_root_before_equals_pinned_root_after':pinned_root_before_key==pinned_root_after_key,
                'named_root_after_equals_pinned_root_after':named_root_after_key==pinned_root_after_key,
                'expected_canonical_path_resolves_to_named_root_after':canonical_root_after_key==named_root_after_key,
            }
            root_identity_contract={
                'identity_definition':['device','inode','file_type_stat.S_IFMT'],
                'expected_canonical_path':root16_path,
                'identities':{
                    'named_root_before':{'source':{'operation':'os.stat','path':root16_path['raw_path'],'follow_symlinks':False},'value':identity_components(named_root_before_key)},
                    'pinned_root_before':{'source':{'operation':'os.fstat','fd_number':rf},'value':identity_components(pinned_root_before_key)},
                    'named_root_after':{'source':{'operation':'os.stat','path':root16_path['raw_path'],'follow_symlinks':False},'value':identity_components(named_root_after_key)},
                    'pinned_root_after':{'source':{'operation':'os.fstat','fd_number':rf},'value':identity_components(pinned_root_after_key)},
                    'expected_canonical_path_after_scan':{'source':{'operation':'os.stat','path':root16_path['canonical_path'],'path_base64':root16_path['canonical_path_base64'],'follow_symlinks':True},'value':identity_components(canonical_root_after_key)},
                },
                'comparisons':root_comparisons,
            }
            scan_contract=live_scan_contract(c); scan_ok=scan_contract['ok']
            contract_ok=all(root_comparisons.values())
            ok=before==after and bound_before and contract_ok and len(c['rows'])==1 and len(pin_matches)==1 and checker_root_contract['stable'] and scan_ok
            observed={'before':before,'after':after,'stable':before==after,'bound_before':bound_before,'root_identity_comparisons':root_comparisons,'checker_root_identity_stable':checker_root_contract['stable'],'pin_matches':len(pin_matches),'scan_contract':scan_contract,'rc':c['returncode'],'complete':c['scan_complete']}
            if not add('16_root_identity_stable','name and pin identities are bound and unchanged before/after complete scan',observed,ok,{'capture':capview(c),'pin_fd':rf,'checker_root_identity':checker_root_contract,'root_identity_contract':root_identity_contract}): return 1
    finally:_cleanup([lambda:_close_fd(rf,'root_fd')])
    return 0

def _emergency_failure_output(*,recorder=None,run_id=None,destination_directory=None,
                              terminal_io=None,terminal_state=None):
    """Publish immutable generations through a finite, non-recursive sink state machine.

    ``terminal_io`` is supplied only by the synthetic acceptance harness.  It
    implements stderr_write/flush_stderr and open_emergency/write_emergency/
    fsync_emergency/close_emergency, keeping the production path on the same
    state machine while preventing synthetic tests from touching real sinks.
    """
    recorder=OBS if recorder is None else recorder
    run_id=RUN_ID if run_id is None else run_id
    destination_directory=HERE if destination_directory is None else destination_directory
    state=TERMINAL_STATE if terminal_state is None else terminal_state
    if state is None:
        state=_new_terminal_state()

    def update_report_state():
        recorder.report['terminal_publication']=state

    def records_complete():
        records=recorder.report.get('failure_records',[])
        return bool(records) and all(item.get('evidence_complete') is True and
                                     bool(item.get('full_traceback')) for item in records)

    def record_terminal_failure(exc,operation_id,phase,description,path=None):
        try:
            paths={'emergency_file':path} if path is not None else None
            context=recorder.context(operation_id,phase,description,paths)
            record=recorder.record(exc,tb=exc.__traceback__,context=context)
            update_report_state()
            return record
        except BaseException as evidence_error:
            state.setdefault('reporter_errors',[]).append({
                'operation_id':operation_id,'exception_type':runtime_type(evidence_error),
                'full_traceback':''.join(traceback.format_exception(type(evidence_error),evidence_error,evidence_error.__traceback__))})
            state['evidence_complete']=False
            update_report_state()
            return None

    def generation_snapshot(generation):
        report=recorder.report
        failure_ids=[item.get('failure_id') for item in report.get('failure_records',[])]
        status_snapshot={name:copy.deepcopy(value) for name,value in state['terminal_sink_results'].items()}
        return {'schema':'r6a-r46-terminal-publication-v1',
                'emission_generation':generation,'known_failure_ids':failure_ids,
                'terminal_sink_attempts':copy.deepcopy(state['terminal_sink_attempts']),
                'terminal_sink_results':status_snapshot,
                'evidence_complete':records_complete(),
                'evidence_status':'EVIDENCE_PENDING'}

    def fallback_typed(value,depth=0):
        """Encode only the bounded built-in fallback tree using the R35 tags."""
        if depth>32:
            raise TypeError('terminal fallback evidence nesting limit exceeded')
        if value is None:return {'tag':'null'}
        if type(value) is bool:return {'tag':'bool','value':value}
        if type(value) is str:return {'tag':'str','value':value}
        if type(value) is bytes:
            return {'tag':'bytes','encoding':'base64','data':base64.b64encode(value).decode('ascii'),
                    'length':len(value),'sha256':hashlib.sha256(value).hexdigest()}
        if type(value) is int:return {'tag':'int','value':str(value)}
        if type(value) is float:
            if math.isnan(value):return {'tag':'float-special','value':'nan'}
            if math.isinf(value):return {'tag':'float-special','value':'-inf' if value<0 else 'inf'}
            return {'tag':'float','hex':value.hex()}
        if type(value) in (list,tuple):
            return {'tag':'list' if type(value) is list else 'tuple',
                    'items':[fallback_typed(item,depth+1) for item in value]}
        if type(value) is dict:
            pairs=[(fallback_typed(key,depth+1),fallback_typed(item,depth+1))
                   for key,item in value.items()]
            pairs.sort(key=lambda pair:json.dumps(pair[0],sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii'))
            return {'tag':'dict','entries':[[key,item] for key,item in pairs]}
        raise TypeError('unsupported value in bounded terminal fallback: '+runtime_type(value))

    def fallback_atom(record,key,types):
        value=record.get(key) if type(record) is dict else None
        return value if type(value) in types else None

    def fallback_safe_projection(value,depth=0):
        """Retain prior built-in evidence and mark bounded omissions explicitly."""
        if value is None or type(value) in (str,bytes,bool,int,float):
            return value,True
        if depth>=12:
            return {'status':'omitted_fallback_depth_limit','runtime_type':runtime_type(value)},False
        if type(value) in (list,tuple):
            items=[]
            complete=True
            for index,item in enumerate(value):
                if index>=256:
                    items.append({'status':'omitted_fallback_item_limit','remaining_count':len(value)-index})
                    complete=False
                    break
                projected,item_complete=fallback_safe_projection(item,depth+1)
                items.append(projected)
                complete=complete and item_complete
            return (tuple(items) if type(value) is tuple else items),complete
        if type(value) is dict:
            projected={}
            complete=True
            for index,(key,item) in enumerate(value.items()):
                if index>=256:
                    projected['_fallback_omitted_item_count']=len(value)-index
                    complete=False
                    break
                if type(key) is not str:
                    complete=False
                    continue
                item_projection,item_complete=fallback_safe_projection(item,depth+1)
                projected[key]=item_projection
                complete=complete and item_complete
            return projected,complete
        return {'status':'omitted_unrepresentable_fallback_value','runtime_type':runtime_type(value)},False

    def fallback_failure_record(record):
        """Project a failure to already-captured built-ins; never call observers."""
        text_keys=('schema','failure_id','run_id','operation_id','case_id','phase','description',
                   'exception_type','exception_type_short','args_repr','exception_message',
                   'full_traceback','traceback','failure_operation_id')
        binary_or_text_keys=('filename','filename2')
        result={key:fallback_atom(record,key,(str,)) for key in text_keys}
        result['strerror']=fallback_atom(record,'strerror',(str,bytes))
        result['strerror_type']=fallback_atom(record,'strerror_type',(str,))
        for key in binary_or_text_keys:
            result[key]=fallback_atom(record,key,(str,bytes))
            result[key+'_type']=fallback_atom(record,key+'_type',(str,))
        for key in ('errno',):
            result[key]=fallback_atom(record,key,(int,))
        for key in ('authoritative','expected_exception','handled_by_existing_fallback','evidence_complete'):
            result[key]=fallback_atom(record,key,(bool,))
        result['fallback_record_schema']='r6a-r46-terminal-failure-minimal-v1'
        omissions=[]
        for key in ('timestamp','cwd','args','input_paths','path_types',
                    'relevant_object_identities_before','relevant_object_identities_after_or_failure',
                    'root_pin_identity_if_live','helper_identity_if_live','scanner_child_identity_if_known',
                    'execution_context','evidence_capture_failures'):
            original=record.get(key) if type(record) is dict else None
            projected,complete=fallback_safe_projection(original)
            result[key]=projected
            if not complete:omissions.append(key)
        result['fallback_projection_omissions']=omissions
        if omissions:result['evidence_complete']=False
        return result

    def fallback_terminal_snapshot(generation):
        failure_ids=[]
        for item in recorder.report.get('failure_records',[]):
            if type(item) is dict and type(item.get('failure_id')) is str:
                failure_ids.append(item['failure_id'])
        sink_results={}
        for name,value in state['terminal_sink_results'].items():
            sink_results[name]={key:item for key,item in value.items()
                                if type(key) is str and type(item) in (str,bool,int,float,type(None))}
        attempts=[]
        for attempt in state['terminal_sink_attempts']:
            if type(attempt) is dict:
                attempts.append({key:item for key,item in attempt.items()
                                 if type(key) is str and type(item) in (str,bool,int,float,type(None))})
        return {'schema':'r6a-r46-terminal-publication-v1','emission_generation':generation,
                'known_failure_ids':failure_ids,'terminal_sink_attempts':attempts,
                'terminal_sink_results':sink_results,'evidence_complete':False,
                'evidence_status':'EVIDENCE_DEGRADED_REPORTER_FAILURE'}

    def make_fallback_generation():
        generation=state['emission_generation']+1
        report_failures=recorder.report.get('failure_records',[])
        failures=[fallback_failure_record(item) for item in report_failures if type(item) is dict]
        primary=recorder.report.get('primary_failure')
        safe_primary=next((item for item in failures if type(primary) is dict and
                           item.get('failure_id')==primary.get('failure_id')),None)
        secondary_ids={item.get('failure_id') for item in recorder.report.get('secondary_failures',[])
                       if type(item) is dict and type(item.get('failure_id')) is str}
        secondary=[item for item in failures if item.get('failure_id') in secondary_ids]
        fallback_complete=all(item.get('evidence_complete') is True for item in failures)
        current_context,context_complete=fallback_safe_projection(recorder.current)
        payload={'schema':'r6a-r46-emergency-failure-v1','run_id':run_id,
                 'publication_complete':False,'primary_failure':safe_primary,
                 'secondary_failures':secondary,'failure_records':failures,
                 'current_execution_context':current_context,
                 'fallback_projection_complete':fallback_complete and context_complete,
                 'terminal_publication':fallback_terminal_snapshot(generation)}
        envelope={'evidence_encoding':EVIDENCE_ENCODING,'version':1,'run_id':run_id,
                  'fallback_schema':'r6a-r46-terminal-failure-minimal-v1',
                  'root':fallback_typed(payload)}
        data=(json.dumps(envelope,sort_keys=True,indent=2,ensure_ascii=True,allow_nan=False)+'\n').encode('ascii')
        state['reporter_errors'].append({'operation_id':'report.emergency.serialize',
                                         'message':'normal structured payload failed twice; emitted bounded R35 fallback',
                                         'fallback_generation':generation,
                                         'known_failure_ids':payload['terminal_publication']['known_failure_ids']})
        state['evidence_complete']=False
        state['emission_generation']=generation
        state['generation_history'].append({'emission_generation':generation,
            'known_failure_ids':payload['terminal_publication']['known_failure_ids'],
            'payload_sha256':hashlib.sha256(data).hexdigest(),'payload_size_bytes':len(data),
            'fallback_schema':'r6a-r46-terminal-failure-minimal-v1'})
        update_report_state()
        return data,generation

    def make_generation():
        # Serialization is bounded: a transient first failure is recorded and
        # followed by one fresh snapshot.  Two failures select the safe R35
        # projection so an otherwise available sink is still attempted.
        for serialization_number in (1,2):
            try:
                generation=state['emission_generation']+1
                payload=recorder.emergency_payload(terminal_publication=generation_snapshot(generation))
                envelope={'evidence_encoding':EVIDENCE_ENCODING,'version':1,
                          'run_id':run_id,'root':_normalize_value(payload)}
                data=(json.dumps(envelope,sort_keys=True,indent=2,ensure_ascii=True,allow_nan=False)+'\n').encode('ascii')
            except BaseException as exc:
                operation_id='report.emergency.serialize' if serialization_number==1 else 'report.emergency.secondary_serialize'
                record_terminal_failure(exc,operation_id,'report generation','serialize structured emergency evidence',
                    Path(destination_directory)/f'R6-A-R46-EMERGENCY-FAILURE-{run_id}.json'
                    if destination_directory is not None else None)
                continue
            state['emission_generation']=generation
            state['generation_history'].append({'emission_generation':generation,
                'known_failure_ids':payload['terminal_publication']['known_failure_ids'],
                'payload_sha256':hashlib.sha256(data).hexdigest(),'payload_size_bytes':len(data)})
            update_report_state()
            return data,generation
        try:
            return make_fallback_generation()
        except BaseException as fallback_error:
            state['evidence_status']='EVIDENCE_DEGRADED_REPORTER_FAILURE'
            state['evidence_complete']=False
            state['reporter_errors'].append({'operation_id':'report.emergency.fallback_serialize',
                'exception_type':runtime_type(fallback_error),
                'message':'bounded typed fallback could not be constructed'})
            update_report_state()
            return None,None

    def stderr_write(text):
        return terminal_io.stderr_write(text) if terminal_io is not None else sys.stderr.write(text)

    def stderr_flush():
        return terminal_io.flush_stderr() if terminal_io is not None else sys.stderr.flush()

    def begin_attempt(sink_name,generation):
        sink=state['terminal_sink_results'][sink_name]
        sink['attempt_count']=sink.get('attempt_count',0)+1
        attempt={'sink':sink_name,'emission_generation':generation,'status':'attempting'}
        state['terminal_sink_attempts'].append(attempt)
        update_report_state()
        return attempt

    def emit_stderr(data,generation):
        sink=state['terminal_sink_results']['stderr']
        if sink['status']!='available':
            return False
        attempt=begin_attempt('stderr',generation)
        text='R46_STRUCTURED_FAILURE='+data.decode('ascii')
        try:
            written=stderr_write(text)
            if type(written) is not int or written!=len(text):
                raise OSError(5,'short structured stderr write')
            stderr_flush()
        except BaseException as exc:
            attempt.update({'status':'failed','failure_operation_id':'report.emergency.stderr'})
            sink['status']='failed'
            record_terminal_failure(exc,'report.emergency.stderr','terminal output',
                                    'emit complete structured emergency evidence',
                                    Path(destination_directory)/f'R6-A-R46-EMERGENCY-FAILURE-{run_id}.json'
                                    if destination_directory is not None else None)
            update_report_state()
            return False
        attempt['status']='succeeded'
        sink['latest_generation_received']=generation
        update_report_state()
        return True

    def file_call(name,fd,destination):
        if terminal_io is not None:
            return getattr(terminal_io,name)(fd,destination) if name in ('open_emergency','write_emergency') else getattr(terminal_io,name)(fd)
        if name=='open_emergency':
            return os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        if name=='write_emergency':
            return os.write(fd,destination)
        if name=='fsync_emergency':
            return os.fsync(fd)
        if name=='close_emergency':
            return os.close(fd)
        raise AssertionError('unknown emergency file operation: '+name)

    def emit_file(data,generation):
        sink=state['terminal_sink_results']['emergency_file']
        if sink['status']!='available':
            return False,False
        if destination_directory is None:
            sink['status']='unavailable'
            update_report_state()
            return False,False
        destination=Path(destination_directory)/f'R6-A-R46-EMERGENCY-FAILURE-{run_id}.json'
        attempt=begin_attempt('emergency_file',generation)
        attempt['stages']=[]
        failed=False
        fd=None

        def stage(operation,call,phase,description):
            nonlocal failed
            operation_id='report.emergency.'+operation
            try:
                result=call()
            except BaseException as exc:
                failed=True
                attempt['stages'].append({'operation_id':operation_id,'status':'failed'})
                record_terminal_failure(exc,operation_id,phase,description,destination)
                update_report_state()
                return False,None
            attempt['stages'].append({'operation_id':operation_id,'status':'succeeded'})
            update_report_state()
            return True,result

        opened,fd=stage('open',lambda:file_call('open_emergency',None,destination),
                        'report publication','open designated no-clobber emergency evidence')
        if opened:
            view=memoryview(data)
            wrote=True
            while view:
                ok,count=stage('write',lambda:file_call('write_emergency',fd,view),
                               'report publication','write designated no-clobber emergency evidence')
                if not ok:
                    wrote=False
                    break
                if type(count) is not int or count<=0 or count>len(view):
                    exc=OSError(5,'short emergency evidence write',os.fspath(destination))
                    failed=True
                    attempt['stages'].append({'operation_id':'report.emergency.write','status':'failed'})
                    record_terminal_failure(exc,'report.emergency.write','report publication',
                                            'complete designated no-clobber emergency evidence write',destination)
                    update_report_state()
                    wrote=False
                    break
                view=view[count:]
            if wrote:
                synced,_=stage('fsync',lambda:file_call('fsync_emergency',fd,None),
                               'report publication','fsync designated no-clobber emergency evidence')
                if not synced:
                    failed=True
            closed,_=stage('close',lambda:file_call('close_emergency',fd,None),
                           'cleanup','close designated emergency evidence descriptor')
            if not closed:
                failed=True

        if failed:
            sink['status']='failed'
            attempt['status']='failed'
            attempt['failure_count']=sum(1 for item in attempt['stages'] if item['status']=='failed')
            update_report_state()
            return False,True
        sink['status']='succeeded'
        sink['latest_generation_received']=generation
        attempt['status']='succeeded'
        update_report_state()
        return True,False

    update_report_state()
    data,generation=make_generation()
    if data is None:
        return finalize_terminal_state(state,recorder,terminal_io)

    stderr_succeeded=emit_stderr(data,generation)
    stderr_failed=state['terminal_sink_results']['stderr']['status']=='failed'
    if stderr_failed:
        # The emergency file must receive a generation created after the stderr
        # failure was recorded; generation 1 remains historically unchanged.
        data,generation=make_generation()

    if data is not None:
        file_succeeded,file_failed=emit_file(data,generation)
    else:
        file_succeeded,file_failed=False,False

    if file_failed:
        # Even with no remaining sink, preserve the final known-failure snapshot
        # in memory.  If stderr remains live, deliver that generation once.
        latest_data,latest_generation=make_generation()
        if latest_data is not None and state['terminal_sink_results']['stderr']['status']=='available':
            if not emit_stderr(latest_data,latest_generation):
                # The final stderr failure is known in memory.  Do not retry it.
                make_generation()
    state=finalize_terminal_state(state,recorder,terminal_io)
    return state

def finalize_terminal_state(state,recorder,terminal_io=None):
    results=state['terminal_sink_results']
    received=[item['latest_generation_received'] for item in results.values()
              if item.get('latest_generation_received') is not None]
    state['latest_generation_delivered']=max(received) if received else None
    capture_complete=bool(recorder.report.get('failure_records')) and all(
        item.get('evidence_complete') is True and bool(item.get('full_traceback'))
        for item in recorder.report.get('failure_records',[]))
    latest=state['emission_generation']
    if latest and state['latest_generation_delivered']==latest and capture_complete and not state.get('reporter_errors'):
        status='EVIDENCE_COMPLETE'
    elif results['stderr']['status']=='failed' and results['emergency_file']['status']=='failed':
        status='EVIDENCE_DEGRADED_ALL_SINKS_FAILED'
    elif not capture_complete:
        status='EVIDENCE_DEGRADED_CAPTURE_INCOMPLETE'
    elif not latest or state['latest_generation_delivered']!=latest:
        remaining_sink=any(item.get('status')=='available' for item in results.values())
        status=('EVIDENCE_DEGRADED_REPORTER_FAILURE' if remaining_sink else
                'EVIDENCE_DEGRADED_NO_REMAINING_SINK')
    else:
        status='EVIDENCE_DEGRADED_REPORTER_FAILURE'
    state['evidence_status']=status
    state['evidence_complete']=status=='EVIDENCE_COMPLETE'
    recorder.report['terminal_publication']=state
    if not state['evidence_complete'] and not state.get('raw_traceback_attempted'):
        state['raw_traceback_attempted']=True
        try:
            original=recorder.report.get('primary_failure')
            if original:
                match=next((exc for exc,item in recorder.original_exceptions if item is original),None)
                if match is not None:
                    if terminal_io is not None:
                        terminal_io.raw_traceback(match,match.__traceback__)
                    else:
                        traceback.print_exception(type(match),match,match.__traceback__,file=sys.stderr)
            state['raw_traceback_status']='best_effort_attempted'
        except BaseException:
            state['raw_traceback_status']='failed'
    recorder.report['terminal_publication']=state
    return state

def main():
    path_self_test='--self-test-path-representation' in sys.argv[1:]
    self_test=(path_self_test or '--self-test-reporting' in sys.argv[1:] or '--self-test-observability' in sys.argv[1:])
    successful_self_test=False
    exit_code=1
    try:
        _initialize()
        if path_self_test:
            preflight=_path_representation_preflight()
            report['path_representation_preflight']=preflight
            _op('path.preflight.print','terminal output','print synthetic byte-decoder preflight',
                lambda:print('R50_PATH_REPRESENTATION_PREFLIGHT='+json.dumps(preflight,sort_keys=True)),observe=False)
            successful_self_test=preflight['result']=='PASS'
            return 0 if successful_self_test else 1
        OBS.current.update(case_id='reporting',phase='synthetic reporting preflight',operation_id='runner.preflight',description='existing synthetic reporting preflight',input_paths={})
        with _scope('runner.preflight','synthetic reporting preflight','existing serialization and publication preflight',observe=False):
            _reporting_preflight()
        path_preflight=_path_representation_preflight()
        report['path_representation_preflight']=path_preflight
        if path_preflight['result']!='PASS':
            raise CaptureError('synthetic path representation preflight failed; live scanner suite was not dispatched')
        if '--self-test-observability' in sys.argv[1:]:
            _observability_preflight()
            successful_self_test=True
            return 0
        if '--self-test-reporting' in sys.argv[1:]:
            successful_self_test=True
            return 0
        OBS.current.update(case_id='suite',phase='parent creation',operation_id='suite.root.create',description='create original temporary suite root',input_paths={})
        temp_parent=_op('suite.parent.resolve','parent creation','observe selected default temporary parent',tempfile.gettempdir,
                        on_success=lambda value:OBS.register(suite_parent=value))
        temporary=_op('suite.root.create','parent creation','construct original TemporaryDirectory',lambda:tempfile.TemporaryDirectory(prefix='r40-scanner-regressions-'),
                      paths={'parent':temp_parent},on_success=lambda value:OBS.register(suite_root=value.name))
        temp=_op('suite.root.enter','parent creation','enter original TemporaryDirectory',temporary.__enter__,on_success=lambda value:OBS.register(suite_root=value))
        try:
            exit_code=_run_disposable_cases(temp)
        finally:
            exception_state=sys.exc_info()
            _cleanup([lambda:_op('suite.root.cleanup','cleanup','original TemporaryDirectory.__exit__',lambda:temporary.__exit__(*exception_state),paths={'suite_root':temp})])
    except BaseException as exc:
        OBS.unexpected(exc)
        exit_code=1
    finally:
        if not successful_self_test:
            OBS.current.update(case_id='reporting',phase='report generation',operation_id='report.finalize',description='publish final runner evidence',input_paths={})
            try:
                publication=write_report()
                _op('runner.report.print','terminal output','print publication receipt',lambda:print('R46_REPORT_PAIR='+json.dumps(publication,sort_keys=True)),observe=False)
            except BaseException as exc:
                OBS.unexpected(exc)
                _emergency_failure_output()
                exit_code=2
    if report['first_failure'] is not None:exit_code=1 if exit_code!=2 else 2
    try:
        _op('runner.result.print','terminal output','print JSON-safe diagnostic result',lambda:print(report.get('result','FAIL')),observe=False)
        if report['first_failure']:
            _op('runner.failure.print','terminal output','print typed-safe diagnostic first failure',lambda:print('first_failure='+json.dumps(_markdown_diagnostic(report['first_failure']),sort_keys=True,ensure_ascii=True)),observe=False)
    except BaseException as exc:
        OBS.unexpected(exc)
        _emergency_failure_output()
        return 2
    return exit_code

if __name__=='__main__':
    try:
        status=main()
    except BaseException as exc:
        OBS.unexpected(exc,operation_id='runner.entrypoint',phase='unexpected generic boundary',description='unanticipated entrypoint failure')
        _emergency_failure_output()
        status=2
    raise SystemExit(status)
