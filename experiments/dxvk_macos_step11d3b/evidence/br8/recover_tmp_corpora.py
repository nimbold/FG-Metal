"""Recover missing historical copies from their retained workspace mirror.

Never overwrite survivors or follow destination symlinks. Record all copied
bytes. Original per-path equality is unknown except separately pinned report
and hash-index files; restored mirrors must not be claimed exact old trees.
"""
import os,stat,hashlib,json,time
from pathlib import Path
E=Path(__file__).resolve().parent
source=Path('/Users/nima/.codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/bridge-gpu-proof')
roots=[Path('/private/tmp/'+n) for n in ['fgmetal-step11d2-bridge-diagnostic-20261006','fgmetal-step11d2-bridge-diagnostic-b-20261006','fgmetal-step11d2-bridge-diagnostic-c-20261006']]
pinned={'REPORT.md':'9e43b796bd96586478e6fcd931155dbb18d38787fdb0c6aa17d0fe213477f014','HASHES.json':'eccbe768e28368050010cbecd1c4b0ee15d4b587b827f6d88447774148892c0b'}
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
for rel,digest in pinned.items():assert sha(source/rel)==digest
rows=[]
for root in roots:
 assert root.is_dir() and not root.is_symlink()
 for src in sorted(source.rglob('*')):
  rel=src.relative_to(source);dst=root/rel
  if src.is_symlink():continue
  for parent in [root,*list(dst.parents)[:len(rel.parts)-1]]:
   if parent.exists():assert parent.is_dir() and not parent.is_symlink(),str(parent)
  if src.is_dir():
   if not dst.exists():dst.mkdir(parents=True)
   continue
  assert src.is_file() and stat.S_ISREG(src.lstat().st_mode)
  if dst.exists() or dst.is_symlink():continue
  dst.parent.mkdir(parents=True,exist_ok=True)
  data=src.read_bytes();digest=hashlib.sha256(data).hexdigest();fd=os.open(dst,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,stat.S_IMODE(src.stat().st_mode))
  with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
  assert sha(dst)==digest
  rows.append({'source':str(src),'restored':str(dst),'sha256':digest,'bytes':len(data),'exact_original_identity':'pinned' if str(rel) in pinned else 'UNKNOWN; retained mirror identity only'})
 for rel,digest in pinned.items():assert sha(root/rel)==digest
(E/'tmp-corpus-recovery-receipt.json').write_text(json.dumps({'status':'PASS','restored_file_count':len(rows),'restored_bytes':sum(x['bytes'] for x in rows),'copied':rows,'deleted':[],'overwritten':[],'original_per_path_equality':'UNKNOWN except pinned REPORT.md/HASHES.json','time_unix':time.time()},indent=2)+'\n')
print(len(rows),sum(x['bytes'] for x in rows))
