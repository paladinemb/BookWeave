import io
import json
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont
import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paddle_converter as c


def scanned_page(number):
    image = Image.new('RGB', (600, 800), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=16)
    draw.text((85, 100), f'English body text on page {number}', font=font, fill='black')
    draw.rectangle((120, 240, 420, 420), fill='green')
    draw.text((120, 430), 'Figure caption', font=font, fill='black')
    draw.text((270, 750), str(number), font=font, fill='black')
    draw.rectangle((570, 170, 590, 275), fill='black')
    draw.text((574, 205), str(number), font=font, fill='white')
    if number == 3:
        draw.ellipse((6, 500, 26, 520), fill='blue')
    return image


def write_scan_pdf(path, count=3):
    doc = pdfium.PdfDocument.new()
    for number in range(1, count+1):
        page = doc.new_page(600, 800)
        image = scanned_page(number)
        data = io.BytesIO()
        image.save(data, format='JPEG', quality=98)
        image.close()
        data.seek(0)
        obj = pdfium.PdfImage.new(doc)
        obj.load_jpeg(data)
        obj.set_matrix(pdfium.PdfMatrix(a=600, d=800))
        page.insert_obj(obj)
        page.gen_content()
        page.close()
    doc.save(str(path))
    doc.close()


def blocked_margin_scan(pdf):
    time.sleep(5)
    raise AssertionError('The stuck scan should have been terminated')


class ScanCleanupTest(unittest.TestCase):
    def test_stuck_decoration_scan_keeps_uncertain_graphics_and_returns_a_warning(self):
        self.assertTrue(callable(getattr(c, 'scan_raster_decorations', None)), 'Unbounded decoration rendering')
        started = time.monotonic()
        with patch.object(c, 'PREPARATION_TIMEOUT', .1), patch.object(c, 'raster_decorations_from_pdf', blocked_margin_scan):
            patterns, warning = c.scan_raster_decorations('unused.pdf')
        self.assertLess(time.monotonic()-started, 3)
        self.assertEqual(patterns, [])
        self.assertTrue(warning)

    def test_chapter_tabs_just_inside_the_margin_are_still_removed(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d)/'scan.pdf'
            write_scan_pdf(pdf)
            with closing(pdfium.PdfDocument(str(pdf))) as doc:
                patterns = c.raster_decorations(doc)
            original = scanned_page(1)
            draw = ImageDraw.Draw(original)
            draw.rectangle((570, 170, 590, 275), fill='white')
            draw.rectangle((540, 170, 560, 275), fill='black')
            cleaned, count = c.remove_margin_decorations(original, patterns)
            self.assertEqual(cleaned.getpixel((550, 190)), (255, 255, 255))
            self.assertEqual(count, 1)
            cleaned.close(); original.close()

    def test_repeated_scan_tabs_removed_without_erasing_body_caption_or_page_number(self):
        self.assertTrue(callable(getattr(c, 'raster_decorations', None)), 'Scanned margin cleanup missing')
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d)/'scan.pdf'
            write_scan_pdf(pdf)
            with closing(pdfium.PdfDocument(str(pdf))) as doc:
                patterns = c.raster_decorations(doc)
            self.assertTrue(patterns, 'Repeated printed chapter tabs must be detected')
            original = scanned_page(3)
            cleaned, count = c.remove_margin_decorations(original, patterns)
            self.assertEqual(cleaned.getpixel((580, 190)), (255, 255, 255))
            self.assertEqual(cleaned.getpixel((200, 300)), (0, 128, 0))
            self.assertEqual(cleaned.getpixel((16, 510)), (0, 0, 255), 'Unique margin picture must survive')
            self.assertEqual(cleaned.crop((80, 90, 440, 130)).tobytes(), original.crop((80, 90, 440, 130)).tobytes())
            self.assertEqual(cleaned.crop((110, 425, 300, 455)).tobytes(), original.crop((110, 425, 300, 455)).tobytes())
            self.assertEqual(cleaned.crop((250, 740, 310, 780)).tobytes(), original.crop((250, 740, 310, 780)).tobytes())
            self.assertEqual(count, 1)
            self.assertEqual(original.getpixel((580, 190)), (0, 0, 0), 'Source image must not be modified')
            cleaned.close(); original.close()

    def test_single_page_graphics_are_not_assumed_to_be_decorations(self):
        self.assertTrue(callable(getattr(c, 'raster_decorations', None)), 'Scanned margin cleanup missing')
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d)/'scan.pdf'
            write_scan_pdf(pdf, count=1)
            with closing(pdfium.PdfDocument(str(pdf))) as doc:
                patterns = c.raster_decorations(doc)
            self.assertEqual(patterns, [])

    def test_cleaned_scan_is_cropped_then_sent_to_ocr_at_high_resolution(self):
        self.assertTrue(callable(getattr(c, 'raster_decorations', None)), 'Scanned margin cleanup missing')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); pdf = root/'scan.pdf'
            write_scan_pdf(pdf)
            before = pdf.read_bytes()
            def recognize(path):
                with Image.open(path) as image:
                    self.assertGreater(image.height, 2400, 'Small text needs more than the old 144 DPI input')
                    self.assertLess(image.height, 3300, 'Blank margins should be trimmed before OCR')
                    green = sum(g > r*2 and g > b*2 for r,g,b in image.convert('RGB').getdata())
                    self.assertGreater(green, 400000, 'Body figure must survive cleanup')
                return 'Editable English body text and figure caption'
            with patch.dict('os.environ', {'BOOKWEAVE_CACHE_DIR': str(root/'cache')}), patch.object(c.LocalOCR, 'read', side_effect=recognize):
                epub = c.convert(pdf, root/'out', 'Book', '', 'en', ROOT)
            self.assertEqual(pdf.read_bytes(), before)
            state = json.loads((epub.parent/'任务状态.json').read_text())
            first = json.loads((Path(state['cache'])/'page-results/00001.json').read_text())
            self.assertEqual(first['preprocessing']['removed_margin_items'], 1)
            self.assertEqual(first['preprocessing']['ocr_dpi'], 300)
            report = json.loads((epub.parent/'转换报告.json').read_text())
            self.assertEqual(report['preprocessing']['cleaned_pages'], 3)
            self.assertEqual(report['preprocessing']['removed_margin_items'], 3)
            with zipfile.ZipFile(epub) as z:
                self.assertIn('Editable English body text', z.read('OPS/s00000-content.xhtml').decode())
            self.assertEqual(state['validation']['status'], 'passed')


if __name__ == '__main__':
    unittest.main()
