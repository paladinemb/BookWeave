import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import paddle_converter as c


class StreamHandler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        image=base64.b64decode(body['messages'][0]['content'][1]['image_url']['url'].split(',')[1])
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
        try:
            if image==b'slow':self.server.slow_started.set()
            time.sleep(3 if image==b'slow' else .3)
            event={'choices':[{'delta':{'content':'English 中文'},'finish_reason':'stop'}]}
            self.wfile.write(('data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n').encode());self.wfile.flush()
        except (BrokenPipeError,ConnectionResetError): pass


@contextmanager
def stream_server():
    server=ThreadingHTTPServer(('127.0.0.1',0),StreamHandler)
    server.slow_started=threading.Event()
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield server
    finally:server.shutdown();server.server_close();thread.join()


def write_scan(path,count,scans=None):
    import pypdfium2 as p
    source=p.PdfDocument(str(ROOT.parent/'fixtures/English-scan.pdf'))
    doc=p.PdfDocument.new()
    for index in range(1,count+1):
        if scans is None or index in scans:doc.import_pages(source,[0])
        else:doc.new_page(200,300).close()
    doc.save(str(path));doc.close();source.close()


class ParallelConversionTest(unittest.TestCase):
    def test_one_request_deadline_does_not_cancel_another_request(self):
        with tempfile.TemporaryDirectory() as d,stream_server() as server:
            root=Path(d);(root/'slow.png').write_bytes(b'slow');(root/'fast.png').write_bytes(b'fast')
            ocr=c.LocalOCR(ROOT,root);ocr.url=f'http://127.0.0.1:{server.server_port}'
            ocr.process=SimpleNamespace(poll=lambda:None)
            started=time.monotonic()
            with patch.object(c,'PAGE_TIMEOUT',1.5),ThreadPoolExecutor(max_workers=2) as executor:
                slow=executor.submit(ocr.read,root/'slow.png')
                self.assertTrue(server.slow_started.wait(3))
                time.sleep(1.3)
                fast=executor.submit(ocr.read,root/'fast.png')
                with self.assertRaisesRegex(TimeoutError,'超时'):slow.result()
                self.assertEqual(fast.result(),'English 中文')
            self.assertLess(time.monotonic()-started,2.8)

    def test_out_of_order_completion_is_cached_and_epub_stays_in_pdf_order(self):
        self.assertTrue(callable(getattr(c,'preferred_parallelism',None)), 'Parallel scheduling missing')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);pdf=root/'book.pdf';write_scan(pdf,301,{1,2,152,153})
            active=0;peak=0;lock=threading.Lock()
            def read(image):
                nonlocal active,peak
                n=int(image.stem.split('-')[1])
                with lock:active+=1;peak=max(peak,active)
                try:
                    if n==1:
                        deadline=time.monotonic()+15
                        while not list((root/'cache').glob('*/page-results/00152.json')) and time.monotonic()<deadline:time.sleep(.02)
                        self.assertEqual(len(list((root/'cache').glob('*/page-results/00152.json'))),1,'Later completed page must be cached before page 1 finishes')
                    else:time.sleep(.05)
                    return f'PAGE_{n}_TEXT'
                finally:
                    with lock:active-=1
            with patch.dict('os.environ',{'BOOKWEAVE_CACHE_DIR':str(root/'cache')}),patch.object(c,'preferred_parallelism',return_value=2),patch.object(c.LocalOCR,'read',side_effect=read):
                epub=c.convert(pdf,root/'out','Book','','en',ROOT)
            self.assertEqual(peak,2)
            text=(epub.parent/'content.md').read_text()
            self.assertEqual(sorted((1,2,152,153),key=lambda n:text.index(f'PAGE_{n}_TEXT')),[1,2,152,153])
            state=json.loads((epub.parent/'任务状态.json').read_text())
            self.assertEqual(state['execution']['max_active_tasks'],2)
            self.assertEqual(state['validation']['status'],'passed')

    def test_resource_pressure_drains_parallel_work_then_uses_one_task(self):
        self.assertTrue(callable(getattr(c,'preferred_parallelism',None)), 'Parallel scheduling missing')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);pdf=root/'book.pdf';write_scan(pdf,301,{1,2,3,152,153})
            active=0;after_limit=0;lock=threading.Lock()
            def read(ocr,image):
                nonlocal active,after_limit
                n=int(image.stem.split('-')[1])
                with lock:
                    active+=1
                    if n in (2,3,153):after_limit=max(after_limit,active)
                try:
                    time.sleep(.25)
                    if n==152:ocr.limit_reason='系统可用内存低于15%'
                    return f'PDF page {n}'
                finally:
                    with lock:active-=1
            with patch.dict('os.environ',{'BOOKWEAVE_CACHE_DIR':str(root/'cache')}),patch.object(c,'preferred_parallelism',return_value=2),patch.object(c.LocalOCR,'read',autospec=True,side_effect=read):
                epub=c.convert(pdf,root/'out','Book','','en',ROOT)
            self.assertEqual(after_limit,1)
            report=json.loads((epub.parent/'转换报告.json').read_text())
            self.assertEqual(report['execution']['final_parallelism'],1)
            self.assertTrue(report['execution']['resource_events'])
            self.assertEqual(report['review_pages'],[])

    def test_small_pdf_keeps_one_task_even_on_a_capable_machine(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);pdf=root/'small.pdf';write_scan(pdf,2)
            active=0;peak=0;lock=threading.Lock()
            def read(image):
                nonlocal active,peak
                with lock:active+=1;peak=max(peak,active)
                try:time.sleep(.1);return 'English 中文'
                finally:
                    with lock:active-=1
            with patch.dict('os.environ',{'BOOKWEAVE_CACHE_DIR':str(root/'cache')}),patch.object(c.LocalOCR,'read',side_effect=read),patch.object(c,'preferred_parallelism',return_value=2):
                epub=c.convert(pdf,root/'out','Book','','en',ROOT)
            self.assertEqual(peak,1)
            state=json.loads((epub.parent/'任务状态.json').read_text())
            self.assertEqual(state['concurrency'],1)

    def test_parallel_interruption_reuses_cached_pages_and_preserves_review_page(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);pdf=root/'book.pdf';write_scan(pdf,301,{1,2,152,153})
            def interrupted(image):
                n=int(image.stem.split('-')[1])
                if n==1:raise TimeoutError('本页识别超时')
                if n==2:raise KeyboardInterrupt()
                time.sleep(.1)
                return f'PDF page {n}'
            with patch.dict('os.environ',{'BOOKWEAVE_CACHE_DIR':str(root/'cache')}),patch.object(c,'preferred_parallelism',return_value=2):
                with patch.object(c.LocalOCR,'read',side_effect=interrupted):
                    with self.assertRaises(KeyboardInterrupt):c.convert(pdf,root/'first','Book','','en',ROOT)
                paused=json.loads((root/'first/book/任务状态.json').read_text())
                self.assertEqual(paused['status'],'paused')
                self.assertIn(paused['completed'],(2,3))
                seen=[]
                def resumed(image):
                    n=int(image.stem.split('-')[1]);seen.append(n)
                    self.assertNotIn(n,(1,152),'Completed pages and review pages must be reused')
                    return f'PDF page {n}'
                with patch.object(c.LocalOCR,'read',side_effect=resumed):
                    epub=c.convert(pdf,root/'resume','Book','','en',ROOT)
            self.assertIn(2,seen)
            self.assertTrue(set(seen)<={2,153})
            report=json.loads((epub.parent/'转换报告.json').read_text())
            self.assertEqual([r['pdf_page'] for r in report['review_pages']],[1])
            self.assertTrue(report['review_pages'][0]['image_preserved'])
            self.assertEqual(report['validation']['status'],'passed')

    def test_resource_policy_is_conservative_when_metrics_are_missing(self):
        self.assertTrue(callable(getattr(c,'resource_limit_reason',None)), 'Resource guard missing')
        normal={'memory_bytes':16*1024**3,'rss_bytes':3*1024**3,'free_percent':50,'cpu_machine_percent':10,'swap_bytes':0}
        self.assertIsNone(c.resource_limit_reason(normal,0,0))
        self.assertTrue(c.resource_limit_reason(None,0,0))
        self.assertTrue(c.resource_limit_reason({**normal,'free_percent':10},0,0))
        self.assertTrue(c.resource_limit_reason({**normal,'rss_bytes':5*1024**3},0,0))
        self.assertTrue(c.resource_limit_reason(normal,0,5))


if __name__=='__main__':unittest.main()
