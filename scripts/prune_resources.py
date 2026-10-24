"""Remove unused build resources from a newly assembled macOS app."""
import os
from pathlib import Path
import shutil
import subprocess
import sys


def dependencies(path):
    output = subprocess.check_output(['otool', '-L', str(path)], text=True)
    return [line.strip().split(' (', 1)[0] for line in output.splitlines()[1:]]


def prune_runtime(root):
    root = root.resolve()
    pending = [root/'llama-server']
    needed = set()
    inspected = set()
    while pending:
        path = pending.pop()
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError(f'Missing or external runtime dependency: {path}')
        needed.update((path, resolved))
        if resolved in inspected:
            continue
        inspected.add(resolved)
        for dep in dependencies(resolved):
            if dep.startswith(('@rpath/', '@loader_path/', '@executable_path/')):
                pending.append(root/Path(dep).name)
    for path in root.iterdir():
        if path in needed or path.is_dir() or 'license' in path.name.lower():
            continue
        if path.is_symlink() or path.suffix == '.dylib' or os.access(path, os.X_OK):
            path.unlink()


def prune_unused_libpython(root):
    libraries = list((root/'lib').glob('libpython*.dylib'))
    if not libraries:
        return
    targets = {p.resolve() for p in libraries}
    for path in root.rglob('*'):
        if not path.is_file() or path.is_symlink() or path.resolve() in targets:
            continue
        with path.open('rb') as source:
            macho = source.read(4) in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca')
        if macho and any('libpython' in dep for dep in dependencies(path)):
            return
    for path in libraries:
        path.unlink()


def prune_resources(root):
    root = root.resolve()
    if root.name != 'Resources' or root.parent.name != 'Contents' or root.parent.parent.suffix != '.app':
        raise ValueError('Expected a built .app/Contents/Resources directory')
    prune_runtime(root/'paddle-engine/runtime/llama-b11814')
    (root/'paddle-engine/llama-runtime.tar.gz').unlink(missing_ok=True)
    shutil.rmtree(root/'paddle-engine/models/.cache', ignore_errors=True)
    python = root/'python-runtime'
    for relative in ('include', 'share/man', 'lib/pkgconfig', 'lib/tk9.0', 'lib/tcl9.0', 'lib/thread3.0.6',
                     'lib/itcl4.3.8', 'lib/tcl9', 'lib/python3.12/tkinter', 'lib/python3.12/idlelib',
                     'lib/python3.12/turtledemo', 'lib/python3.12/ensurepip', 'lib/python3.12/config-3.12-darwin'):
        path = python/relative
        if path.is_dir():
            shutil.rmtree(path)
    for pattern in ('lib/libtcl*.dylib', 'lib/python3.12/lib-dynload/_tkinter*', 'bin/*-config'):
        for path in python.glob(pattern):
            path.unlink()
    prune_unused_libpython(python)
    for path in list(root.rglob('__pycache__')):
        shutil.rmtree(path)
    for pattern in ('*.pyc', '*.pyo', '.DS_Store'):
        for path in root.rglob(pattern):
            path.unlink()


if __name__ == '__main__':
    prune_resources(Path(sys.argv[1]))
