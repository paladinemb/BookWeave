import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2]/'scripts/prune_resources.py'


class PackagingTest(unittest.TestCase):
    def load_pruner(self):
        self.assertTrue(SCRIPT.is_file(), 'Build still bundles unused runtime resources')
        spec = importlib.util.spec_from_file_location('prune_resources', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_runtime_pruning_keeps_transitive_libraries_aliases_and_license(self):
        p = self.load_pruner()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in ('llama-server','libserver.dylib','libcore.1.dylib','llama-bench','LICENSE','kernels.metallib'):
                (root/name).write_text(name)
            (root/'llama-bench').chmod(0o755)
            (root/'libcore.dylib').symlink_to('libcore.1.dylib')
            (root/'llama-unused-alias').symlink_to('llama-bench')
            outputs = {'llama-server': 'llama-server:\n\t@rpath/libserver.dylib (compatibility version 0)\n',
                       'libserver.dylib': 'libserver.dylib:\n\t@rpath/libcore.dylib (compatibility version 0)\n',
                       'libcore.1.dylib': 'libcore.1.dylib:\n\t/usr/lib/libSystem.B.dylib (compatibility version 0)\n'}
            with patch.object(p.subprocess, 'check_output', side_effect=lambda args, **kw: outputs[Path(args[-1]).name]):
                p.prune_runtime(root)
            self.assertFalse((root/'llama-bench').exists())
            self.assertFalse((root/'llama-unused-alias').is_symlink())
            self.assertEqual((root/'libcore.dylib').read_text(), 'libcore.1.dylib')
            self.assertEqual((root/'libserver.dylib').read_text(), 'libserver.dylib')
            self.assertTrue((root/'LICENSE').is_file())
            self.assertTrue((root/'kernels.metallib').is_file())

    def test_resource_cleanup_preserves_both_models_and_python_licenses(self):
        p = self.load_pruner()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)/'Book.app/Contents/Resources'
            paths = {'paddle-engine/models/main.gguf': b'GGUF main weights',
                     'paddle-engine/models/mmproj.gguf': b'GGUF essential visual weights',
                     'paddle-engine/llama-runtime.tar.gz': b'unused archive',
                     'python-runtime/lib/python3.12/LICENSE.txt': b'Python license',
                     'python-runtime/include/python3.12/Python.h': b'development header',
                     'python-runtime/lib/python3.12/ensurepip/installer.py': b'not needed',
                     'python-runtime/lib/python3.12/tkinter/__init__.py': b'not needed',
                     'python-runtime/lib/python3.12/site-packages/PIL/image.py': b'required package',
                     'python-runtime/lib/python3.12/site-packages/PIL/__pycache__/image.pyc': b'generated cache'}
            for name,data in paths.items():
                target=root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            with patch.object(p, 'prune_runtime'), patch.object(p, 'prune_unused_libpython'):
                p.prune_resources(root)
            self.assertEqual((root/'paddle-engine/models/mmproj.gguf').read_bytes(), b'GGUF essential visual weights')
            self.assertEqual((root/'paddle-engine/models/main.gguf').read_bytes(), b'GGUF main weights')
            self.assertFalse((root/'paddle-engine/llama-runtime.tar.gz').exists())
            self.assertFalse((root/'python-runtime/include').exists())
            self.assertFalse((root/'python-runtime/lib/python3.12/ensurepip').exists())
            self.assertFalse((root/'python-runtime/lib/python3.12/tkinter').exists())
            self.assertFalse(list(root.rglob('__pycache__')))
            self.assertTrue((root/'python-runtime/lib/python3.12/LICENSE.txt').is_file())
            self.assertTrue((root/'python-runtime/lib/python3.12/site-packages/PIL/image.py').is_file())

    def test_pruner_rejects_non_app_directory(self):
        p = self.load_pruner()
        with tempfile.TemporaryDirectory() as d:
            sentinel = Path(d)/'keep.txt';sentinel.write_text('keep')
            with self.assertRaises(ValueError):
                p.prune_resources(Path(d))
            self.assertEqual(sentinel.read_text(), 'keep')

    def test_libpython_is_kept_when_an_extension_links_to_it(self):
        p = self.load_pruner()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); library=root/'lib/libpython3.12.dylib'; extension=root/'lib/helper.so'
            library.parent.mkdir(); library.write_bytes(b'\xcf\xfa\xed\xfe library');extension.write_bytes(b'\xcf\xfa\xed\xfe extension')
            with patch.object(p.subprocess, 'check_output', return_value='helper.so:\n\t@rpath/libpython3.12.dylib (compatibility version 0)\n'):
                p.prune_unused_libpython(root)
            self.assertTrue(library.is_file())

    def test_unreferenced_libpython_copy_is_removed(self):
        p = self.load_pruner()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); library=root/'lib/libpython3.12.dylib'; executable=root/'bin/python3.12'
            library.parent.mkdir();executable.parent.mkdir()
            library.write_bytes(b'\xcf\xfa\xed\xfe library');executable.write_bytes(b'\xcf\xfa\xed\xfe executable')
            with patch.object(p.subprocess, 'check_output', return_value='python3.12:\n\t/usr/lib/libSystem.B.dylib (compatibility version 0)\n'):
                p.prune_unused_libpython(root)
            self.assertFalse(library.exists())
            self.assertTrue(executable.is_file())


if __name__ == '__main__':
    unittest.main()
