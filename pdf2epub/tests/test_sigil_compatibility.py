import tempfile
import unittest
from xml.etree import ElementTree as ET

from modules.mark2epub import get_chapter_XML, get_coverpage_XML, get_TOC_XML, copy_and_optimize_image, convert_math_to_mathml
from pathlib import Path
from PIL import Image


class SigilCompatibilityTest(unittest.TestCase):
    def test_existing_png_is_copied_without_losing_transparency_or_recompression(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/'source.png'; target=root/'target.png'
            Image.new('RGBA',(16,16),(30,90,150,128)).save(source)
            copy_and_optimize_image(source,target)
            self.assertEqual(source.read_bytes(),target.read_bytes())

    def test_malformed_ocr_tags_remain_readable_in_valid_xhtml(self):
        for text in ['Text <fcel> continues', 'Text<br> &nbsp; continues']:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                chapter, _ = get_chapter_XML(directory, 'scan.md', [], content=text)
                try:
                    root = ET.fromstring(chapter)
                except ET.ParseError as error:
                    self.fail(f'OCR text produced invalid XHTML: {error}')
                self.assertIn('Text', ''.join(root.itertext()))
                self.assertIn('continues', ''.join(root.itertext()))
                self.assertIn('&lt;', chapter)
        with tempfile.TemporaryDirectory() as directory:
            chapter, _ = get_chapter_XML(directory, 'table.md', [], content='<table><tr><td>中文</td></tr></table>')
            self.assertIsNotNone(ET.fromstring(chapter).find('.//{*}td'))

    def test_currency_is_not_math_and_does_not_cross_paragraphs(self):
        html='<p>It costs $50 today and $60 tomorrow.</p><p>Budget: $20.</p><p>Another cost: $30.</p>'
        output=convert_math_to_mathml(html)
        self.assertEqual(output,html)
        ET.fromstring('<body>'+output+'</body>')

    def test_explicit_inline_math_still_renders_as_valid_mathml(self):
        output=convert_math_to_mathml('<p>Formula: $E=mc^2$.</p>')
        root=ET.fromstring('<body>'+output+'</body>')
        self.assertIsNotNone(root.find('.//{http://www.w3.org/1998/Math/MathML}math'))

    def test_generated_pages_have_doctype_and_title(self):
        with tempfile.TemporaryDirectory() as directory:
            chapter, _ = get_chapter_XML(directory, "中文测试.md", [], content="# 中文正文")
        pages = {
            "cover": get_coverpage_XML("中文测试", "作者"),
            "navigation": get_TOC_XML([], ["中文测试.md"]),
            "chapter": chapter,
        }
        for name, page in pages.items():
            with self.subTest(page=name):
                self.assertIn("<!DOCTYPE html>", page)
                title = ET.fromstring(page).find("{*}head/{*}title")
                self.assertIsNotNone(title)
                self.assertTrue(title.text)


if __name__ == "__main__":
    unittest.main()
