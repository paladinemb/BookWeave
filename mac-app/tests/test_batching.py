import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import paddle_converter as c

class BatchingTest(unittest.TestCase):
    def test_balanced_segments_cover_each_page_exactly_once(self):
        self.assertTrue(callable(getattr(c,'page_segments',None)))
        self.assertEqual(c.page_segments(1000),[(0,250),(250,500),(500,750),(750,1000)])
        self.assertEqual(c.page_segments(800),[(0,267),(267,534),(534,800)])
        self.assertEqual(c.page_segments(1027),[(0,257),(257,514),(514,771),(771,1027)])
        self.assertEqual(c.page_segments(1),[(0,1)])
        with self.assertRaises(ValueError):c.page_segments(0)

    def test_stream_preserves_bilingual_text_and_stops_repetition_early(self):
        self.assertTrue(callable(getattr(c,'consume_ocr_stream',None)))
        def event(text='',finish=None):
            return ('data: '+json.dumps({'choices':[{'delta':{'content':text},'finish_reason':finish}]})+'\n').encode()
        self.assertEqual(c.consume_ocr_stream([event('中文 English'),event(finish='stop')]),'中文 English')
        with self.assertRaisesRegex(ValueError,'重复'):
            def repeating():
                yield event('Datawhale\n25.'+'0'*140)
                raise AssertionError('The stream should already have been cancelled')
            c.consume_ocr_stream(repeating())
        self.assertEqual(c.consume_ocr_stream([event('0 0 0\n'*50),event(finish='stop')]),('0 0 0\n'*50).strip())
        with self.assertRaises(ValueError):c.consume_ocr_stream([event('partial',finish='length')])

    def test_repeated_text_blocks_cancel_but_numeric_tables_do_not(self):
        def event(text, finish=None):
            return ('data: '+json.dumps({'choices':[{'delta':{'content':text},'finish_reason':finish}]})+'\n').encode()
        with self.assertRaisesRegex(ValueError,'重复'):
            c.consume_ocr_stream([event('Deep scar\nTemperature Affect\nHigh Stress\nFacial Excision\n'*10),event('', 'stop')])
        rows='0 0 0 0 0 0\n'*100
        self.assertEqual(c.consume_ocr_stream([event(rows),event('', 'stop')]), rows.strip())

    def test_resume_uses_completed_pages_and_reuses_images(self):
        import pypdfium2 as p
        import zipfile
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'BOOKWEAVE_CACHE_DIR':str(Path(d)/'cache')}):
            root=Path(d)
            src=p.PdfDocument(str(ROOT.parent/'fixtures/English-scan.pdf'))
            doc=p.PdfDocument.new();doc.import_pages(src,[0,0]);doc.save(str(root/'book.pdf'))
            doc.close();src.close()
            with patch.object(c.LocalOCR,'read',side_effect=['First page',KeyboardInterrupt]):
                with self.assertRaises(KeyboardInterrupt):c.convert(root/'book.pdf',root/'run1','Book','','en',ROOT)
            with patch.object(c.LocalOCR,'read',return_value='Second page') as recognition:
                out=c.convert(root/'book.pdf',root/'run2','Book','','en',ROOT)
            self.assertEqual(recognition.call_count,1,'Completed first page must not be recognized again')
            with zipfile.ZipFile(out) as z:
                html=''.join(z.read(n).decode() for n in z.namelist() if n.endswith('.xhtml'))
                self.assertLess(html.index('First page'),html.index('Second page'))
                self.assertEqual(sum(n.endswith('.png') for n in z.namelist()),2)
            state=json.loads((out.parent/'任务状态.json').read_text())
            self.assertEqual(state['completed'],2)
            self.assertEqual(state['status'],'complete')
            with patch.object(c.LocalOCR,'read',side_effect=AssertionError('Cached EPUB rebuild must not use OCR')):
                c.convert(root/'book.pdf',root/'run3','Revised title','','en',ROOT)

    def test_retry_failed_does_not_repeat_successful_pages(self):
        import pypdfium2 as p
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'BOOKWEAVE_CACHE_DIR':str(Path(d)/'cache')}):
            root=Path(d)
            src=p.PdfDocument(str(ROOT.parent/'fixtures/English-scan.pdf'))
            doc=p.PdfDocument.new();doc.import_pages(src,[0,0]);doc.save(str(root/'book.pdf'));doc.close();src.close()
            with patch.object(c.LocalOCR,'read',side_effect=[ValueError('bad page'),'Good second page']):
                c.convert(root/'book.pdf',root/'run1','Book','','en',ROOT)
            with patch.object(c.LocalOCR,'read',return_value='Recovered first page') as recognition:
                out=c.convert(root/'book.pdf',root/'run2','Book','','en',ROOT,retry_failed=True)
            self.assertEqual(recognition.call_count,1)
            self.assertEqual((out.parent/'需校对页面.txt').read_text(),'')
            self.assertIn('Good second page',(out.parent/'content.md').read_text())

    def test_engine_start_failure_pauses_job_instead_of_silently_finishing(self):
        import shutil
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'BOOKWEAVE_CACHE_DIR':str(Path(d)/'cache')}):
            root=Path(d)
            shutil.copy(ROOT.parent/'fixtures/English-scan.pdf',root/'scan.pdf')
            with patch.object(c.LocalOCR,'read',side_effect=RuntimeError('engine cannot start')):
                with self.assertRaisesRegex(RuntimeError,'engine cannot start'):
                    c.convert(root/'scan.pdf',root/'out','Book','','en',ROOT)
            state=json.loads((root/'out/scan/任务状态.json').read_text())
            self.assertEqual(state['status'],'paused')
            self.assertEqual(state['completed'],0)
