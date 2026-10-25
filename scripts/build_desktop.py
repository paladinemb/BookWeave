"""Build a self-contained Windows/Linux app and its platform installer."""
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
VERSION=plistlib.loads((ROOT/'mac-app/Info.plist').read_bytes())['CFBundleShortVersionString']


def run(*args):subprocess.run(list(map(str,args)),check=True,cwd=ROOT)


def build():
    windows=sys.platform=='win32'
    data=[(ROOT/'runtime-resources/paddle-engine','paddle-engine'),(ROOT/'pdf2epub/modules','pdf2epub/modules'),
          (ROOT/'pdf2epub/LICENSE','pdf2epub'),(ROOT/'THIRD_PARTY_NOTICES.md','.'),(ROOT/'LICENSE','.')]
    args=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--windowed','--name','BookWeave',
          '--paths',ROOT/'mac-app','--paths',ROOT/'pdf2epub','--collect-all','pypdfium2_raw','--collect-submodules','markdown.extensions','--collect-data','latex2mathml']
    for package in ['pypdfium2','Pillow','Markdown','regex','latex2mathml']:args += ['--copy-metadata',package]
    for source,dest in data:args += ['--add-data',f'{source}:{dest}']
    if windows:
        from PIL import Image
        icon=ROOT/'build/BookWeave.ico';icon.parent.mkdir(exist_ok=True)
        with Image.open(ROOT/'assets/BookWeave.png') as image:image.save(icon,sizes=[(16,16),(32,32),(48,48),(256,256)])
        args += ['--icon',icon]
    run(*args,ROOT/'desktop/app.py')
    # Data weights and platform libraries are copied verbatim, never analyzed as Python binaries.
    app=ROOT/'dist/BookWeave'
    executable=app/('BookWeave.exe' if windows else 'BookWeave')
    test=ROOT/'build/installed-test'
    command=[executable,'--smoke-test',ROOT/'fixtures',test]
    run(*command) if windows else run('xvfb-run','-a',*command)
    assert json.loads((test/'passed.json').read_text())['scan_ocr']=='passed'
    output=ROOT/'release-output';output.mkdir(exist_ok=True)
    if windows:
        compiler=Path('C:/Program Files (x86)/Inno Setup 6/ISCC.exe')
        run(compiler,f'/DAppVersion={VERSION}',ROOT/'release/windows.iss')
    else:
        package=ROOT/'build/deb';install=package/'opt/bookweave'
        shutil.copytree(app,install,dirs_exist_ok=True,symlinks=True)
        (package/'DEBIAN').mkdir(parents=True,exist_ok=True)
        (package/'DEBIAN/control').write_text(f'Package: bookweave\nVersion: {VERSION}\nSection: utils\nPriority: optional\nArchitecture: amd64\nMaintainer: BookWeave <noreply@github.com>\nDepends: libc6 (>= 2.39), libstdc++6, libgomp1, libx11-6, libxext6, libxrender1, libxft2, libfontconfig1\nDescription: Offline Chinese and English PDF to EPUB converter\n')
        desktop=package/'usr/share/applications';desktop.mkdir(parents=True)
        (desktop/'bookweave.desktop').write_text('[Desktop Entry]\nType=Application\nName=BookWeave\nExec=/opt/bookweave/BookWeave\nIcon=bookweave\nTerminal=false\nCategories=Office;\n')
        icons=package/'usr/share/icons/hicolor/256x256/apps';icons.mkdir(parents=True)
        from PIL import Image
        with Image.open(ROOT/'assets/BookWeave.png') as image:image.resize((256,256)).save(icons/'bookweave.png')
        run('dpkg-deb','--root-owner-group','-Zxz','-z3','--build',package,output/f'BookWeave-{VERSION}-linux-x64.deb')
    assets=list(output.iterdir())
    assert len(assets)==1,assets
    asset=assets[0]
    assert asset.stat().st_size<2*1024**3,'GitHub assets must be under 2 GiB'
    with asset.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    asset.with_suffix(asset.suffix+'.sha256').write_text(f'{digest}  {asset.name}\n')


if __name__=='__main__':build()
