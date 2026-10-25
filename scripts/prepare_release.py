"""Fetch checksum-pinned official OCR resources for one release platform."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def sha256(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def download(item,destination):
    destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists() and sha256(destination)==item['sha256']:return
    part=destination.with_suffix('.part')
    try:
        request=urllib.request.Request(item['url'],headers={'User-Agent':'BookWeave-release'})
        with urllib.request.urlopen(request,timeout=180) as source,part.open('wb') as target:shutil.copyfileobj(source,target,1024*1024)
        if sha256(part)!=item['sha256']:raise ValueError(f'SHA256 mismatch: {destination.name}')
        part.replace(destination)
    finally:part.unlink(missing_ok=True)


def unpack(archive,destination):
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for item in z.infolist():
                if not (destination/item.filename).resolve().is_relative_to(destination.resolve()):raise ValueError('Unsafe archive path')
            z.extractall(destination)
    else:
        with tarfile.open(archive) as t:t.extractall(destination,filter='data')


def prepare(target):
    manifest=json.loads((ROOT/'release/resources.json').read_text())
    resources=ROOT/'runtime-resources';engine=resources/'paddle-engine'
    for item in manifest['models']:download(item,engine/'models'/item['name'])
    runtime_name={'macos-arm64':'macos-arm64.tar.gz','macos-x64':'macos-x64.tar.gz','windows-x64':'win-cpu-x64.zip','linux-x64':'ubuntu-x64.tar.gz'}[target]
    runtime=next(item for item in manifest['llama'] if item['name'].endswith(runtime_name))
    with tempfile.TemporaryDirectory() as temp:
        temp=Path(temp);archive=temp/'download';download(runtime,archive);unpack(archive,temp/'unpacked')
        executable='llama-server.exe' if target=='windows-x64' else 'llama-server'
        server=next((temp/'unpacked').rglob(executable))
        shutil.copytree(server.parent,engine/'runtime/llama-b11814',dirs_exist_ok=True,symlinks=True)
    notices=engine/'licenses';notices.mkdir(parents=True,exist_ok=True)
    for name,url in [('llama-LICENSE','https://raw.githubusercontent.com/ggml-org/llama.cpp/b11814/LICENSE'),
                     ('PaddleOCR-LICENSE','https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/main/LICENSE'),
                     ('Python-LICENSE','https://raw.githubusercontent.com/python/cpython/v3.12.15/LICENSE'),
                     ('PyInstaller-COPYING','https://raw.githubusercontent.com/pyinstaller/pyinstaller/v6.22.3/COPYING.txt')]:
        with urllib.request.urlopen(url,timeout=60) as response:(notices/name).write_bytes(response.read())
    if target.startswith('macos'):
        arch='aarch64' if target.endswith('arm64') else 'x86_64'
        item=next(item for item in manifest['python'] if arch+'-apple' in item['name'])
        with tempfile.TemporaryDirectory() as temp:
            temp=Path(temp);archive=temp/'python.tar.gz';download(item,archive);unpack(archive,temp/'unpacked')
            shutil.copytree(temp/'unpacked/python',resources/'python-runtime',symlinks=True,dirs_exist_ok=True)
        python=resources/'python-runtime/bin/python3.12'
        subprocess.run([str(python),'-m','ensurepip'],check=True)
        subprocess.run([str(python),'-m','pip','install','-r',str(ROOT/'requirements.txt')],check=True)
    print(f'Verified resources prepared: {target}',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('target',choices=['macos-arm64','macos-x64','windows-x64','linux-x64'])
    prepare(parser.parse_args().target)
