"""BookWeave: local PDF text extraction and PaddleOCR-VL-1.6 via Metal."""
import hashlib
import math
import re
import shutil
import argparse
from contextlib import closing, contextmanager
import platform
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from itertools import zip_longest
import http.client
import threading
from urllib.parse import urlsplit
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
CPU_PAGE_TIMEOUT = 300
PREPARATION_TIMEOUT = 60


def default_cache_directory():
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home()/'AppData/Local'))/'BookWeave/cache'
    if sys.platform == 'darwin':
        return Path.home()/'Library/Application Support/BookWeave/cache'
    return Path(os.environ.get('XDG_CACHE_HOME', Path.home()/'.cache'))/'bookweave'


@contextmanager
def job_lock(path):
    with path.open('a+b') as lock:
        if sys.platform == 'win32':
            import msvcrt
            if lock.tell() == 0:
                lock.write(b'0'); lock.flush()
            lock.seek(0)
            acquire = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: (lock.seek(0), msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl
            acquire = lambda: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(lock, fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as failure:
            raise RuntimeError('同一份 PDF 已在转换中，请等待当前任务完成。') from failure
        try:
            yield
        finally:
            release()


def resource_sample(pid=None):
    try:
        def command(*args):
            return subprocess.check_output(args, text=True, timeout=2, stderr=subprocess.DEVNULL)
        memory = int(command('/usr/sbin/sysctl', '-n', 'hw.memsize'))
        cores = int(command('/usr/sbin/sysctl', '-n', 'hw.logicalcpu'))
        free = float(re.search(r'free percentage: (\d+)%', command('/usr/bin/memory_pressure', '-Q')).group(1))
        swap = re.search(r'used = ([\d.]+)([KMG])', command('/usr/sbin/sysctl', '-n', 'vm.swapusage'))
        rss, cpu = command('/bin/ps', '-p', str(pid), '-o', 'rss=,%cpu=').split() if pid else ('0', '0')
        return {'memory_bytes': memory, 'free_percent': free, 'swap_bytes': float(swap[1])*1024**('KMG'.index(swap[2])+1),
                'rss_bytes': int(rss)*1024, 'cpu_machine_percent': float(cpu)/cores}
    except (OSError, ValueError, AttributeError, TypeError, subprocess.SubprocessError):
        return None


def preferred_parallelism():
    sample = resource_sample()
    return 2 if sample and sample['memory_bytes'] >= 12*1024**3 and sample['free_percent'] >= 25 else 1


def resource_limit_reason(sample, initial_swap, high_cpu_samples):
    if sample is None:
        return '无法读取资源占用，自动使用单任务'
    if sample['free_percent'] < 15:
        return '系统可用内存低于 15%'
    if sample['rss_bytes'] > min(4*1024**3, sample['memory_bytes']*.25):
        return '识别引擎内存达到保护上限'
    if sample['swap_bytes']-initial_swap > 256*1024**2:
        return '系统交换空间增加超过 256 MB'
    if high_cpu_samples >= 5:
        return 'CPU 持续高负载'
    return None


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
    def __init__(self, resources, folder, parallelism=1):
        self.resources = resources
        self.folder = folder
        self.process = None
        self.log = None
        self.parallelism = parallelism
        self.use_gpu = sys.platform == 'darwin' and platform.machine() == 'arm64'
        self.limit_reason = None
        self.metrics = None
        self.start_lock = threading.Lock()
        self.stopped = threading.Event()
        self.monitor = None

    def watch_resources(self):
        initial_swap = self.initial_swap
        high_cpu = 0
        while not self.stopped.wait(1):
            sample = resource_sample(self.process.pid)
            self.metrics = sample
            if sample:
                if initial_swap is None:
                    initial_swap = sample['swap_bytes']
                high_cpu = high_cpu+1 if sample['cpu_machine_percent'] > 75 else 0
            reason = resource_limit_reason(sample, initial_swap or 0, high_cpu)
            if self.parallelism == 2 and reason:
                self.limit_reason = reason
                # Stop admission first; only critical pressure interrupts the two active requests.
                if sample and (sample['free_percent'] < 10 or sample['rss_bytes'] > min(5*1024**3, sample['memory_bytes']*.32)):
                    if self.process.poll() is None:
                        self.process.terminate()
                return

    def command(self, port):
        engine = self.resources / 'paddle-engine'
        models = engine / 'models'
        gpu = '99' if self.use_gpu and sys.platform == 'darwin' and platform.machine() == 'arm64' else '0'
        command = [str(engine / ('runtime/llama-b11814/llama-server' + ('.exe' if sys.platform == 'win32' else ''))),
                '-m', str(models / 'PaddleOCR-VL-1.6-GGUF.gguf'),
                '--mmproj', str(models / 'PaddleOCR-VL-1.6-GGUF-mmproj.gguf'),
                '--host', '127.0.0.1', '--port', str(port), '-ngl', gpu,
                '-c', str(16384*self.parallelism), '--parallel', str(self.parallelism), '--temp', '0']
        if gpu == '0':command.append('--no-mmproj-offload')
        return command

    def start(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        self.log = (self.folder / '识别引擎记录.txt').open('a', encoding='utf-8')
        if self.use_gpu:
            try:
                devices = subprocess.check_output([self.command(port)[0], '--list-devices'], stderr=subprocess.STDOUT, text=True, timeout=15)
                self.use_gpu = bool(re.search(r'^\s*MTL\d+:', devices, re.MULTILINE))
                self.log.write(devices); self.log.flush()
            except (OSError, subprocess.SubprocessError):
                self.use_gpu = False
        baseline = resource_sample()
        self.initial_swap = baseline['swap_bytes'] if baseline else None
        if self.stopped.is_set():
            raise OSError('识别引擎已停止。')
        environment = os.environ.copy()
        if sys.platform.startswith('linux'):
            if 'LD_LIBRARY_PATH_ORIG' in environment:
                environment['LD_LIBRARY_PATH'] = environment['LD_LIBRARY_PATH_ORIG']
            else:
                environment.pop('LD_LIBRARY_PATH', None)
        self.process = subprocess.Popen(self.command(port), stdout=self.log, stderr=subprocess.STDOUT,
            env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.monitor = threading.Thread(target=self.watch_resources, daemon=True)
        self.monitor.start()
        for _ in range(90):
            if self.stopped.is_set():
                self.process.terminate()
                self.process.wait(timeout=10)
                raise OSError('识别引擎已停止。')
            if self.process.poll() is not None:
                if self.use_gpu and not self.limit_reason:
                    self.use_gpu = False
                    self.log.write('GPU 启动失败，自动尝试 CPU。\n'); self.log.flush()
                    self.process = subprocess.Popen(self.command(port), stdout=self.log, stderr=subprocess.STDOUT, env=environment)
                    continue
                if self.limit_reason:
                    raise OSError(self.limit_reason)
                raise RuntimeError('识别引擎启动失败，请查看识别引擎记录。')
            try:
                with urllib.request.urlopen(self.url + '/health', timeout=1) as r:
                    if r.status == 200:
                        return
            except OSError:
                time.sleep(1)
        raise RuntimeError('识别引擎启动超时。')

    def read(self, image):
        with self.start_lock:
            if self.stopped.is_set():
                raise OSError('识别引擎已停止。')
            if self.process is None:
                print('正在加载 PaddleOCR-VL-1.6…', flush=True)
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
        endpoint = urlsplit(self.url)
        timeout = PAGE_TIMEOUT if self.use_gpu else CPU_PAGE_TIMEOUT
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=timeout)
        expired = threading.Event()
        transport = None
        def cancel_request():
            expired.set()
            target = transport or connection.sock
            if target is not None:
                try:
                    target.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        timer = threading.Timer(timeout, cancel_request)
        timer.daemon = True
        timer.start()
        try:
            connection.request('POST', '/v1/chat/completions', body=json.dumps(payload).encode(),
                               headers={'Content-Type': 'application/json'})
            transport = connection.sock
            with connection.getresponse() as response:
                if response.status != 200:
                    raise OSError(f'识别引擎返回 HTTP {response.status}')
                text = consume_ocr_stream(response)
            if expired.is_set():
                raise TimeoutError('本页识别超时，已跳过并保留原图。')
            return text
        except (OSError, ValueError, http.client.HTTPException) as failure:
            if expired.is_set() or isinstance(failure, TimeoutError):
                raise TimeoutError('本页识别超时，已跳过并保留原图。') from failure
            if isinstance(failure, http.client.HTTPException):
                raise OSError(f'识别连接中断：{failure}') from failure
            raise
        finally:
            if timer:
                timer.cancel()
            connection.close()

    def close(self):
        self.stopped.set()
        if self.monitor:
            self.monitor.join(timeout=8)
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


def margin_graphics(image):
    from PIL import Image
    gray = image.convert('L')
    gray.thumbnail((900, 900))
    width, height = gray.size
    dx, dy = max(1, int(width*.12)), max(1, int(height*.12))
    candidates = []
    seen = set()
    for side, bounds in [('left', (0, 0, dx, height)), ('right', (width-dx, 0, width, height)),
                         ('top', (0, 0, width, dy)), ('bottom', (0, height-dy, width, height))]:
        band = gray.crop(bounds)
        bw, bh = band.size
        dark = {i for i, value in enumerate(band.tobytes()) if value < 180}
        band.close()
        while dark:
            first = dark.pop()
            pending = [first]
            xs, ys, pixels = first % bw, first // bw, 0
            left = right = xs; top = bottom = ys
            while pending:
                index = pending.pop(); x, y = index % bw, index // bw
                pixels += 1
                left, right, top, bottom = min(left,x), max(right,x), min(top,y), max(bottom,y)
                neighbors = (index-1 if x else -1, index+1 if x+1 < bw else -1,
                             index-bw if y else -1, index+bw if y+1 < bh else -1)
                for neighbor in neighbors:
                    if neighbor in dark:
                        dark.remove(neighbor); pending.append(neighbor)
            w, h = right-left+1, bottom-top+1
            # Small glyphs and thin rules are text, page numbers or chart borders, not decoration evidence.
            if min(w,h) < 4 or (w < width*.035 and h < height*.035) or w*h > width*height*.02 or pixels/(w*h) < .35:
                continue
            if side in ('top', 'bottom') and h < height*.018:
                continue
            box = (left+bounds[0], top+bounds[1], right+bounds[0]+1, bottom+bounds[1]+1)
            if ((side == 'left' and box[2] > width*.105) or (side == 'right' and box[0] < width*.895)
                    or (side == 'top' and box[3] > height*.105) or (side == 'bottom' and box[1] < height*.895) or box in seen):
                continue
            seen.add(box)
            thumb = gray.crop(box).resize((8, 8), Image.Resampling.BOX)
            signature = sum((value < 180) << i for i, value in enumerate(thumb.tobytes()))
            thumb.close()
            candidates.append({'side': side, 'width': w/width, 'height': h/height,
                               'signature': signature, 'box': [box[0]/width, box[1]/height, box[2]/width, box[3]/height]})
    gray.close()
    return candidates


def same_margin_graphic(item, pattern):
    return (item['side'] == pattern['side'] and
            abs(item['width']/pattern['width']-1) < .25 and abs(item['height']/pattern['height']-1) < .25 and
            (item['signature'] ^ pattern['signature']).bit_count() <= 8)


def raster_decorations(doc):
    if len(doc) < 3:
        return []
    # Sample across the book; confirmation needs different pages, never repeated shapes on one page.
    indices = sorted({round(i*(len(doc)-1)/31) for i in range(32)}) if len(doc) > 32 else list(range(len(doc)))
    groups = []
    for index in indices:
        with closing(doc[index]) as page:
            bitmap = page.render(scale=min(1.5, 900/max(page.get_size())))
            image = bitmap.to_pil()
            for candidate in margin_graphics(image):
                group = next((g for g in groups if same_margin_graphic(candidate, g[0])), None)
                if group is None:
                    groups.append((candidate, {index}))
                else:
                    group[1].add(index)
            image.close(); bitmap.close()
    return [pattern for pattern, pages in groups if len(pages) >= max(3, math.ceil(len(indices)*.15))]


def raster_decorations_from_pdf(pdf):
    import pypdfium2 as pdfium
    with closing(pdfium.PdfDocument(pdf)) as doc:
        return raster_decorations(doc)


def scan_raster_decorations(pdf):
    import multiprocessing
    pool = multiprocessing.get_context('spawn').Pool(processes=1)
    try:
        return pool.apply_async(raster_decorations_from_pdf, (str(pdf),)).get(timeout=PREPARATION_TIMEOUT), None
    except Exception as failure:
        reason = '超时' if isinstance(failure, multiprocessing.TimeoutError) else str(failure)
        return [], f'扫描页边装饰检查未完成（{reason}），已保留不确定图形并继续转换。'
    finally:
        pool.terminate(); pool.join()


def remove_margin_decorations(image, patterns):
    from PIL import ImageDraw
    cleaned = image.convert('RGB')
    draw = ImageDraw.Draw(cleaned)
    removed = 0
    if patterns:
        for item in margin_graphics(image):
            if any(same_margin_graphic(item, pattern) for pattern in patterns):
                x0, y0, x1, y1 = item['box']
                draw.rectangle((max(0, int(x0*image.width)-2), max(0, int(y0*image.height)-2),
                                min(image.width-1, math.ceil(x1*image.width)+2), min(image.height-1, math.ceil(y1*image.height)+2)), fill='white')
                removed += 1
    return cleaned, removed


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



CACHE_VERSION = 'paddleocr-vl-1.6-b11814-clean-300dpi-v2'


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(20):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if sys.platform != 'win32' or attempt == 19:
                raise
            time.sleep(.05)


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
        if (epub.parent/'content.md').read_text(encoding='utf-8') != '\n\n---\n\n'.join(page_markdown(i, results[i]) for i in range(total_pages)):
            raise RuntimeError('校验失败：PDF 页序与汇总正文不一致。')
        chapter = ElementTree.fromstring(archive.read('OPS/s00000-content.xhtml'))
        actual_images = [Path(node.attrib['src']).name for node in chapter.iter() if node.tag.endswith('img')]
        if actual_images != [name for i in range(total_pages) for name in results[i]['files']]:
            raise RuntimeError('校验失败：图片顺序与 PDF 页序不一致。')
    return {'status': 'passed', 'pages': total_pages, 'images': len(images)}


def initialize_preparation(pdf, cache, decorations):
    import pypdfium2 as pdfium
    global preparation_doc, preparation_cache, preparation_decorations, preparation_raster_decorations
    preparation_doc = pdfium.PdfDocument(pdf)
    preparation_cache = Path(cache)
    preparation_decorations = decorations
    preparation_raster_decorations = json.loads((preparation_cache/'raster-decorations.json').read_text(encoding='utf-8'))['patterns']


def prepare_page(index):
    folder = preparation_cache
    with closing(preparation_doc[index]) as page:
        with closing(page.get_textpage()) as tp:
            text = tp.get_text_bounded().strip()
        scanned = sum(c.isalnum() for c in text) < 30
        blank = False
        preprocessing = None
        image_path = folder / 'images' / f'page-{index+1:05d}.png'
        if scanned:
            scale = min(300/72, 5000/max(page.get_size()))
            bitmap = page.render(scale=scale)
            source = bitmap.to_pil()
            image, removed = remove_margin_decorations(source, preparation_raster_decorations)
            source.close(); bitmap.close()
            gray = image.convert('L')
            histogram = gray.histogram()
            blank = sum(histogram[:200]) / (gray.width * gray.height) < 0.001
            ink = gray.point(lambda value: 255 if value < 245 else 0)
            box = ink.getbbox()
            ink.close()
            gray.close()
            original_size = image.size
            if box:
                pad = max(8, round(min(image.size)*.01))
                box = (max(0,box[0]-pad), max(0,box[1]-pad), min(image.width,box[2]+pad), min(image.height,box[3]+pad))
                cropped = image.crop(box)
                image.close(); image = cropped
            preprocessing = {'removed_margin_items': removed, 'source_pixels': list(original_size),
                             'crop_box': box, 'ocr_pixels': list(image.size), 'ocr_dpi': round(scale*72)}
            image.save(image_path)
            image.close()
        pictures = '' if blank else preserve_pictures(page, folder, index+1, scanned, preparation_decorations)
    return {'text': text, 'scanned': scanned, 'blank': blank, 'image': str(image_path), 'pictures': pictures,
            'preprocessing': preprocessing}


def convert(pdf, output, title, author, language, resources, retry_failed=False):
    import pypdfium2 as pdfium
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
    cache_root = Path(os.environ.get('BOOKWEAVE_CACHE_DIR', str(default_cache_directory())))
    cache = cache_root / key
    (cache / 'images').mkdir(parents=True, exist_ok=True)
    (cache / 'page-results').mkdir(exist_ok=True)
    with job_lock(cache / 'job.lock'):
        with closing(pdfium.PdfDocument(str(pdf))) as doc:
            count = len(doc)
            segments = page_segments(count)
            decoration_file = cache / 'decorations.json'
            if decoration_file.exists():
                decorations = set(json.loads(decoration_file.read_text(encoding='utf-8')))
            else:
                print('正在检查重复页边装饰…', flush=True)
                decorations = repeated_decorations(doc)
                atomic_json(decoration_file, sorted(decorations))
        results = {}
        for index in range(count):
            path = cache / 'page-results' / f'{index+1:05d}.json'
            try:
                result = json.loads(path.read_text(encoding='utf-8'))
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
        concurrency = preferred_parallelism() if len(segments) > 1 else 1
        execution = {'initial_parallelism': concurrency, 'final_parallelism': concurrency,
                     'max_active_tasks': 0, 'peak_engine_rss_bytes': 0, 'peak_cpu_machine_percent': 0, 'resource_events': []}
        state = {'source': str(pdf), 'source_sha256': digest.hexdigest(), 'cache': str(cache),
                 'total_pages': count, 'segments': [[a+1,b] for a,b in segments],
                 'completed': len(results), 'cache_hits': len(results), 'status': 'running', 'version': CACHE_VERSION,
                 'execution': execution, 'concurrency': concurrency, 'active_pages': [], 'resource_message': None,
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
                if (state['current_part'] == number or any(p['part'] == number for p in state['active_pages'])) and completed < last-first:
                    part_status = 'paused' if value == 'paused' else 'processing'
                progress.append({'number': number, 'first': first+1, 'last': last, 'completed': completed,
                                 'review_count': reviews, 'status': part_status})
            state['segment_progress'] = progress
            atomic_json(folder / '任务状态.json', state)
            atomic_json(cache / 'job.json', state)
        status('running')
        raster_file = cache/'raster-decorations.json'
        if not raster_file.exists():
            status('running', stage='cleaning')
            print('正在检查扫描图中的重复页边装饰…', flush=True)
            patterns, warning = scan_raster_decorations(pdf)
            atomic_json(raster_file, {'patterns': patterns, 'warning': warning})
        preprocessing_warning = json.loads(raster_file.read_text(encoding='utf-8'))['warning']
        if preprocessing_warning:
            print(preprocessing_warning, flush=True)
        print(f'正在分段：共 {count} 页，{len(segments)} 段；已缓存 {len(results)} 页。', flush=True)
        groups = [list(enumerate(segments, 1))[i:i+concurrency] for i in range(0, len(segments), concurrency)]
        waves = []
        for group in groups:
            waves.append([[(part, index) for part, index in zip([p for p, _ in group], indices) if index is not None and index not in results]
                          for indices in zip_longest(*(range(first,last) for _,(first,last) in group))])
        missing = iter(index for group in waves for wave in group for _,index in wave)
        pool = None
        pending = {}
        ocr = LocalOCR(resources, folder, concurrency)
        executor = ThreadPoolExecutor(max_workers=2)
        live = folder / '转换中.md'
        live.write_text('', encoding='utf-8')
        next_live = 0
        def flush_live():
            nonlocal next_live
            with live.open('a', encoding='utf-8') as document:
                while next_live in results:
                    document.write(('\n\n---\n\n' if next_live else '') + page_markdown(next_live, results[next_live]))
                    next_live += 1
        def save_result(index, prepared, text, error, error_type, page_started):
            raw_text = text.replace('\r\n','\n')
            text = raw_text + prepared['pictures']
            result = {'markdown': text, 'raw_text': raw_text,
                      'files': re.findall(r'!\[[^\]]*\]\(images/([^/)]+)\)', text),
                      'error': error, 'error_type': error_type, 'seconds': round(time.monotonic()-page_started,2),
                      'preprocessing': prepared.get('preprocessing')}
            atomic_json(cache / 'page-results' / f'{index+1:05d}.json', result)
            results[index] = result
            state['active_pages'] = [p for p in state['active_pages'] if p['pdf_page'] != index+1]
            flush_live()
            status('running')
            print(f'已处理 {len(results)}/{count} 页（PDF 第 {index+1} 页），本页 {result["seconds"]:.1f} 秒', flush=True)
        def update_resources():
            if ocr.metrics:
                execution['peak_engine_rss_bytes'] = max(execution['peak_engine_rss_bytes'], ocr.metrics['rss_bytes'])
                execution['peak_cpu_machine_percent'] = max(execution['peak_cpu_machine_percent'], ocr.metrics['cpu_machine_percent'])
            if concurrency == 2 and ocr.limit_reason:
                state['resource_message'] = ocr.limit_reason + '，自动改为单任务'
        def adjust_concurrency():
            nonlocal concurrency, ocr
            update_resources()
            if concurrency == 2 and ocr.limit_reason:
                concurrency = 1
                execution['final_parallelism'] = 1
                execution['resource_events'].append(state['resource_message'])
                state['concurrency'] = 1
                print(state['resource_message'], flush=True)
                ocr.close()
                ocr = LocalOCR(resources, folder, 1)
        flush_live()
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
            for group, group_waves in zip(groups, waves):
                saved_parts = set()
                for wave in group_waves:
                    queue = list(wave)
                    while queue:
                        adjust_concurrency()
                        batch, queue = queue[:concurrency], queue[concurrency:]
                        futures = {}
                        restart_ocr = False
                        state['active_pages'] = [{'pdf_page': index+1, 'part': part, 'started_at': time.time(), 'stage': 'preparing'} for part,index in batch]
                        execution['max_active_tasks'] = max(execution['max_active_tasks'], len(batch))
                        for part, index in batch:
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
                            if error or prepared['blank'] or not prepared['scanned']:
                                text = '' if error else '[空白页]' if prepared['blank'] else prepared['text']
                                save_result(index, prepared, text, error, error_type, page_started)
                            else:
                                for page in state['active_pages']:
                                    if page['pdf_page'] == index+1:
                                        page.update(stage='recognizing', started_at=time.time())
                                status('running', stage='recognizing')
                                futures[executor.submit(ocr.read, Path(prepared['image']))] = (index, prepared, page_started)
                        while futures:
                            done, _ = wait(futures, timeout=.25, return_when=FIRST_COMPLETED)
                            update_resources()
                            for future in done:
                                index, prepared, page_started = futures.pop(future)
                                error = error_type = None
                                try:
                                    text = future.result()
                                except (ValueError, OSError) as failure:
                                    error = str(failure)
                                    error_type = 'ocr_timeout' if isinstance(failure, TimeoutError) else 'ocr_error'
                                    text = ''
                                    restart_ocr = True
                                    print(f'PDF 第 {index+1} 页待校对，已保留原图并继续：{error}', flush=True)
                                save_result(index, prepared, text, error, error_type, page_started)
                            status('running')
                        adjust_concurrency()
                        if restart_ocr:
                            update_resources()
                            # Drain both requests before replacing the one shared engine.
                            reason = ocr.limit_reason
                            ocr.close()
                            ocr = LocalOCR(resources, folder, concurrency)
                            ocr.limit_reason = reason
                    for part, (first,last) in group:
                        if part not in saved_parts and all(i in results for i in range(first,last)):
                            (folder / f'part-{part:02d}.md').write_text('\n\n---\n\n'.join(page_markdown(i, results[i]) for i in range(first,last)), encoding='utf-8')
                            saved_parts.add(part)
                            print(f'第 {part}/{len(segments)} 段已保存，自动继续。', flush=True)
                adjust_concurrency()
                ocr.close()
                ocr = LocalOCR(resources, folder, concurrency)
                if group != groups[-1] and pool is not None and len(results) < count:
                    restart_preparation()
            status('assembling', stage='packaging')
        except BaseException:
            status('paused')
            raise
        finally:
            ocr.close()
            executor.shutdown(wait=True, cancel_futures=True)
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
            preprocessing = {'cleaned_pages': sum(bool((r.get('preprocessing') or {}).get('removed_margin_items')) for r in results.values()),
                             'removed_margin_items': sum((r.get('preprocessing') or {}).get('removed_margin_items', 0) for r in results.values()),
                             'warning': preprocessing_warning}
            report = {'total_pages': count, 'normal_pages': count-len(reviews), 'review_pages': reviews,
                      'segments': state['segments'], 'validation': validation, 'preprocessing': preprocessing, 'execution': execution}
            atomic_json(folder/'转换报告.json', report)
            (folder/'转换报告.md').write_text(f'# 转换报告\n\nPDF 共 {count} 页，分 {len(segments)} 段，正常处理 {count-len(reviews)} 页，需校对 {len(reviews)} 页。\n\n'
                '页码均指原 PDF 物理页，从封面第 1 页起。异常页已自动跳过，不会阻止后续页面处理。\n\n'
                + f'已清理 {preprocessing["cleaned_pages"]} 页的 {preprocessing["removed_margin_items"]} 处重复页边装饰；扫描页使用最高 300 DPI 高清输入，参考图已清边并裁去空白。原 PDF 未修改。\n\n'
                + f'分段并行：初始 {execution["initial_parallelism"]} 个任务，结束时 {execution["final_parallelism"]} 个任务。' + ('；'.join(execution['resource_events']) or '未触发资源降级。') + '\n\n'
                + ((preprocessing_warning+'\n\n') if preprocessing_warning else '')
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
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
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
    if sys.platform != 'win32' and os.getpgrp() != os.getpid():
        os.setsid()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
    convert(args.pdf, args.output, args.title or args.pdf.stem, args.author, args.language, Path(__file__).resolve().parent, args.retry_failed)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'转换失败：{e}', file=sys.stderr, flush=True)
        sys.exit(1)
