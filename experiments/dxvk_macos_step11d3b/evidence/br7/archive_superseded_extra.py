"""Archive only superseded B evidence, verify all bytes, retain receipts before retirement."""
from pathlib import Path
import hashlib,json,os,shutil,tarfile,time
base=Path(__file__).resolve().parents[2]
evidence=Path(__file__).resolve().parent
protected={'step11d3b-smoke-20261010-053718-a78894d0','step11d3b-smoke-20261009-223106-0e5e3a6b','step11d3b-baseline-01-20261009-223854-cfea0560','step11d3b-baseline-02-20261010-054210-3a16e5aa','step11d3b-baseline-03-20261010-062321-91bb8f4d'}
paths=[base/'evidence/br4/previous-control',base/'evidence/br4/control-build']
archive=evidence/'superseded-pre-br7-extra.tar.gz'
receipt=evidence/'superseded-extra-retirement-receipt.json'
def digest(stream):
 h=hashlib.sha256()
 for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
 return h.hexdigest()
entries={}
for directory in paths:
 if directory.is_symlink() or not directory.is_relative_to(base):raise RuntimeError('unsafe directory')
 for p in directory.rglob('*'):
  if p.is_symlink(): raise RuntimeError(f'unsupported symlink {p}')
  if p.is_file():
   st=p.stat()
   with p.open('rb') as f:h=digest(f)
   entries[str(p.relative_to(base))]={'sha256':h,'size':st.st_size,'mtime_ns':st.st_mtime_ns,'inode':st.st_ino,'device':st.st_dev}
with tarfile.open(archive,'w:gz',compresslevel=1) as tf:
 for p in paths:tf.add(p,arcname=str(p.relative_to(base)),recursive=True)
verified=set()
with tarfile.open(archive,'r:gz') as tf:
 for m in tf:
  if m.isfile():
   if m.name not in entries:raise RuntimeError('unexpected archive member')
   with tf.extractfile(m) as f:h=digest(f)
   if h!=entries[m.name]['sha256'] or m.size!=entries[m.name]['size']:raise RuntimeError('archive mismatch')
   verified.add(m.name)
if verified!=set(entries):raise RuntimeError('incomplete archive')
for name,entry in entries.items():
 p=base/name;st=p.stat()
 if (st.st_size,st.st_mtime_ns,st.st_ino,st.st_dev)!=(entry['size'],entry['mtime_ns'],entry['inode'],entry['device']):raise RuntimeError('original changed')
with archive.open('rb') as f:archive_sha=digest(f)
r={'status':'VERIFIED_BEFORE_RETIREMENT','archive':str(archive),'archive_sha256':archive_sha,'archive_bytes':archive.stat().st_size,'retired_directories':[str(p) for p in paths],'protected_accepted_evidence':sorted(protected),'files':entries,'created_unix':time.time(),'restore':'Extract archive into experiments/dxvk_macos_step11d3b; member paths retain original locations.'}
receipt.write_text(json.dumps(r,indent=2)+'\n')
for p in paths:shutil.rmtree(p)
r['status']='PASS';r['retired_original_logical_bytes']=sum(e['size'] for e in entries.values());r['verified_regular_files']=len(entries)
receipt.write_text(json.dumps(r,indent=2)+'\n')
print({k:v for k,v in r.items() if k not in ('files','retired_directories','protected_accepted_evidence')})
