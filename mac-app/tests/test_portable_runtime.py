import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import paddle_converter as c

class PortableRuntimeTest(unittest.TestCase):
    def test_cache_uses_the_platform_user_directory(self):
        self.assertTrue(callable(getattr(c,'default_cache_directory',None)))
        with patch.dict(os.environ,{'LOCALAPPDATA':'/local','XDG_CACHE_HOME':'/xdg'}):
            with patch.object(c.sys,'platform','win32'):self.assertEqual(c.default_cache_directory(),Path('/local/BookWeave/cache'))
            with patch.object(c.sys,'platform','linux'):self.assertEqual(c.default_cache_directory(),Path('/xdg/bookweave'))
            with patch.object(c.sys,'platform','darwin'):self.assertEqual(c.default_cache_directory(),Path.home()/'Library/Application Support/BookWeave/cache')

    def test_task_lock_rejects_duplicate_and_releases_after_failure(self):
        self.assertTrue(callable(getattr(c,'job_lock',None)))
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'lock'
            with self.assertRaisesRegex(ValueError,'interrupted'):
                with c.job_lock(path):
                    with self.assertRaisesRegex(RuntimeError,'转换中'):
                        with c.job_lock(path):self.fail('Duplicate conversion admitted')
                    raise ValueError('interrupted')
            with c.job_lock(path):pass

    def test_engine_uses_windows_executable_and_cpu_without_metal(self):
        ocr=c.LocalOCR(Path('/resources'),Path('/output'),2)
        self.assertTrue(callable(getattr(ocr,'command',None)))
        with patch.object(c.sys,'platform','win32'):
            command=ocr.command(4567)
            self.assertTrue(command[0].endswith('llama-server.exe'))
            self.assertEqual(command[command.index('-ngl')+1],'0')
        with patch.object(c.sys,'platform','linux'):
            self.assertEqual(Path(ocr.command(4567)[0]),Path('/resources/paddle-engine/runtime/llama-b11814/llama-server'))
            self.assertEqual(ocr.command(4567)[ocr.command(4567).index('--parallel')+1],'2')
