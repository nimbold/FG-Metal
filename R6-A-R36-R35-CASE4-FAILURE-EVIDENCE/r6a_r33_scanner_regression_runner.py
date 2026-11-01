#!/usr/bin/env python3
"""Fail-fast R35 disposable-root regressions for the structured lsof scanner."""
import base64, hashlib, importlib.util, json, math, os, select, stat, subprocess, sys, tempfile, time, uuid
from pathlib import Path
from datetime import datetime, timezone

HERE=Path(__file__).resolve().parent
SCANNER_PATH=HERE/'r6a_r33_live_open_capture.py'
RUN_ID=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:12]
REPORT_PREFIX=f'R6-A-R35-STRUCTURED-LSOF-DISPOSABLE-REGRESSIONS-{RUN_ID}'
JSON_OUT=HERE/(REPORT_PREFIX+'.json')
MD_OUT=HERE/(REPORT_PREFIX+'.md')
COMPLETE_OUT=HERE/(REPORT_PREFIX+'-COMPLETE.json')
INCOMPLETE_OUT=HERE/(REPORT_PREFIX+'-INCOMPLETE.json')
EVIDENCE_ENCODING='r6a-r35-typed-json'
spec=importlib.util.spec_from_file_location('r33', SCANNER_PATH)
scanner=importlib.util.module_from_spec(spec); spec.loader.exec_module(scanner)
report={'schema_version':2,'scanner':{'path':str(SCANNER_PATH),'sha256':hashlib.sha256(SCANNER_PATH.read_bytes()).hexdigest(),'argv_field':scanner.LSOF_ARG},
        'runner':{'path':str(Path(__file__).resolve()),'sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        'started_at_unix':time.time(),'cases':[],'cleanup':[],'first_failure':None}

class ReportSerializationError(Exception): pass
class ReportPublicationError(Exception): pass

def _canonical_json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')

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

def _render_markdown(payload,run_id):
    diagnostic=_markdown_diagnostic(payload)
    result=payload.get('result','SYNTHETIC') if type(payload) is dict else 'SYNTHETIC'
    scanner_hash=payload.get('scanner',{}).get('sha256','not-applicable') if type(payload) is dict and type(payload.get('scanner')) is dict else 'not-applicable'
    lines=['# R6-A R35 Disposable Structured lsof Scanner Regressions','',f'Run ID: `{run_id}`',f'Result: `{result}`',f'Scanner SHA-256: `{scanner_hash}`','',
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
        raise ReportSerializationError(f'report serialization/validation failed: {type(e).__name__}: {e}') from e
    manifest={'schema':'r6a-r35-report-pair-v1','run_id':run_id,'status':'COMPLETE',
              'json':{'path':json_path.name,'size_bytes':len(json_bytes),'sha256':hashlib.sha256(json_bytes).hexdigest()},
              'markdown':{'path':markdown_path.name,'size_bytes':len(markdown_bytes),'sha256':hashlib.sha256(markdown_bytes).hexdigest()}}
    manifest_bytes=(json.dumps(manifest,indent=2,sort_keys=True,ensure_ascii=True,allow_nan=False)+'\n').encode('ascii')
    if json.loads(manifest_bytes.decode('ascii'))!=manifest: raise ReportSerializationError('completion manifest failed JSON parse-back')
    return {'json':json_bytes,'markdown':markdown_bytes,'manifest':manifest_bytes,'manifest_object':manifest}

def _file_signature(path):
    st=os.lstat(path)
    if not stat.S_ISREG(st.st_mode): raise ReportPublicationError(f'publication path is not a regular file: {path}')
    return (st.st_dev,st.st_ino)

def _fsync_directory(path):
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try: os.fsync(fd)
    finally: os.close(fd)

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
    finally: os.close(fd)
    actual=Path(path).read_bytes()
    if len(actual)!=len(data) or hashlib.sha256(actual).digest()!=hashlib.sha256(data).digest():
        raise OSError(f'staged artifact verification failed: {path}')

def _verify_complete_pair(json_path,markdown_path,complete_path,incomplete_path):
    if Path(incomplete_path).exists(): raise ValueError('incomplete marker exists')
    manifest=json.loads(Path(complete_path).read_text(encoding='ascii'))
    if (type(manifest) is not dict or manifest.get('schema')!='r6a-r35-report-pair-v1' or
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

def _create_incomplete_marker(path,run_id,phase,remaining):
    data=(json.dumps({'schema':'r6a-r35-report-pair-incomplete-v1','run_id':run_id,'phase':phase,
                      'remaining_artifacts':[str(p) for p in remaining]},indent=2,sort_keys=True)+'\n').encode('ascii')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        view=memoryview(data)
        while view:
            count=os.write(fd,view)
            if count<=0: raise OSError('short write while creating incomplete marker')
            view=view[count:]
        os.fsync(fd)
    finally: os.close(fd)
    _fsync_directory(Path(path).parent)

def _trigger_fault(fault_inject,phase):
    if fault_inject is not None: fault_inject(phase)

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
        cleanup_errors=[]
        for path,sig in list(published.items()):
            try:
                if _file_signature(path)==sig: os.unlink(path)
                else: cleanup_errors.append(f'ownership changed for final path {path}')
            except FileNotFoundError: pass
            except Exception as cleanup_error: cleanup_errors.append(f'{path}: {type(cleanup_error).__name__}: {cleanup_error}')
        for path,sig in list(stage_sigs.items()):
            try:
                if Path(path).exists() and _file_signature(path)==sig: os.unlink(path)
            except Exception as cleanup_error: cleanup_errors.append(f'{path}: {type(cleanup_error).__name__}: {cleanup_error}')
        try: _fsync_directory(json_path.parent)
        except Exception as sync_error: cleanup_errors.append(f'cleanup directory fsync failed: {type(sync_error).__name__}: {sync_error}')
        if cleanup_errors:
            try: _create_incomplete_marker(incomplete_path,run_id,phase,cleanup_errors)
            except Exception as marker_error: cleanup_errors.append(f'incomplete marker creation failed: {type(marker_error).__name__}: {marker_error}')
            try: _fsync_directory(json_path.parent)
            except Exception as sync_error: cleanup_errors.append(f'incomplete marker directory fsync failed: {type(sync_error).__name__}: {sync_error}')
        detail=f'{type(e).__name__}: {e}'
        if cleanup_errors: detail+='; cleanup='+' | '.join(cleanup_errors)
        raise ReportPublicationError(f'report publication failed during {phase}: {detail}') from e

def _reporting_preflight(run_id=RUN_ID,output_directory=HERE):
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
    with tempfile.TemporaryDirectory(prefix='r35-serialization-failure-') as td:
        base=Path(td); j=base/'bad.json'; m=base/'bad.md'; c=base/'bad-complete.json'; inc=base/'bad-incomplete.json'
        try: _publish_pair({'unsupported':object()},j,m,c,inc,run_id)
        except ReportSerializationError: serialization_rejected=True
        except Exception: serialization_rejected=True
        else: serialization_rejected=False
        passed('serialization_failure_creates_no_final_artifacts',serialization_rejected and not any(p.exists() for p in (j,m,c,inc)))
    def inject_publish_failure(phase):
        if phase=='publish_markdown': raise OSError('synthetic second-artifact publication failure')
    with tempfile.TemporaryDirectory(prefix='r35-publication-failure-') as td:
        base=Path(td); j=base/'bad.json'; m=base/'bad.md'; c=base/'bad-complete.json'; inc=base/'bad-incomplete.json'
        try: _publish_pair(synthetic,j,m,c,inc,run_id,fault_inject=inject_publish_failure)
        except ReportPublicationError as e: publication_error='publish_markdown' in str(e) and 'synthetic second-artifact' in str(e)
        else: publication_error=False
        passed('publication_failure_explicit_and_fail_closed',publication_error and not c.exists() and not j.exists() and not m.exists())
    # A successful end-to-end synthetic publication provides durable, hash-addressed preflight evidence.
    preflight_payload={'schema_version':1,'result':'SYNTHETIC_PREFLIGHT_PASS','run_id':run_id,'serialization_checks':checks,
                       'binary_roundtrip':synthetic['nested'],'scanner_cases_executed':False}
    prefix=f'R6-A-R35-SYNTHETIC-REPORT-PREFLIGHT-{run_id}'
    base=Path(output_directory)
    j=base/(prefix+'.json'); m=base/(prefix+'.md'); c=base/(prefix+'-COMPLETE.json'); inc=base/(prefix+'-INCOMPLETE.json')
    artifacts=_publish_pair(preflight_payload,j,m,c,inc,run_id)
    verified=_verify_complete_pair(j,m,c,inc)
    passed('synthetic_complete_report_artifacts_nonempty_and_hash_recorded',
           artifacts['json_size_bytes']>0 and artifacts['markdown_size_bytes']>0 and
           len(verified['json']['sha256'])==64 and len(verified['markdown']['sha256'])==64)
    artifacts['checks']=checks
    print('R35_REPORT_PREFLIGHT='+json.dumps(artifacts,sort_keys=True))
    return artifacts

def capview(c):
    raw=c.get('stdout_raw',b''); err=c.get('stderr_raw',b'')
    return {'argv':c.get('argv'),'child_pid':c.get('child_pid'),'child_ppid':c.get('child_ppid'),'child_identity':c.get('child_identity'),'child_identity_error':c.get('child_identity_error'),'returncode':c.get('returncode'),'stdout_bytes':c.get('stdout_total_bytes'),'stdout_captured_bytes':c.get('stdout_bytes_captured'),'stdout_sha256':c.get('stdout_sha256'),'stdout_captured_sha256':c.get('stdout_captured_sha256'),'stdout_base64':c.get('stdout_base64'),'stdout_repr':repr(raw),'stderr_bytes':c.get('stderr_total_bytes'),'stderr_sha256':c.get('stderr_sha256'),'stderr_base64':c.get('stderr_base64'),'stderr_repr':repr(err),'timed_out':c.get('timed_out'),'stdout_capped':c.get('stdout_capped'),'stderr_capped':c.get('stderr_capped'),'scan_complete':c.get('scan_complete'),'stream':c.get('stream'),'rows':[{'pid':r.get('pid'),'ppid':r.get('ppid'),'fd_raw':repr(r.get('fd_raw')),'fd_number':r.get('fd_number'),'type_raw':repr(r.get('fd_type_raw')),'device':r.get('device'),'inode':r.get('inode'),'name_raw':repr(r.get('name_raw')),'name_base64':base64.b64encode(r.get('name_raw',b'')).decode(),'parser_status':r.get('parser_status')} for r in c.get('rows',[])]}

def add(name,expected,observed,ok,evidence=None):
    x={'case':name,'expected':expected,'observed':observed,'result':'PASS' if ok else 'FAIL'}
    if evidence is not None: x['evidence']=evidence
    report['cases'].append(x)
    if not ok and report['first_failure'] is None: report['first_failure']={'case':name,'expected':expected,'observed':observed}
    return ok

def scan(root): return scanner.scan_lsof_path(os.path.realpath(root,strict=True))
def opendir(path): return os.open(path,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))

def canonical_path(raw_path):
    raw=os.fsencode(raw_path) if isinstance(raw_path,str) else raw_path
    if not isinstance(raw,bytes) or not os.path.isabs(raw): raise ValueError('path is not absolute')
    canonical=os.path.realpath(raw,strict=True)
    if not os.path.isabs(canonical): raise ValueError('canonical path is not absolute')
    return canonical

def lsof_type(mode):
    if stat.S_ISDIR(mode): return b'DIR'
    if stat.S_ISREG(mode): return b'REG'
    if stat.S_ISLNK(mode): return b'LINK'
    if stat.S_ISCHR(mode): return b'CHR'
    if stat.S_ISBLK(mode): return b'BLK'
    if stat.S_ISFIFO(mode): return b'FIFO'
    if stat.S_ISSOCK(mode): return b'unix'
    return b'UNKNOWN'

def path_evidence(raw_path):
    raw=os.fsencode(raw_path) if isinstance(raw_path,str) else raw_path
    canonical=canonical_path(raw)
    return {'raw_path':os.fsdecode(raw),'raw_path_repr':repr(raw),'raw_path_base64':base64.b64encode(raw).decode(),
            'canonical_path':os.fsdecode(canonical),'canonical_path_repr':repr(canonical),
            'canonical_path_base64':base64.b64encode(canonical).decode(),'canonicalization_status':'RESOLVED'}

def object_identity(info):
    return {'device':info.st_dev,'inode':info.st_ino,'type_raw':lsof_type(info.st_mode).decode(),
            'mode_type':stat.S_IFMT(info.st_mode)}

def emitted_path_evidence(row):
    raw=row['name_raw']; canonical=canonical_path(raw)
    return {'name_raw_repr':repr(raw),'name_raw_base64':base64.b64encode(raw).decode(),
            'name_canonical':os.fsdecode(canonical),'name_canonical_repr':repr(canonical),
            'name_canonical_base64':base64.b64encode(canonical).decode()}

def pinrows(c,fd,root):
    expected=canonical_path(root); descriptor=os.fstat(fd); named=os.stat(root,follow_symlinks=False)
    expected_identity=object_identity(descriptor)
    if expected_identity!=object_identity(named): return []
    return [r for r in c['rows'] if r['pid']==os.getpid() and r['fd_number']==fd and r['fd_raw']==str(fd).encode()
            and r['fd_type_raw']==b'DIR' and r['device']==descriptor.st_dev and r['inode']==descriptor.st_ino
            and canonical_path(r['name_raw'])==expected]
def runscan_case(name,root,check):
    try:
        c=scan(root); ok,obs=check(c)
        return add(name,obs['expected'],obs['observed'],ok,{'capture':capview(c),**obs.get('extra',{})})
    except Exception as e:
        return add(name,'scanner operation completes under stated contract',f'{type(e).__name__}: {e}',False)

def helper(mode,root,child=None):
    code=("import os,sys,stat; m=sys.argv[1]; r=sys.argv[2]; c=sys.argv[3]; "
          "fd=os.open(r,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)) if m=='root' else (os.open(c,os.O_RDONLY) if m=='child' else None); "
          "st=os.fstat(fd) if fd is not None else os.stat(r,follow_symlinks=False); "
          "t='DIR' if stat.S_ISDIR(st.st_mode) else ('REG' if stat.S_ISREG(st.st_mode) else 'UNKNOWN'); "
          "print('READY',fd if fd is not None else 'cwd',st.st_dev,st.st_ino,t,flush=True); "
          "sys.stdin.readline(); os.close(fd) if fd is not None else None")
    p=subprocess.Popen([sys.executable,'-c',code,mode,root,child or ''],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=root if mode=='cwd' else None)
    ready,_,_=select.select([p.stdout],[],[],5)
    try:
        if not ready: raise RuntimeError('foreign helper readiness failed')
        fields=p.stdout.readline().split()
        if len(fields)!=5 or fields[0]!='READY': raise RuntimeError('foreign helper identity handshake failed')
        descriptor=None if fields[1]=='cwd' else int(fields[1])
        identity={'fd_raw':b'cwd' if descriptor is None else str(descriptor).encode(),
                  'fd_number':descriptor,'device':int(fields[2]),'inode':int(fields[3]),'type_raw':fields[4].encode()}
        return p,identity
    except Exception:
        if p.poll() is None: p.kill()
        p.wait(timeout=5)
        raise
def stop(p):
    if p.poll() is None:
        try: p.stdin.write('\n');p.stdin.flush();p.wait(timeout=5)
        except Exception: p.kill();p.wait(timeout=5)
    report['cleanup'].append({'pid':p.pid,'returncode':p.returncode})

def write_report():
    report['finished_at_unix']=time.time()
    report['result']='PASS' if report['first_failure'] is None else 'FAIL'
    return _publish_pair(report,JSON_OUT,MD_OUT,COMPLETE_OUT,INCOMPLETE_OUT,RUN_ID)

def main():
    if '--self-test-reporting' in sys.argv[1:]:
        try: _reporting_preflight()
        except Exception as e:
            print(f'report_preflight_error={type(e).__name__}: {e}',file=sys.stderr)
            return 2
        return 0
    try:
        _reporting_preflight()
    except Exception as e:
        print(f'report_preflight_error={type(e).__name__}: {e}',file=sys.stderr)
        return 2
    try:
        with tempfile.TemporaryDirectory(prefix='r35-scanner-regressions-') as temp:
            roots={i:str(Path(temp)/f'case_{i}') for i in range(1,17)}
            for p in roots.values(): os.mkdir(p)
            # 1 root pin only
            fd=opendir(roots[1])
            try:
                c=scan(roots[1]); m=pinrows(c,fd,roots[1]); ok=len(c['rows'])==1 and len(m)==1 and c['returncode']==0 and c['stderr_total_bytes']==0 and c['scan_complete'] is True
                if not add('1_root_pin_only','exactly one pin row and complete rc0 scan',{'rows':len(c['rows']),'pin_matches':len(m),'rc':c['returncode'],'complete':c['scan_complete']},ok,{'capture':capview(c),'pin_fd':fd}): return 1
            finally: os.close(fd)
            # 2 same checker pin + child; preserve raw spellings and compare strict canonical forms.
            child2=str(Path(roots[2])/'child.bin');Path(child2).write_bytes(b'x');rf=opendir(roots[2]);cf=os.open(child2,os.O_RDONLY)
            try:
                expected_path=path_evidence(child2); expected_canonical=base64.b64decode(expected_path['canonical_path_base64'])
                named_before=os.stat(child2,follow_symlinks=False); fd_before=os.fstat(cf)
                expected_before_same=object_identity(named_before)==object_identity(fd_before)
                c=scan(roots[2]);m=pinrows(c,rf,roots[2]); candidates=[r for r in c['rows'] if r['pid']==os.getpid() and r['fd_number']==cf and r['fd_raw']==str(cf).encode()]
                child_paths=[emitted_path_evidence(r) for r in candidates]
                cm=[r for r in candidates if r['fd_type_raw']==lsof_type(fd_before.st_mode) and r['device']==fd_before.st_dev and r['inode']==fd_before.st_ino
                    and base64.b64decode(emitted_path_evidence(r)['name_canonical_base64'])==expected_canonical]
                named_after=os.stat(child2,follow_symlinks=False); fd_after=os.fstat(cf)
                expected_after_same=object_identity(named_after)==object_identity(fd_after)
                identity_stable=object_identity(fd_before)==object_identity(fd_after)==object_identity(named_before)==object_identity(named_after)
                scan_ok=c['returncode']==0 and c['stderr_total_bytes']==0 and c['scan_complete'] is True
                ok=len(c['rows'])==2 and len(m)==1 and len(cm)==1 and len(candidates)==1 and expected_before_same and expected_after_same and identity_stable and scan_ok
                observed={'rows':len(c['rows']),'pin_matches':len(m),'child_fd_candidates':len(candidates),'child_matches':len(cm),'expected_identity_before':object_identity(fd_before),'expected_identity_after':object_identity(fd_after),'expected_name_matches_fd_before':expected_before_same,'expected_name_matches_fd_after':expected_after_same,'identity_stable':identity_stable,'rc':c['returncode'],'stderr_bytes':c['stderr_total_bytes'],'complete':c['scan_complete']}
                evidence={'capture':capview(c),'pin_fd':rf,'child_fd':cf,'expected_raw_path':expected_path,'expected_object_before':object_identity(named_before),'child_fd_before':object_identity(fd_before),'child_fd_after':object_identity(fd_after),'expected_object_after':object_identity(named_after),'emitted_child_paths':child_paths}
                if not add('2_checker_pin_plus_child_fd','exact pin and child rows; raw/canonical paths, FD, device, inode, type, rc0, empty stderr and complete framing agree',observed,ok,evidence): return 1
            finally: os.close(cf);os.close(rf)
            # 3 cwd under root
            cwd=os.getcwd();rf=opendir(roots[3])
            try:
                cwd_expected=path_evidence(roots[3]); cwd_info=os.stat(roots[3],follow_symlinks=False)
                os.chdir(roots[3]);c=scan(roots[3]); same=[r for r in c['rows'] if r['pid']==os.getpid()]
                cr=[r for r in same if r['fd_raw']==b'cwd' and r['fd_type_raw']==lsof_type(cwd_info.st_mode) and r['device']==cwd_info.st_dev and r['inode']==cwd_info.st_ino
                    and base64.b64decode(emitted_path_evidence(r)['name_canonical_base64'])==base64.b64decode(cwd_expected['canonical_path_base64'])]
                pm=pinrows(c,rf,roots[3]); scan_ok=c['returncode']==0 and c['stderr_total_bytes']==0 and c['scan_complete'] is True
                ok=len(same)==2 and len(cr)==1 and len(pm)==1 and scan_ok
                if not add('3_checker_cwd_under_root','cwd and exact pin rows visible with matching object identities',{'same_pid_rows':len(same),'cwd_rows':len(cr),'pin_rows':len(pm),'rc':c['returncode'],'complete':c['scan_complete']},ok,{'capture':capview(c),'pin_fd':rf,'cwd_expected_path':cwd_expected,'cwd_expected_identity':object_identity(cwd_info),'cwd_emitted_path':emitted_path_evidence(cr[0]) if cr else None}): return 1
            finally: os.chdir(cwd);os.close(rf)
            # 4-6 foreign holders
            for n,mode in ((4,'root'),(5,'child'),(6,'cwd')):
                child=str(Path(roots[n])/'foreign-child.bin') if mode=='child' else None
                if child: Path(child).write_bytes(b'x')
                p,helper_identity=helper(mode,roots[n],child)
                try:
                    expected_target=child if mode=='child' else roots[n]; expected_path=path_evidence(expected_target)
                    named_before=os.stat(expected_target,follow_symlinks=False)
                    c=scan(roots[n]);rr=[r for r in c['rows'] if r['pid']==p.pid]
                    expected_canonical=base64.b64decode(expected_path['canonical_path_base64'])
                    expected_fd=helper_identity['fd_number']; expected_fd_raw=helper_identity['fd_raw']
                    named_after=os.stat(expected_target,follow_symlinks=False)
                    candidates=[r for r in rr if r['fd_raw']==expected_fd_raw and r['fd_number']==expected_fd]
                    path_rows=[emitted_path_evidence(r) for r in candidates]
                    matches=[r for r in candidates if r['fd_type_raw']==helper_identity['type_raw'] and r['device']==helper_identity['device'] and r['inode']==helper_identity['inode']
                             and base64.b64decode(emitted_path_evidence(r)['name_canonical_base64'])==expected_canonical]
                    scan_ok=c['returncode']==0 and c['stderr_total_bytes']==0 and c['scan_complete'] is True
                    helper_object={'device':helper_identity['device'],'inode':helper_identity['inode'],'type_raw':helper_identity['type_raw'],'mode_type':stat.S_IFMT(named_before.st_mode)}
                    identity_stable=object_identity(named_before)==object_identity(named_after)==helper_object
                    ok=len(rr)==1 and len(candidates)==1 and len(matches)==1 and rr[0]['ppid']==os.getpid() and p.poll() is None and scan_ok and identity_stable and helper_identity['type_raw']==lsof_type(named_before.st_mode)
                    observed={'foreign_pid':p.pid,'foreign_ppid':rr[0]['ppid'] if rr else None,'rows':len(rr),'fd_candidates':len(candidates),'identity_matches':len(matches),'helper_identity':helper_identity,'path_identity_stable':identity_stable,'rc':c['returncode'],'stderr_bytes':c['stderr_total_bytes'],'complete':c['scan_complete']}
                    if not add(f'{n}_foreign_{mode}_holder',f'foreign {mode} row visible with exact descriptor/object identity',observed,ok,{'capture':capview(c),'expected_path':expected_path,'emitted_paths':path_rows}): return 1
                finally: stop(p)
            # 7 no holders: row set empty, but lsof's rc1 must still block absence conclusion.
            c=scan(roots[7]);ok=(not c['rows'] and c['stream'].get('complete') is True and c['returncode']==1
                                 and c['stderr_total_bytes']==0 and not c.get('stdout_capped') and not c.get('stderr_capped')
                                 and not c.get('timed_out') and 'child_identity_error' not in c and c['scan_complete'] is False)
            if not add('7_no_holders','zero rows; rc1 remains fail-closed under the exact capture contract',{'rows':len(c['rows']),'stream_complete':c['stream'].get('complete'),'rc':c['returncode'],'stderr_bytes':c['stderr_total_bytes'],'scan_complete':c['scan_complete']},ok,{'capture':capview(c)}): return 1
            # 8 newline-containing filename; embedded NL remains part of the NUL-delimited name value.
            child8=str(Path(roots[8])/'child\nline.bin');Path(child8).write_bytes(b'x');rf=opendir(roots[8]);cf=os.open(child8,os.O_RDONLY)
            try:
                expected_path=path_evidence(child8); expected_canonical=base64.b64decode(expected_path['canonical_path_base64'])
                named_before=os.stat(child8,follow_symlinks=False); fd_before=os.fstat(cf)
                c=scan(roots[8]); candidates=[r for r in c['rows'] if r['pid']==os.getpid() and r['fd_number']==cf and r['fd_raw']==str(cf).encode()]
                path_rows=[emitted_path_evidence(r) for r in candidates]
                rr=[r for r in candidates if r['fd_type_raw']==lsof_type(fd_before.st_mode) and r['device']==fd_before.st_dev and r['inode']==fd_before.st_ino
                    and base64.b64decode(emitted_path_evidence(r)['name_canonical_base64'])==expected_canonical and b'\n' in r['name_raw']]
                named_after=os.stat(child8,follow_symlinks=False); fd_after=os.fstat(cf)
                stable=object_identity(named_before)==object_identity(fd_before)==object_identity(fd_after)==object_identity(named_after)
                scan_ok=c['returncode']==0 and c['stderr_total_bytes']==0 and c['scan_complete'] is True
                ok=len(c['rows'])==2 and len(rr)==1 and stable and scan_ok
                observed={'rows':len(c['rows']),'matches':len(rr),'newline_preserved':bool(rr and b'\n' in rr[0]['name_raw']),'identity_stable':stable,'rc':c['returncode'],'stderr_bytes':c['stderr_total_bytes'],'complete':c['scan_complete']}
                if not add('8_newline_child_filename','LF retained inside n field payload with exact FD/object identity and canonical path',observed,ok,{'capture':capview(c),'pin_fd':rf,'child_fd':cf,'expected_path':expected_path,'expected_identity':object_identity(fd_before),'emitted_paths':path_rows}): return 1
            finally: os.close(cf);os.close(rf)
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
                try:
                    rows,stream=scanner.parse_lsof(raw); rejected=False;obs={'accepted':True,'rows':len(rows),'stream':stream}
                except scanner.LsofParseError as e: rejected=True;obs={'accepted':False,'error':str(e),'prefix_rows':len(e.rows)}
                ok=rejected
                if not add(name,'strict parser rejects invalid stream',obs,ok,{'input_bytes':len(raw),'input_sha256':hashlib.sha256(raw).hexdigest(),'input_base64':base64.b64encode(raw).decode(),'input_repr':repr(raw)}): return 1
            # Status gate: empty valid grammar cannot prove absence unless rc0 + empty stderr.
            rows,stream=scanner.parse_lsof(valid)
            if not add('14b_status_gate','complete scan requires rc0 and empty stderr',{'valid_fixture_rows':len(rows),'fixture_complete':stream['complete'],'scan_lsof_path_gate':'rc==0 and stderr_bytes==0'},stream['complete'] and len(rows)==1,{'fixture_sha256':hashlib.sha256(valid).hexdigest()}): return 1
            # 15 lsof child is not a false holder.
            rf=opendir(roots[15])
            try:
                c=scan(roots[15]); lr=[r for r in c['rows'] if r['pid']==c['child_pid']]
                ok=not lr and c['returncode']==0 and c['scan_complete'] is True
                if not add('15_scanner_child_not_false_holder','lsof child PID absent from rows',{'lsof_child_pid':c['child_pid'],'child_rows':len(lr),'rc':c['returncode']},ok,{'capture':capview(c),'pin_fd':rf}): return 1
            finally: os.close(rf)
            # 16 root identity stable around scan.
            rf=opendir(roots[16]); n0=os.stat(roots[16],follow_symlinks=False); f0=os.fstat(rf)
            before=(n0.st_dev,n0.st_ino,stat.S_IFMT(n0.st_mode),f0.st_dev,f0.st_ino,stat.S_IFMT(f0.st_mode))
            bound_before=object_identity(n0)==object_identity(f0)
            try:
                c=scan(roots[16]);n1=os.stat(roots[16],follow_symlinks=False);f1=os.fstat(rf)
                after=(n1.st_dev,n1.st_ino,stat.S_IFMT(n1.st_mode),f1.st_dev,f1.st_ino,stat.S_IFMT(f1.st_mode))
                pin_matches=pinrows(c,rf,roots[16]); scan_ok=c['returncode']==0 and c['stderr_total_bytes']==0 and c['scan_complete'] is True
                ok=before==after and bound_before and len(c['rows'])==1 and len(pin_matches)==1 and scan_ok
                if not add('16_root_identity_stable','name and pin identities are bound and unchanged before/after scan',{'before':before,'after':after,'stable':before==after,'bound_before':bound_before,'pin_matches':len(pin_matches),'rc':c['returncode'],'complete':c['scan_complete']},ok,{'capture':capview(c),'pin_fd':rf}): return 1
            finally: os.close(rf)
    except Exception as e:
        if report['first_failure'] is None: report['first_failure']={'case':'runner','expected':'safe regression completion','observed':f'{type(e).__name__}: {e}'}
    finally:
        try:
            publication=write_report()
            print('R35_REPORT_PAIR='+json.dumps(publication,sort_keys=True))
        except Exception as e:
            print(f'report_write_error={type(e).__name__}: {e}',file=sys.stderr)
            raise SystemExit(2)
    print(report['result'])
    if report['first_failure']: print('first_failure='+json.dumps(report['first_failure'],sort_keys=True))
    return 0 if report['first_failure'] is None else 1

if __name__=='__main__': raise SystemExit(main())
