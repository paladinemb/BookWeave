import json
import signal
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paddle_converter as c


def blocked_preparation(index):
    time.sleep(5)
    raise AssertionError('The blocked worker should have been terminated')


class AutomaticFlowTest(unittest.TestCase):
    def test_corrupt_ocr_text_is_flagged_instead_of_silently_published(self):
        with self.assertRaisesRegex(ValueError, '异常'):
            c.recognized_text({'choices': [{'finish_reason': 'stop', 'message': {'content': 'English \ufffd text'}}]})

    def test_old_cached_garbled_text_is_marked_without_repeating_ocr(self):
        import shutil
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); pdf = root/'book.pdf'
            shutil.copyfile(ROOT.parent/'fixtures/English-scan.pdf', pdf)
            with patch.dict('os.environ', {'BOOKWEAVE_CACHE_DIR': str(root/'cache')}):
                with patch.object(c.LocalOCR, 'read', return_value='Original cached text'):
                    first = c.convert(pdf, root/'first', 'Book', '', 'en', ROOT)
                state = json.loads((first.parent/'任务状态.json').read_text())
                cached = Path(state['cache'])/'page-results/00001.json'
                result = json.loads(cached.read_text())
                result['raw_text'] = 'English \ufffd text'
                result['markdown'] = result['markdown'].replace('Original cached text', result['raw_text'])
                cached.write_text(json.dumps(result))
                with patch.object(c.LocalOCR, 'read', side_effect=AssertionError('Do not repeat OCR for a flagged cached page')):
                    final = c.convert(pdf, root/'final', 'Book', '', 'en', ROOT)
            report = json.loads((final.parent/'转换报告.json').read_text())
            self.assertEqual(len(report['review_pages']), 1)
            self.assertEqual(report['review_pages'][0]['pdf_page'], 1)
            self.assertEqual(report['review_pages'][0]['type'], 'ocr_quality')

    def test_deadline_interrupts_a_blocked_operation_and_restores_signal(self):
        self.assertTrue(callable(getattr(c, 'operation_deadline', None)), 'Missing absolute page deadline')
        previous = signal.getsignal(signal.SIGALRM)
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, '超时'):
            with c.operation_deadline(0.04):
                time.sleep(1)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)

    def test_page_57_is_skipped_once_marked_live_and_reported_after_validation(self):
        import pypdfium2 as p
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = p.PdfDocument(str(ROOT.parent/'fixtures/English-scan.pdf'))
            doc = p.PdfDocument.new()
            doc.import_pages(source, [0] * 60)
            pdf = root/'book.pdf'
            doc.save(str(pdf)); doc.close(); source.close()
            recognized = []
            def read(image):
                page = int(image.stem.split('-')[1])
                recognized.append(page)
                if page == 57:
                    raise TimeoutError('本页识别超时')
                if page == 58:
                    live = (root/'out/book/转换中.md').read_text()
                    self.assertIn('PDF 第 57 页', live)
                    self.assertIn('需校对', live)
                return f'Editable text on PDF page {page}'
            with patch.dict('os.environ', {'BOOKWEAVE_CACHE_DIR': str(root/'cache')}), patch.object(c.LocalOCR, 'read', side_effect=read):
                epub = c.convert(pdf, root/'out', 'Book', '', 'en', ROOT)
            self.assertEqual(recognized, list(range(1, 61)))
            report = json.loads((epub.parent/'转换报告.json').read_text())
            self.assertEqual(report['total_pages'], 60)
            self.assertEqual(report['review_pages'][0]['pdf_page'], 57)
            self.assertTrue(report['review_pages'][0]['image_preserved'])
            self.assertEqual(report['normal_pages'], 59)
            self.assertEqual(report['validation']['status'], 'passed')
            state = json.loads((epub.parent/'任务状态.json').read_text())
            self.assertEqual(state['status'], 'complete')
            self.assertEqual(state['segment_progress'][0]['completed'], 60)
            self.assertEqual(state['segment_progress'][0]['review_count'], 1)
            with zipfile.ZipFile(epub) as z:
                chapter = z.read('OPS/s00000-content.xhtml').decode()
                self.assertIn('PDF 第 57 页', chapter)
                self.assertIn('page-00057.png', chapter)
                self.assertIn('Editable text on PDF page 58', chapter)

    def test_validation_rejects_missing_images_and_page_results(self):
        self.assertTrue(callable(getattr(c, 'validate_epub', None)), 'Missing final EPUB validation')
        with tempfile.TemporaryDirectory() as d:
            epub = Path(d)/'bad.epub'
            with zipfile.ZipFile(epub, 'w') as z:
                z.writestr('mimetype', 'application/epub+zip')
                z.writestr('OPS/chapter.xhtml', '<html><img src="images/missing.png"/></html>')
            with self.assertRaisesRegex(RuntimeError, '图片|资源'):
                c.validate_epub(epub, {0: {'files': ['missing.png']}}, 1)
            with self.assertRaisesRegex(RuntimeError, '页'):
                c.validate_epub(epub, {}, 1)

    def test_blocked_render_worker_is_stopped_and_missing_image_is_reported_honestly(self):
        import shutil
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pdf = root/'book.pdf'
            shutil.copyfile(ROOT.parent/'fixtures/English-source.pdf', pdf)
            started = time.monotonic()
            with patch.dict('os.environ', {'BOOKWEAVE_CACHE_DIR': str(root/'cache')}), \
                 patch.object(c, 'PREPARATION_TIMEOUT', 0.1), patch.object(c, 'prepare_page', blocked_preparation):
                epub = c.convert(pdf, root/'out', 'Book', '', 'en', ROOT)
            self.assertLess(time.monotonic()-started, 3)
            report = json.loads((epub.parent/'转换报告.json').read_text())
            self.assertEqual(report['review_pages'][0]['type'], 'render_error')
            self.assertFalse(report['review_pages'][0]['image_preserved'])
            self.assertIn('未能提取原图', (epub.parent/'需校对页面.txt').read_text())

    def test_valid_recompressed_jpeg_is_not_reported_as_a_missing_or_corrupt_image(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'images').mkdir()
            Image.new('RGB', (96, 64), 'navy').save(root/'images/figure.jpg', quality=96)
            text = '![diagram](images/figure.jpg)'
            (root/'content.md').write_text(text)
            epub = c.package_book(root, Path('book.pdf'), 'Book', '', 'en', ROOT)
            report = c.validate_epub(epub, {0: {'files': ['figure.jpg'], 'markdown': text}}, 1)
            self.assertEqual(report['images'], 1)

    def test_each_segment_uses_a_fresh_render_worker(self):
        import multiprocessing
        import pypdfium2 as p
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            text = p.PdfDocument(str(ROOT.parent/'fixtures/English-source.pdf'))
            scan = p.PdfDocument(str(ROOT.parent/'fixtures/English-scan.pdf'))
            doc = p.PdfDocument.new()
            doc.import_pages(scan, [0]); doc.import_pages(text, [0]*150)
            doc.import_pages(scan, [0]); doc.import_pages(text, [0]*149)
            pdf = root/'book.pdf'; doc.save(str(pdf)); doc.close(); text.close(); scan.close()
            workers = []
            def read(image):
                workers.append({process.pid for process in multiprocessing.active_children()})
                return 'Editable text'
            with patch.dict('os.environ', {'BOOKWEAVE_CACHE_DIR': str(root/'cache')}), patch.object(c.LocalOCR, 'read', side_effect=read):
                epub = c.convert(pdf, root/'out', 'Book', '', 'en', ROOT)
            self.assertEqual(len(workers), 2)
            self.assertTrue(workers[0] and workers[1])
            self.assertTrue(workers[0].isdisjoint(workers[1]), 'Render worker must be replaced between segments')
            state = json.loads((epub.parent/'任务状态.json').read_text())
            self.assertEqual([part['completed'] for part in state['segment_progress']], [151, 150])


if __name__ == '__main__':
    unittest.main()
