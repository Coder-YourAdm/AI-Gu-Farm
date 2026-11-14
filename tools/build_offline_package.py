"""Build the current frontend and local tutor into a self-contained offline ZIP."""
from __future__ import annotations
import hashlib
import json
import shutil
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
STAGE=ROOT/'dist/demo_offline'
ZIP=ROOT/'dist/demo_offline.zip'
SKIP={'__pycache__','.pytest_cache','.env','node_modules'}
def main():
    STAGE.mkdir(parents=True,exist_ok=True)
    files=[]
    for name in ('app','web','data','prompts'):
        for p in (ROOT/name).rglob('*'):
            if not p.is_file() or any(x in SKIP for x in p.parts) or p.suffix=='.pyc' or p.name.endswith('.local.json'): continue
            dst=STAGE/name/p.relative_to(ROOT/name)
            dst.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(p,dst)
            files.append(dst)
    for p in (ROOT/'docs/demo/offline_assets').iterdir():
        if p.is_file():
            dst=STAGE/p.name
            shutil.copy2(p,dst)
            files.append(dst)
    assets={p.name:json.loads(p.read_text(encoding='utf-8-sig')) for p in files if p.parent==STAGE/'web' and p.suffix=='.json' and p.name!='evolve.json'}
    bundle='/* Public packaged snapshots. */\nwindow.RIXIN_DATA='+json.dumps(assets,ensure_ascii=False,separators=(',',':'))+';\n'
    (STAGE/'web/offline-data.js').write_text(bundle,encoding='utf-8')
    if (STAGE/'web/offline-data.js') not in files: files.append(STAGE/'web/offline-data.js')
    files=sorted(set(files))
    text='SHA-256\n'+'\n'.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.relative_to(STAGE).as_posix() for p in files)+'\n'
    manifest=STAGE/'MANIFEST.txt'
    manifest.write_text(text,encoding='utf-8')
    with zipfile.ZipFile(ZIP,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for p in files+[manifest]: z.write(p,'demo_offline/'+p.relative_to(STAGE).as_posix())
    print(f'Built {ZIP} ({ZIP.stat().st_size:,} bytes), {len(files)+1} files')
    return 0
if __name__=='__main__': raise SystemExit(main())
