from pathlib import Path
import importlib.util
import hashlib
import io
import tempfile
import unittest
from unittest.mock import patch

SCRIPT=Path(__file__).resolve().parents[2]/'scripts/prepare_release.py'

class ReleaseResourcesTest(unittest.TestCase):
    def load_script(self):
        spec=importlib.util.spec_from_file_location('prepare_release',SCRIPT)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

    def test_wrong_download_hash_never_installs_or_leaves_partial_file(self):
        m=self.load_script()
        with tempfile.TemporaryDirectory() as d,patch.object(m.urllib.request,'urlopen',return_value=io.BytesIO(b'corrupt download')):
            path=Path(d)/'model.gguf'
            with self.assertRaisesRegex(ValueError,'SHA256'):m.download({'url':'https://example.invalid/model','sha256':'0'*64},path)
            self.assertFalse(path.exists());self.assertFalse(path.with_suffix('.part').exists())

    def test_verified_cache_reuse_does_not_need_network(self):
        m=self.load_script()
        with tempfile.TemporaryDirectory() as d,patch.object(m.urllib.request,'urlopen',side_effect=AssertionError('Do not redownload verified content')):
            path=Path(d)/'model.gguf';path.write_bytes(b'GGUF test')
            m.download({'url':'https://example.invalid/model','sha256':hashlib.sha256(b'GGUF test').hexdigest()},path)
            self.assertEqual(path.read_bytes(),b'GGUF test')
