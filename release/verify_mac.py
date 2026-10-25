"""Cold text/scan conversion using the exact app that will be distributed."""
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
RESOURCES=Path(sys.argv[1]).resolve()/'Contents/Resources'
sys.path.insert(0,str(RESOURCES))
import paddle_converter as c

if __name__=='__main__':
    output=ROOT/'build/mac-install-test';output.mkdir(parents=True,exist_ok=True)
    os.environ['BOOKWEAVE_CACHE_DIR']=str(output/'cache')
    for name in ('English-source.pdf','Install-scan.pdf'):
        epub=c.convert(ROOT/'fixtures'/name,output/name.replace('.pdf',''),'BookWeave install test','','en',RESOURCES)
        report=json.loads((epub.parent/'转换报告.json').read_text())
        assert report['validation']['status']=='passed' and not report['review_pages'],report
        if name=='Install-scan.pdf':
            text=(epub.parent/'content.md').read_text(encoding='utf-8')
            assert '314159' in text and 'BookWeave' in text,text
    print('PASS: distributed app runtime, text PDF, real scan OCR and EPUB validation')
