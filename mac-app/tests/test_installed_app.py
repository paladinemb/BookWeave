import os
import json
import plistlib
import subprocess
import unittest
from pathlib import Path

APP = Path(os.environ.get('BOOKWEAVE_APP', '/Applications/BookWeave.app')) / 'Contents'
RESOURCES = APP / 'Resources'

@unittest.skipUnless((APP/'MacOS/BookWeave').is_file(), 'Build or install BookWeave to run bundle tests')
class InstalledAppTest(unittest.TestCase):
    def test_native_application_and_job_safety(self):
        plist=plistlib.loads((APP/'Info.plist').read_bytes())
        self.assertEqual(plist['CFBundleDisplayName'], 'BookWeave')
        result=subprocess.run([str(APP/'MacOS/BookWeave'), '--self-test'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('PASS', result.stdout)

    def test_self_contained_python_and_converter(self):
        result=subprocess.run([str(RESOURCES/'python-runtime/bin/python3.12'), '-B', '-I', '-c',
            'import sys,pypdfium2,markdown,PIL; print(sys.prefix)'],capture_output=True,text=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(result.stdout.strip(), str(RESOURCES/'python-runtime'))

    def test_paddle_models_and_runtime_are_bundled(self):
        engine=RESOURCES/'paddle-engine'
        for name in ['PaddleOCR-VL-1.6-GGUF.gguf','PaddleOCR-VL-1.6-GGUF-mmproj.gguf']:
            with self.subTest(model=name):
                with (engine/'models'/name).open('rb') as f:
                    self.assertEqual(f.read(4),b'GGUF')
        result=subprocess.run([str(engine/'runtime/llama-b11814/llama-server'),'--version'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_runner_accepts_existing_process_group(self):
        result=subprocess.run([str(RESOURCES/'python-runtime/bin/python3.12'),'-B','-I',
            str(RESOURCES/'paddle_converter.py'),'--help'],capture_output=True,text=True,start_new_session=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('BookWeave',result.stdout)
