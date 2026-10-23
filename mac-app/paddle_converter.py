"""BookWeave: local PDF text extraction and PaddleOCR-VL-1.6 via Metal."""
import hashlib
import math
import re
import shutil
import argparse
from contextlib import closing, contextmanager
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

PAGE_TIMEOUT = 90
PREPARATION_TIMEOUT = 60


@contextmanager
def operation_deadline(seconds=PAGE_TIMEOUT):
    def expired(*_):
        raise TimeoutError('本页识别超时，已跳过并保留原图。')
    previous = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    started = time.monotonic()
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(.001, previous_timer[0] - (time.monotonic()-started)), previous_timer[1])


def text_issue(text):
    return '识别文字含异常替代字符，需校对。' if '\ufffd' in text else None


def recognized_text(response):
    choice = response['choices'][0]
    text = choice['message']['content'].strip()
    if choice.get('finish_reason') != 'stop':
        raise ValueError('本页识别输出达到上限，需校对。')
    if not text:
        raise ValueError('本页未识别到文字，已保留页面供检查。')
    if text_issue(text):
        raise ValueError(text_issue(text))
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


def page_markdown(index, result):
    if not result.get('error'):
        return result['markdown']
    reason = result['error'].replace('，已停止，未生成不完整电子书。', '，需校对。')
    pictures = '\n\n'.join(f'![PDF 第 {index+1} 页原图](images/{name})' for name in result['files'])
    return f'> **需校对：PDF 第 {index+1} 页**。{reason}\n\n{pictures}'


def validate_epub(epub, results, total_pages):
    import zipfile
    import posixpath
    import zlib
    from urllib.parse import unquote
    from xml.etree import ElementTree
    if set(results) != set(range(total_pages)):
        raise RuntimeError('校验失败：PDF 页面记录不完整。')
    with zipfile.ZipFile(epub) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('校验失败：EPUB 文件损坏。')
        names = set(archive.namelist())
        if (archive.infolist()[0].filename != 'mimetype' or archive.infolist()[0].compress_type != zipfile.ZIP_STORED
                or archive.read('mimetype') != b'application/epub+zip'):
            raise RuntimeError('校验失败：EPUB 格式标记不正确。')
        for name in names:
            if name.endswith(('.xml', '.xhtml', '.opf', '.ncx')):
                try:
                    root = ElementTree.fromstring(archive.read(name))
                except ElementTree.ParseError as failure:
                    raise RuntimeError(f'校验失败：{name} 的正文结构异常。') from failure
                for node in root.iter():
                    ref = node.attrib.get('src') or (node.attrib.get('href') if node.tag.endswith('item') else None)
                    if ref and not ref.startswith(('http:', 'https:', 'data:')):
                        target = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(ref.split('#')[0])))
                        if target not in names:
                            raise RuntimeError(f'校验失败：缺少图片或资源 {ref}。')
        images = {name for result in results.values() for name in result['files']}
        for name in images:
            target = 'OPS/images/' + name
            source = epub.parent/'images'/name
            if target not in names or not source.is_file():
                raise RuntimeError(f'校验失败：缺少图片 {name}。')
            from PIL import Image
            with Image.open(source) as picture:
                copied_without_changes = source.suffix.lower() == '.png' and max(picture.size) <= 1800
            if copied_without_changes and archive.getinfo(target).CRC != zlib.crc32(source.read_bytes()):
                raise RuntimeError(f'校验失败：图片 {name} 与原图不一致。')
        if (epub.parent/'content.md').read_text() != '\n\n---\n\n'.join(page_markdown(i, results[i]) for i in range(total_pages)):
            raise RuntimeError('校验失败：PDF 页序与汇总正文不一致。')
        chapter = ElementTree.fromstring(archive.read('OPS/s00000-content.xhtml'))
        actual_images = [Path(node.attrib['src']).name for node in chapter.iter() if node.tag.endswith('img')]
        if actual_images != [name for i in range(total_pages) for name in results[i]['files']]:
            raise RuntimeError('校验失败：图片顺序与 PDF 页序不一致。')
    return {'status': 'passed', 'pages': total_pages, 'images': len(images)}


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
                if not result.get('error') and text_issue(result.get('raw_text', result['markdown'])):
                    result['error'] = text_issue(result.get('raw_text', result['markdown']))
                    result['error_type'] = 'ocr_quality'
                    atomic_json(path, result)
                if retry_failed and result.get('error'):
                    continue
                if all((cache / 'images' / name).is_file() for name in result['files']):
                    results[index] = result
            except (OSError, ValueError, KeyError, TypeError):
                continue
        state = {'source': str(pdf), 'source_sha256': digest.hexdigest(), 'cache': str(cache),
                 'total_pages': count, 'segments': [[a+1,b] for a,b in segments],
                 'completed': len(results), 'cache_hits': len(results), 'status': 'running', 'version': CACHE_VERSION,
                 'stage': 'planning', 'current_page': None, 'current_part': None, 'page_started_at': time.time()}
        started = time.monotonic()
        def status(value, **changes):
            state.update(changes)
            state.update(status=value, completed=len(results), elapsed_seconds=round(time.monotonic()-started,1),
                         review_pages=[i+1 for i in sorted(results) if results[i].get('error')])
            progress = []
            for number, (first, last) in enumerate(segments, 1):
                completed = sum(i in results for i in range(first, last))
                reviews = sum(bool(results[i].get('error')) for i in range(first, last) if i in results)
                part_status = ('review' if reviews else 'complete') if completed == last-first else 'waiting'
                if state['current_part'] == number and completed < last-first:
                    part_status = 'paused' if value == 'paused' else 'processing'
                progress.append({'number': number, 'first': first+1, 'last': last, 'completed': completed,
                                 'review_count': reviews, 'status': part_status})
            state['segment_progress'] = progress
            atomic_json(folder / '任务状态.json', state)
            atomic_json(cache / 'job.json', state)
        status('running')
        print(f'正在分段：共 {count} 页，{len(segments)} 段；已缓存 {len(results)} 页。', flush=True)
        missing = iter(i for i in range(count) if i not in results)
        pool = None
        pending = {}
        ocr = LocalOCR(resources, folder)
        live = folder / '转换中.md'
        live.write_text('', encoding='utf-8')
        def append_live(index, result):
            with live.open('a', encoding='utf-8') as document:
                document.write(('\n\n---\n\n' if index else '') + page_markdown(index, result))
        def new_pool():
            return multiprocessing.get_context('spawn').Pool(processes=1, initializer=initialize_preparation,
                initargs=(str(pdf), str(cache), decorations))
        def submit_next():
            index = next(missing, None)
            if index is not None:
                pending[index] = pool.apply_async(prepare_page, (index,))
        def restart_preparation():
            nonlocal pool, missing
            remaining = sorted(pending) + list(missing)
            pool.terminate(); pool.join()
            pending.clear()
            missing = iter(remaining)
            pool = new_pool()
            submit_next(); submit_next()
        try:
            if len(results) < count:
                # PDFium runs in its own process: it is unsafe to render PDF pages in concurrent threads.
                pool = new_pool()
                submit_next()
                submit_next()
            for part, (first, last) in enumerate(segments, 1):
                for index in range(first, last):
                    if index in results:
                        append_live(index, results[index])
                        continue
                    page_started = time.monotonic()
                    status('running', stage='preparing', current_part=part, current_page=index+1, page_started_at=time.time())
                    print(f'正在处理第 {part}/{len(segments)} 段，第 {index+1}/{count} 页', flush=True)
                    error = None
                    error_type = None
                    try:
                        prepared = pending.pop(index).get(timeout=PREPARATION_TIMEOUT)
                        submit_next()
                    except Exception as failure:
                        if isinstance(failure, OSError) and failure.errno in (13, 28):
                            raise
                        error = '页面渲染超时' if isinstance(failure, multiprocessing.TimeoutError) else f'页面渲染异常：{failure}'
                        error_type = 'render_error'
                        restart_preparation()
                        image = cache/'images'/f'page-{index+1:05d}.png'
                        pictures = ''
                        if image.is_file():
                            from PIL import Image
                            try:
                                with Image.open(image) as source:
                                    source.verify()
                                pictures = f'\n\n![第 {index+1} 页原图](images/{image.name})'
                            except (OSError, ValueError):
                                pass
                        prepared = {'text': '', 'blank': False, 'scanned': False, 'pictures': pictures}
                        print(f'第 {index+1} 页待校对，自动跳过：{error}', flush=True)
                    if error:
                        text = ''
                    elif prepared['blank']:
                        text = '[空白页]'
                    elif prepared['scanned']:
                        status('running', stage='recognizing')
                        try:
                            with operation_deadline():
                                text = ocr.read(Path(prepared['image']))
                        except (ValueError, OSError) as failure:
                            error = str(failure)
                            error_type = 'ocr_timeout' if isinstance(failure, TimeoutError) else 'ocr_error'
                            text = ''
                            ocr.close()
                            print(f'第 {index+1} 页待校对，已保留原图并继续：{error}', flush=True)
                    else:
                        text = prepared['text']
                    raw_text = text.replace('\r\n','\n')
                    text = raw_text + prepared['pictures']
                    files = re.findall(r'!\[[^\]]*\]\(images/([^/)]+)\)', text)
                    result = {'markdown': text, 'raw_text': raw_text, 'files': files, 'error': error,
                              'error_type': error_type, 'seconds': round(time.monotonic()-page_started,2)}
                    atomic_json(cache / 'page-results' / f'{index+1:05d}.json', result)
                    results[index] = result
                    append_live(index, result)
                    status('running')
                    print(f'已完成 {index+1}/{count} 页，本页 {result["seconds"]:.1f} 秒', flush=True)
                (folder / f'part-{part:02d}.md').write_text('\n\n---\n\n'.join(page_markdown(i, results[i]) for i in range(first,last)), encoding='utf-8')
                ocr.close()
                if part < len(segments) and pool is not None and len(results) < count:
                    restart_preparation()
                print(f'第 {part}/{len(segments)} 段已保存，自动继续。', flush=True)
            status('assembling', stage='packaging')
        except BaseException:
            status('paused')
            raise
        finally:
            ocr.close()
            if pool:
                pool.terminate(); pool.join()
        shutil.copyfile(live, folder / 'content.md')
        (folder / 'images').mkdir(exist_ok=True)
        for name in {name for result in results.values() for name in result['files']}:
            shutil.copyfile(cache / 'images' / name, folder / 'images' / name)
        reviews = [{'pdf_page': i+1, 'reason': results[i]['error'].replace('，已停止，未生成不完整电子书。', '，需校对。'),
                    'type': results[i].get('error_type') or 'review', 'image_preserved': bool(results[i]['files'])}
                   for i in sorted(results) if results[i].get('error')]
        warnings = [f'PDF 第 {r["pdf_page"]} 页：{r["reason"]} ' + ('已保留原图。' if r['image_preserved'] else '未能提取原图，请对照原 PDF。') for r in reviews]
        (folder / '需校对页面.txt').write_text('\n'.join(warnings), encoding='utf-8')
        print('正在汇总分段并生成 EPUB…', flush=True)
        try:
            epub = package_book(folder, pdf, title, author, language, resources)
            status('validating', stage='validating')
            print('正在校验 PDF 页数、图片和 EPUB 结构…', flush=True)
            validation = validate_epub(epub, results, count)
            report = {'total_pages': count, 'normal_pages': count-len(reviews), 'review_pages': reviews,
                      'segments': state['segments'], 'validation': validation}
            atomic_json(folder/'转换报告.json', report)
            (folder/'转换报告.md').write_text(f'# 转换报告\n\nPDF 共 {count} 页，分 {len(segments)} 段，正常处理 {count-len(reviews)} 页，需校对 {len(reviews)} 页。\n\n'
                '页码均指原 PDF 物理页，从封面第 1 页起。异常页已自动跳过，不会阻止后续页面处理。\n\n'
                + ('\n'.join('- '+warning for warning in warnings) if warnings else '未检测到处理异常。')
                + f'\n\n自动校验通过：{count} 条页面记录、{validation["images"]} 张图片及 EPUB 结构完整。\n', encoding='utf-8')
        except BaseException:
            status('packaging_failed')
            raise
        status('complete', stage='complete', validation=validation)
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
