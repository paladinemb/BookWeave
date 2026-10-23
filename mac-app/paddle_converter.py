"""BookWeave: local PDF text extraction and PaddleOCR-VL-1.6 via Metal."""
import hashlib
import math
import re
import shutil
import argparse
from contextlib import closing
import base64
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request


def recognized_text(response):
    choice = response['choices'][0]
    text = choice['message']['content'].strip()
    if choice.get('finish_reason') != 'stop':
        raise ValueError('本页识别输出达到上限，需校对。')
    if not text:
        raise ValueError('本页未识别到文字，已保留页面供检查。')
    return text


def page_segments(count):
    if count <= 0:
        raise ValueError('PDF 没有页面。')
    parts = math.ceil(count / 300)
    size, extra = divmod(count, parts)
    boundaries = [0]
    for i in range(parts):
        boundaries.append(boundaries[-1] + size + (i < extra))
    return list(zip(boundaries, boundaries[1:]))


def consume_ocr_stream(lines):
    text = ''
    finish = None
    checked = 0
    for line in lines:
        if not line.startswith(b'data: '):
            continue
        data = line[6:].strip()
        if data == b'[DONE]':
            break
        response = json.loads(data)
        if response.get('error'):
            raise ValueError(str(response['error']))
        if not response.get('choices'):
            continue
        choice = response['choices'][0]
        text += choice.get('delta', {}).get('content') or ''
        tail = text[-2048:]
        # Long uninterrupted runs are generation failures; ordinary repeated table cells are allowed.
        if re.search(r'(\S)\1{127,}', tail):
            raise ValueError('检测到异常重复输出，已提前停止本页识别。')
        if len(text) - checked >= 64 or choice.get('finish_reason'):
            checked = len(text)
            for repeated in re.finditer(r'(.{32,160}?)\1{7,}', tail, re.DOTALL):
                if sum(char.isalpha() for char in repeated.group(1)) >= 16:
                    raise ValueError('检测到异常重复段落，已提前停止本页识别。')
        if choice.get('finish_reason'):
            finish = choice['finish_reason']
    return recognized_text({'choices': [{'finish_reason': finish, 'message': {'content': text}}]})


class LocalOCR:
    def __init__(self, resources, folder):
        self.resources = resources
        self.folder = folder
        self.process = None
        self.log = None

    def start(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        engine = self.resources / 'paddle-engine'
        models = engine / 'models'
        self.log = (self.folder / '识别引擎记录.txt').open('a')
        self.process = subprocess.Popen([
            str(engine / 'runtime/llama-b11814/llama-server'),
            '-m', str(models / 'PaddleOCR-VL-1.6-GGUF.gguf'),
            '--mmproj', str(models / 'PaddleOCR-VL-1.6-GGUF-mmproj.gguf'),
            '--host', '127.0.0.1', '--port', str(port),
            '-ngl', '99', '-c', '16384', '--parallel', '1', '--temp', '0',
        ], stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(180):
            if self.process.poll() is not None:
                raise RuntimeError('识别引擎启动失败，请查看识别引擎记录。')
            try:
                with urllib.request.urlopen(self.url + '/health', timeout=1) as r:
                    if r.status == 200:
                        return
            except OSError:
                time.sleep(1)
        raise RuntimeError('识别引擎启动超时。')

    def read(self, image):
        if self.process is None:
            print('正在加载 PaddleOCR-VL-1.6（Apple GPU）…', flush=True)
            self.start()
        if isinstance(image, Path):
            data = image.read_bytes()
        else:
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            data = buffer.getvalue()
        encoded = base64.b64encode(data).decode('ascii')
        payload = {'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'OCR:'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + encoded}},
        ]}], 'repeat_penalty': 1.1, 'temperature': 0, 'max_tokens': 8192, 'stream': True}
        request = urllib.request.Request(self.url + '/v1/chat/completions',
            data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=300) as response:
            return consume_ocr_stream(response)

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.log:
            self.log.close()
        self.process = None
        self.log = None


def margin_picture_signature(page, obj):
    import hashlib
    width, height = page.get_size()
    left, bottom, right, top = obj.get_pos()
    area = max(0, right-left) * max(0, top-bottom)
    at_margin = (top <= height*.15 or bottom >= height*.85 or
                 right <= width*.1 or left >= width*.9)
    if not at_margin or area > width*height*.02:
        return None
    bitmap = obj.get_bitmap()
    image = bitmap.to_pil()
    signature = hashlib.sha256(image.convert('RGBA').tobytes()).hexdigest()
    image.close()
    bitmap.close()
    return signature


def repeated_decorations(doc):
    import pypdfium2 as pdfium
    from collections import Counter
    counts = Counter()
    for index in range(len(doc)):
        with closing(doc[index]) as page:
            signatures = {margin_picture_signature(page, obj) for obj in
                page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE], max_depth=8)}
            counts.update(signatures - {None})
    return {key for key, count in counts.items() if count >= max(3, len(doc)*.1)}


def preserve_pictures(page, folder, number, scanned, decorations=frozenset()):
    import pypdfium2 as pdfium
    import shutil
    images = folder / 'images'
    images.mkdir(exist_ok=True)
    objects = list(page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE], max_depth=8))
    paths = list(page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_PATH], max_depth=8))
    references = []
    # Complex vector art needs the original page to retain labels and overlapping shapes.
    if scanned or len(paths) >= 10:
        name = f'page-{number:05d}.png'
        source = folder / 'pages' / name
        if (images / name).exists():
            pass
        elif source.exists():
            shutil.copy(source, images / name)
        else:
            bitmap = page.render(scale=min(2.0, 1800 / max(page.get_size())))
            image = bitmap.to_pil()
            image.save(images / name)
            image.close()
            bitmap.close()
        return f'\n\n![第 {number} 页原图](images/{name})'
    for index, obj in enumerate(objects):
        if decorations and margin_picture_signature(page, obj) in decorations:
            continue
        name = f'page-{number:05d}-image-{index+1:03d}.png'
        bitmap = obj.get_bitmap(render=True)
        image = bitmap.to_pil()
        image.save(images / name)
        image.close()
        bitmap.close()
        references.append(f'![第 {number} 页图片 {index+1}](images/{name})')
    return ('\n\n' + '\n\n'.join(references)) if references else ''



CACHE_VERSION = 'paddleocr-vl-1.6-b11814-batched-v1'


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def initialize_preparation(pdf, cache, decorations):
    import pypdfium2 as pdfium
    global preparation_doc, preparation_cache, preparation_decorations
    preparation_doc = pdfium.PdfDocument(pdf)
    preparation_cache = Path(cache)
    preparation_decorations = decorations


def prepare_page(index):
    folder = preparation_cache
    with closing(preparation_doc[index]) as page:
        with closing(page.get_textpage()) as tp:
            text = tp.get_text_bounded().strip()
        scanned = sum(c.isalnum() for c in text) < 30
        blank = False
        image_path = folder / 'images' / f'page-{index+1:05d}.png'
        if scanned:
            bitmap = page.render(scale=min(2.0, 1800 / max(page.get_size())))
            image = bitmap.to_pil()
            gray = image.convert('L')
            histogram = gray.histogram()
            blank = sum(histogram[:200]) / (gray.width * gray.height) < 0.001
            gray.close()
            image.save(image_path)
            image.close()
            bitmap.close()
        pictures = '' if blank else preserve_pictures(page, folder, index+1, scanned, preparation_decorations)
    return {'text': text, 'scanned': scanned, 'blank': blank, 'image': str(image_path), 'pictures': pictures}


def convert(pdf, output, title, author, language, resources, retry_failed=False):
    import pypdfium2 as pdfium
    import fcntl
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing
    pdf, output, resources = Path(pdf), Path(output), Path(resources)
    if not pdf.is_file() or pdf.suffix.lower() != '.pdf':
        raise ValueError('请选择可读取的 PDF 文件。')
    folder = output / pdf.stem
    folder.mkdir(parents=True, exist_ok=False)
    digest = hashlib.sha256()
    with pdf.open('rb') as source:
        for block in iter(lambda: source.read(1024*1024), b''):
            digest.update(block)
    key = hashlib.sha256((digest.hexdigest()+CACHE_VERSION).encode()).hexdigest()
    cache_root = Path(os.environ.get('BOOKWEAVE_CACHE_DIR', str(Path.home()/'Library/Application Support/BookWeave/cache')))
    cache = cache_root / key
    (cache / 'images').mkdir(parents=True, exist_ok=True)
    (cache / 'page-results').mkdir(exist_ok=True)
    with (cache / 'job.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('同一份 PDF 已在转换中，请等待当前任务完成。')
        with closing(pdfium.PdfDocument(str(pdf))) as doc:
            count = len(doc)
            segments = page_segments(count)
            decoration_file = cache / 'decorations.json'
            if decoration_file.exists():
                decorations = set(json.loads(decoration_file.read_text()))
            else:
                print('正在检查重复页边装饰…', flush=True)
                decorations = repeated_decorations(doc)
                atomic_json(decoration_file, sorted(decorations))
        results = {}
        for index in range(count):
            path = cache / 'page-results' / f'{index+1:05d}.json'
            try:
                result = json.loads(path.read_text())
                if retry_failed and result.get('error'):
                    continue
                if all((cache / 'images' / name).is_file() for name in result['files']):
                    results[index] = result
            except (OSError, ValueError, KeyError, TypeError):
                continue
        state = {'source': str(pdf), 'source_sha256': digest.hexdigest(), 'cache': str(cache),
                 'total_pages': count, 'segments': [[a+1,b] for a,b in segments],
                 'completed': len(results), 'cache_hits': len(results), 'status': 'running', 'version': CACHE_VERSION}
        started = time.monotonic()
        def status(value):
            state.update(status=value, completed=len(results), elapsed_seconds=round(time.monotonic()-started,1),
                         review_pages=[i+1 for i in sorted(results) if results[i].get('error')])
            atomic_json(folder / '任务状态.json', state)
            atomic_json(cache / 'job.json', state)
        status('running')
        print(f'正在分段：共 {count} 页，{len(segments)} 段；已缓存 {len(results)} 页。', flush=True)
        missing = iter(i for i in range(count) if i not in results)
        pool = None
        pending = {}
        ocr = LocalOCR(resources, folder)
        def submit_next():
            index = next(missing, None)
            if index is not None:
                pending[index] = pool.submit(prepare_page, index)
        try:
            if len(results) < count:
                # PDFium runs in its own process: it is unsafe to render PDF pages in concurrent threads.
                pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context('spawn'),
                    initializer=initialize_preparation, initargs=(str(pdf), str(cache), decorations))
                submit_next()
                submit_next()
            for part, (first, last) in enumerate(segments, 1):
                for index in range(first, last):
                    if index in results:
                        continue
                    page_started = time.monotonic()
                    print(f'正在处理第 {part}/{len(segments)} 段，第 {index+1}/{count} 页', flush=True)
                    prepared = pending.pop(index).result()
                    submit_next()
                    error = None
                    if prepared['blank']:
                        text = '[空白页]'
                    elif prepared['scanned']:
                        try:
                            try:
                                text = ocr.read(Path(prepared['image']))
                            except OSError:
                                ocr.close()
                                text = ocr.read(Path(prepared['image']))
                        except (ValueError, OSError) as failure:
                            error = str(failure)
                            text = ''
                            print(f'第 {index+1} 页待校对，已保留原图并继续：{error}', flush=True)
                    else:
                        text = prepared['text']
                    raw_text = text.replace('\r\n','\n')
                    text = raw_text + prepared['pictures']
                    files = re.findall(r'!\[[^\]]*\]\(images/([^/)]+)\)', text)
                    result = {'markdown': text, 'raw_text': raw_text, 'files': files, 'error': error,
                              'seconds': round(time.monotonic()-page_started,2)}
                    atomic_json(cache / 'page-results' / f'{index+1:05d}.json', result)
                    results[index] = result
                    status('running')
                    print(f'已完成 {index+1}/{count} 页，本页 {result["seconds"]:.1f} 秒', flush=True)
                (folder / f'part-{part:02d}.md').write_text('\n\n---\n\n'.join(results[i]['markdown'] for i in range(first,last)), encoding='utf-8')
            status('assembling')
        except BaseException:
            status('paused')
            raise
        finally:
            ocr.close()
            if pool:
                pool.shutdown(wait=True, cancel_futures=True)
        (folder / 'content.md').write_text('\n\n---\n\n'.join(results[i]['markdown'] for i in range(count)), encoding='utf-8')
        (folder / 'images').mkdir(exist_ok=True)
        for name in {name for result in results.values() for name in result['files']}:
            shutil.copyfile(cache / 'images' / name, folder / 'images' / name)
        warnings = [f'第 {i+1} 页：{results[i]["error"].replace("，已停止，未生成不完整电子书。", "，需校对。")} 已保留原图。' for i in sorted(results) if results[i].get('error')]
        (folder / '需校对页面.txt').write_text('\n'.join(warnings), encoding='utf-8')
        print('正在汇总分段并生成 EPUB…', flush=True)
        try:
            epub = package_book(folder, pdf, title, author, language, resources)
        except BaseException:
            status('packaging_failed')
            raise
        status('complete')
        print(f'转换完成：{count} 页，{len(segments)} 段，待校对 {len(warnings)} 页，共 {state["elapsed_seconds"]} 秒。', flush=True)
        return epub


def package_book(folder, pdf, title, author, language, resources):
    sys.path.insert(0, str(resources / 'pdf2epub'))
    from modules import mark2epub
    (folder / 'description.json').write_text(json.dumps({'metadata': {
        'dc:title': title, 'dc:creator': author or 'Unknown Author', 'dc:language': language,
        'dc:publisher': 'BookWeave'}, 'chapters': [{'markdown': 'content.md', 'css': ''}]}, ensure_ascii=False), encoding='utf-8')
    epub = folder / (pdf.stem + '.epub')
    old_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO('\n'*7 + 'n\n')
        mark2epub.main([str(folder), str(epub)])
    finally:
        sys.stdin = old_stdin
    if not epub.is_file():
        raise RuntimeError('EPUB 打包失败，中间文件已保留。')
    # The reused packager defaults chapter XHTML to English; honor the book language.
    import zipfile
    import re
    temporary = epub.with_suffix('.tmp')
    with zipfile.ZipFile(epub) as src, zipfile.ZipFile(temporary, 'w') as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.endswith('.xhtml'):
                data = re.sub(rb'lang="en"', f'lang="{language}"'.encode(), data)
            dst.writestr(item, data)
    temporary.replace(epub)
    print(f'转换完成：{epub}', flush=True)
    return epub


def main():
    parser = argparse.ArgumentParser(description='BookWeave: PDF to EPUB using PaddleOCR-VL-1.6')
    parser.add_argument('pdf', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--title', default='')
    parser.add_argument('--author', default='')
    parser.add_argument('--language', choices=['zh', 'en'], default='zh')
    args = parser.parse_args()
    if not args.title:
        answers = sys.stdin.read().splitlines()
        if len(answers) >= 4:
            args.title, args.author, _, args.language = answers[:4]
    if args.language not in ('zh', 'en'):
        parser.error('Unsupported language')
    if os.getpgrp() != os.getpid():
        os.setsid()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
    convert(args.pdf, args.output, args.title or args.pdf.stem, args.author, args.language, Path(__file__).resolve().parent, args.retry_failed)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'转换失败：{e}', file=sys.stderr, flush=True)
        sys.exit(1)
