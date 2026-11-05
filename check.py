#!/usr/bin/env python3
"""Read-only source checks; never calls providers, sends notices or starts services."""
import ast, json, shutil, subprocess
from pathlib import Path
root=Path(__file__).resolve().parent
counts={'python':0,'shell':0,'javascript':0,'json':0}
for path in root.rglob('*'):
    if not path.is_file() or any(p in ('.git','.venv','venv','node_modules','__pycache__') for p in path.relative_to(root).parts): continue
    if path.suffix=='.py' or (path.parent.name=='bin' and b'python' in path.read_bytes().split(b'\n')[0]):
        ast.parse(path.read_text()); counts['python']+=1
    elif path.suffix=='.sh' or (path.parent.name=='bin' and b'bash' in path.read_bytes().split(b'\n')[0]):
        subprocess.run(['bash','-n',str(path)],check=True); counts['shell']+=1
    elif path.suffix=='.js' and shutil.which('node'):
        subprocess.run(['node','--check',str(path)],check=True,capture_output=True); counts['javascript']+=1
    elif path.suffix=='.json':
        json.loads(path.read_text()); counts['json']+=1
print(json.dumps({'source_checks':'passed','counts':counts}))
