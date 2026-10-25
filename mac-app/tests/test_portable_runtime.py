import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

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

    def test_windows_progress_write_retries_a_temporary_reader_lock(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'state.json';target.write_text('{}')
            replace=Path.replace;attempts=[]
            def replace_after_reader_closed(path,destination):
                attempts.append(1)
                if len(attempts)==1:raise PermissionError(13,'File temporarily open by progress reader')
                return replace(path,destination)
            with patch.object(c.sys,'platform','win32'),patch.object(Path,'replace',autospec=True,side_effect=replace_after_reader_closed):
                c.atomic_json(target,{'status':'转换中'})
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')),{'status':'转换中'})

    def test_mac_without_a_metal_device_starts_cpu_engine(self):
        with tempfile.TemporaryDirectory() as d,patch.object(c.sys,'platform','darwin'),patch.object(c.platform,'machine',return_value='arm64'):
            ocr=c.LocalOCR(Path(d),Path(d));server=MagicMock();server.poll.return_value=None
            response=MagicMock();response.__enter__.return_value.status=200
            with patch.object(c,'resource_sample',return_value=None),patch.object(c.socket,'socket'),patch.object(c.threading,'Thread'),patch.object(c.subprocess,'check_output',return_value='Available devices:\n BLAS: Accelerate'),patch.object(c.subprocess,'Popen',return_value=server) as launch,patch.object(c.urllib.request,'urlopen',return_value=response):
                ocr.start();ocr.close()
            args=launch.call_args.args[0]
            self.assertEqual(args[args.index('-ngl')+1],'0')
            self.assertIn('--no-mmproj-offload',args)

    def test_failed_metal_startup_retries_once_on_cpu(self):
        with tempfile.TemporaryDirectory() as d,patch.object(c.sys,'platform','darwin'),patch.object(c.platform,'machine',return_value='arm64'):
            ocr=c.LocalOCR(Path(d),Path(d));gpu=MagicMock();gpu.poll.return_value=1
            cpu=MagicMock();cpu.poll.return_value=None
            response=MagicMock();response.__enter__.return_value.status=200
            with patch.object(c,'resource_sample',return_value=None),patch.object(c.socket,'socket'),patch.object(c.threading,'Thread'),patch.object(c.subprocess,'check_output',return_value='Available devices:\n MTL0: GPU'),patch.object(c.subprocess,'Popen',side_effect=[gpu,cpu]) as launch,patch.object(c.urllib.request,'urlopen',return_value=response):
                ocr.start();ocr.close()
            self.assertEqual(launch.call_count,2)
            args=launch.call_args.args[0]
            self.assertEqual(args[args.index('-ngl')+1],'0')
