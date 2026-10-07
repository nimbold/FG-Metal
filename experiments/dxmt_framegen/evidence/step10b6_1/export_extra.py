#!/usr/bin/env python3
"""Retain signpost and Metal submission/completion/error tables."""
import subprocess,sys,xml.etree.ElementTree as ET
from pathlib import Path
trace=Path(sys.argv[1]);out=Path(sys.argv[2])
tables=ET.parse(out/'xctrace-toc.xml').getroot().findall('./run[1]/data/table')
wanted={'metal-application-command-buffer-submissions','metal-command-buffer-completed','metal-command-buffer-error'}
for i,node in enumerate(tables,1):
 schema=node.get('schema')
 if schema not in wanted and not (schema in ('os-signpost','os-signpost-arg') and node.get('category')=='PointsOfInterest'):continue
 path=out/(schema+'-'+str(i)+'.xml')
 if path.exists():continue
 subprocess.run(['xcrun','xctrace','export',str(trace),'--xpath',f'//trace-toc[1]/run[1]/data[1]/table[{i}]','--output',str(path)],check=True)
 print(path.name, 'rows=',sum(1 for _ in ET.parse(path).getroot().iter('row')))
