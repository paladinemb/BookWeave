import importlib.util
import tempfile
import unittest
from pathlib import Path
import sys
import os
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

class IsolatedConversionTest(unittest.TestCase):
    def setUp(self):
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        settings = patch.dict(os.environ, {'BOOKWEAVE_CACHE_DIR': cache.name})
        settings.start()
        self.addCleanup(settings.stop)

class PaddleConversionTest(IsolatedConversionTest):
    def test_empty_or_truncated_ocr_is_not_silently_published(self):
        import paddle_converter as c
        self.assertTrue(callable(getattr(c, "recognized_text", None)), "OCR response validation missing")
        with self.assertRaises(ValueError):
            c.recognized_text({'choices': [{'finish_reason':'length','message':{'content':'partial'}}]})
        with self.assertRaises(ValueError):
            c.recognized_text({'choices': [{'finish_reason':'stop','message':{'content':''}}]})
        self.assertEqual(c.recognized_text({'choices': [{'finish_reason':'stop','message':{'content':'中文 English'}}]}), '中文 English')

    def test_text_pdf_creates_bilingual_epub_without_loading_model(self):
        import paddle_converter as c
        self.assertTrue(callable(getattr(c, "convert", None)), "Paddle conversion missing")
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            pdf=root/'book.pdf'
            import shutil
            shutil.copy(ROOT.parent / 'fixtures/English-source.pdf', pdf)
            out=c.convert(pdf, root/'output', '中文 Book', 'Author', 'zh', ROOT)
            self.assertTrue(out.is_file())
            with zipfile.ZipFile(out) as z:
                data=''.join(z.read(n).decode('utf-8') for n in z.namelist() if n.endswith(('.xhtml','.opf')))
                self.assertIn('English',data)
                self.assertIn('中文 Book',data)
            with self.assertRaises(FileExistsError):
                c.convert(pdf, root/'output', 'Book', '', 'en', ROOT)

class BlankPageTest(IsolatedConversionTest):
    def test_blank_pdf_does_not_abort_or_load_ocr(self):
        import paddle_converter as c
        import pypdfium2 as pdfium
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            doc=pdfium.PdfDocument.new()
            page=doc.new_page(300,400)
            doc.save(str(p/'blank.pdf'))
            page.close();doc.close()
            with patch.object(c.LocalOCR,'read', side_effect=AssertionError('Blank page must not invoke OCR')):
                result=c.convert(p/'blank.pdf',p/'out','Blank','','en',ROOT)
            self.assertTrue(result.is_file())

class FailedOCRPageTest(IsolatedConversionTest):
    def test_failed_ocr_preserves_page_in_epub_and_continues(self):
        import paddle_converter as c
        import shutil,zipfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            shutil.copy(ROOT.parent/'fixtures/English-scan.pdf',p/'scan.pdf')
            with patch.object(c.LocalOCR,'read',side_effect=ValueError('truncated')):
                out=c.convert(p/'scan.pdf',p/'out','Scan','','en',ROOT)
            with zipfile.ZipFile(out) as z:
                self.assertTrue(any(n.endswith('.png') for n in z.namelist()))
            self.assertIn('第 1 页',(out.parent/'需校对页面.txt').read_text())

class KeepImagesTest(IsolatedConversionTest):
    def test_successful_scan_keeps_image_alongside_editable_text(self):
        import paddle_converter as c
        import shutil,zipfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            shutil.copy(ROOT.parent/'fixtures/English-scan.pdf',p/'scan.pdf')
            with patch.object(c.LocalOCR,'read',return_value='Editable English text'):
                out=c.convert(p/'scan.pdf',p/'out','Scan','','en',ROOT)
            with zipfile.ZipFile(out) as z:
                self.assertTrue(any(n.endswith('.png') for n in z.namelist()),'Successful OCR must retain source picture')
                self.assertTrue(any(b'Editable English text' in z.read(n) for n in z.namelist() if n.endswith('.xhtml')))

class DecorativeImageTest(IsolatedConversionTest):
    def test_repeated_margin_logo_is_removed_but_body_picture_survives(self):
        import paddle_converter as c
        import pypdfium2 as pdfium
        self.assertTrue(callable(getattr(c,'repeated_decorations',None)))
        with tempfile.TemporaryDirectory() as d:
            import io
            from PIL import Image
            doc=pdfium.PdfDocument.new()
            for index in range(3):
                page=doc.new_page(600,800)
                boxes=[(8,760,10,20)]
                if index==2: boxes.append((160,250,240,200))
                for left,bottom,width,height in boxes:
                    data=io.BytesIO()
                    Image.new('RGB',(20,20),'navy' if width==10 else 'green').save(data,format='JPEG')
                    data.seek(0)
                    image=pdfium.PdfImage.new(doc)
                    image.load_jpeg(data)
                    image.set_matrix(pdfium.PdfMatrix(a=width,d=height,e=left,f=bottom))
                    page.insert_obj(image)
                page.gen_content()
                page.close()
            excluded=c.repeated_decorations(doc)
            self.assertTrue(excluded)
            p=Path(d)
            page=doc[0]
            self.assertEqual(c.preserve_pictures(page,p,1,False,excluded),'')
            page.close()
            page=doc[2]
            references=c.preserve_pictures(page,p,3,False,excluded)
            self.assertEqual(references.count('!['),1)
            page.close();doc.close()
