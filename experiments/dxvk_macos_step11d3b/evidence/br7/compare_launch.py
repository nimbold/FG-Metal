import json
from pathlib import Path
E=Path(__file__).resolve().parent;R=E.parents[1]
a=json.loads((E/'additional-run.json').read_text())
oldid='step11d3b-baseline-02-20261010-054210-3a16e5aa'
o=json.loads((R/'runs'/oldid/'run-manifest.json').read_text());n=json.loads((R/'runs'/a['run_id']/'run-manifest.json').read_text())
identities={k:{'br6':o['candidate_identities'][k].get('sha256'),'br7':n['candidate_identities'].get(k,{}).get('sha256'),'same':o['candidate_identities'][k].get('sha256')==n['candidate_identities'].get(k,{}).get('sha256')} for k in o['candidate_identities']}
fields=['command','duration_seconds','requested','prefix_template','prefix_template_runtime_identity','runtime_root','wine_version','moltenvk_version','environment']
comp={k:{'br6':o.get(k),'br7':n.get(k),'same':o.get(k)==n.get(k)} for k in fields}
result={'br6_run_id':oldid,'br7_run_id':a['run_id'],'candidate_identities':identities,'manifest_fields':comp,'different_top_level_keys':[k for k in sorted(set(o)|set(n)) if o.get(k)!=n.get(k)],'limits':['New run IDs/nonces/tokens/lease clones/authentication differ by design. Compare actual environment path substitutions in retained manifests, not an assumed byte-identical launch.','BR6 has no target AppKit/display identity telemetry; exact prior focus/screen state cannot be reproduced or compared.','BR7 removes tracer startup activation and drawable-size assignments. This is the closest permitted 300s baseline2 reproduction, not an exact old-proxy repeat.','Application binary/source, runtime tree, prefix template, MoltenVK, requested FIFO/images/geometry are compared explicitly. Window creation order comes from unchanged application binary; host state/timing is observed, not forced.']}
def normalized_env(m):
 return {k:('<RUN_NONCE_OR_TOKEN>' if 'NONCE' in k or 'TOKEN' in k else str(v).replace(m['experiment_id'],'<RUN_ID>')) for k,v in m['environment'].items()}
result['normalized_environment']={k:{'br6':normalized_env(o).get(k),'br7':normalized_env(n).get(k),'same':normalized_env(o).get(k)==normalized_env(n).get(k)} for k in sorted(set(o['environment'])|set(n['environment']))}
result['normalized_environment_difference_keys']=[k for k,v in result['normalized_environment'].items() if not v['same']]
(E/'launch-comparison.json').write_text(json.dumps(result,indent=2)+'\n')
