"""Windows/Linux desktop entry point; conversion stays in the shared backend."""
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback
import uuid

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'mac-app'))
import paddle_converter as converter


def open_path(path):
    if sys.platform == 'win32':
        os.startfile(str(path))
    else:
        subprocess.Popen(['xdg-open', str(path)])


def worker_command(log, args):
    command = [sys.executable]
    if not getattr(sys, 'frozen', False):
        command += [str(Path(__file__).resolve())]
    return command + ['--convert', str(log), *map(str,args)]


def launch_window():
    import tkinter as tk
    from tkinter import ttk, filedialog
    root = tk.Tk(className='BookWeave'); root.title('BookWeave'); root.geometry('900x700'); root.minsize(800,620)
    body = ttk.Frame(root,padding=22); body.pack(fill='both',expand=True)
    ttk.Label(body,text='BookWeave',font=('',24,'bold')).pack(anchor='w')
    ttk.Label(body,text='在本机转换中英文 PDF，保留图片，生成可编辑的 EPUB。').pack(anchor='w',pady=(4,18))
    form = ttk.Frame(body); form.pack(fill='x'); form.columnconfigure(1,weight=1)
    source=tk.StringVar(); title=tk.StringVar(); author=tk.StringVar(); language=tk.StringVar(value='中文')
    output=tk.StringVar(value=str(Path.home()/'Documents/BookWeave/转换结果'))
    status=tk.StringVar(value='选择 PDF 后开始。大文件自动分段，异常页自动标注并继续。')
    stats=tk.StringVar(); active=tk.StringVar(); resource=tk.StringVar()
    process=None; job=None; stopped=False; page_count=0; log_handle=None
    controls=[]
    def render_segments(state):
        table.delete(*table.get_children())
        for part in state.get('segment_progress',[]):
            labels={'waiting':'等待','processing':'处理中','complete':'已完成','review':'已完成 · 需校对','paused':'已暂停'}
            table.insert('', 'end', values=(part['number'],f"PDF 第 {part['first']}–{part['last']} 页",
                f"{part['completed']}/{part['last']-part['first']+1}",labels.get(part['status'],part['status']),part['review_count']))
    def choose_pdf():
        nonlocal page_count
        path=filedialog.askopenfilename(filetypes=[('PDF','*.pdf')])
        if not path:return
        source.set(path);title.set(Path(path).stem)
        try:
            import pypdfium2 as p
            with converter.closing(p.PdfDocument(path)) as doc:page_count=len(doc)
            segments=converter.page_segments(page_count)
            render_segments({'segment_progress':[{'number':i,'first':a+1,'last':b,'completed':0,'status':'waiting','review_count':0} for i,(a,b) in enumerate(segments,1)]})
            stats.set(f'共 {page_count} 页，{len(segments)} 段')
        except Exception as failure:
            status.set(f'无法读取 PDF：{failure}');source.set('')
    def choose_output():
        path=filedialog.askdirectory()
        if path:output.set(path)
    for row,(label,value) in enumerate((('PDF 文件',source),('书名',title),('作者',author),('电子书语言',language),('保存到',output))):
        ttk.Label(form,text=label).grid(row=row,column=0,sticky='w',padx=(0,14),pady=5)
        widget=ttk.Combobox(form,textvariable=value,values=['中文','English'],state='readonly') if row==3 else ttk.Entry(form,textvariable=value)
        widget.grid(row=row,column=1,sticky='ew',pady=5);controls.append(widget)
        if row in (0,4):
            button=ttk.Button(form,text='选择…',command=choose_pdf if row==0 else choose_output)
            button.grid(row=row,column=2,padx=(10,0));controls.append(button)
    ttk.Separator(body).pack(fill='x',pady=15)
    ttk.Label(body,textvariable=stats).pack(anchor='w')
    progress=ttk.Progressbar(body,maximum=100);progress.pack(fill='x',pady=8)
    ttk.Label(body,textvariable=active,wraplength=820).pack(anchor='w')
    table=ttk.Treeview(body,columns=('part','pages','progress','status','reviews'),show='headings',height=6)
    for name,label,width in zip(table['columns'],('分段','原 PDF 页码','进度','状态','需校对'),(50,230,90,170,65)):
        table.heading(name,text=label);table.column(name,width=width,stretch=name=='pages')
    table.pack(fill='both',expand=True,pady=10)
    ttk.Label(body,textvariable=resource,wraplength=820,foreground='#9b5b00').pack(anchor='w')
    ttk.Label(body,textvariable=status,wraplength=820).pack(anchor='w',pady=10)
    buttons=ttk.Frame(body);buttons.pack(fill='x')
    def stop():
        nonlocal stopped
        if process is not None and process.poll() is None:
            stopped=True
            if sys.platform=='win32':
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                try:os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
            status.set('正在停止，已完成页面保留，可再次继续。')
    def start():
        nonlocal process,job,stopped,log_handle
        pdf=Path(source.get())
        if not pdf.is_file() or pdf.suffix.lower()!='.pdf':status.set('请选择可读取的 PDF。');return
        try:
            job=Path(output.get())/(pdf.stem[:60]+'-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
            job.mkdir(parents=True,exist_ok=False)
            log=job/'运行记录.txt';log_handle=log.open('a',encoding='utf-8')
            process=subprocess.Popen(worker_command(log,[pdf,job,'--title',title.get() or pdf.stem,'--author',author.get(),
                '--language','zh' if language.get()=='中文' else 'en']),stdout=log_handle,stderr=subprocess.STDOUT,
                start_new_session=sys.platform!='win32',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            stopped=False
            for widget in controls:widget.configure(state='disabled')
            start_button.configure(state='disabled');stop_button.configure(state='normal')
            status.set('正在自动转换…');resource.set('');poll()
        except Exception as failure:status.set(f'无法开始：{failure}')
    def poll():
        nonlocal process,log_handle
        state={}
        try:
            state=json.loads((job/Path(source.get()).stem/'任务状态.json').read_text(encoding='utf-8'))
            done,total=state['completed'],state['total_pages']
            mode='双分段并行' if state.get('concurrency')==2 else '单任务'
            stats.set(f"已处理 {done}/{total} 页 · {mode} · 复用 {state['cache_hits']} 页 · 需校对 {len(state['review_pages'])} 页")
            progress['value']=100*done/max(total,1);render_segments(state)
            active.set('　|　'.join(f"第 {p['part']} 段 · PDF 第 {p['pdf_page']} 页 · {max(0,int(time.time()-p['started_at']))} 秒" for p in state.get('active_pages',[])))
            resource.set(state.get('resource_message') or '')
        except (OSError,ValueError,KeyError):pass
        if process.poll() is None:root.after(500,poll);return
        code=process.returncode;process=None
        if log_handle:log_handle.close();log_handle=None
        for widget in controls:widget.configure(state='normal')
        controls[4].configure(state='readonly')
        start_button.configure(state='normal');stop_button.configure(state='disabled');active.set('')
        if stopped:status.set('已停止。重新点击开始可复用已完成页面。')
        elif code==0 and state.get('validation',{}).get('status')=='passed':status.set(f"转换完成，自动校验通过，{len(state['review_pages'])} 页需校对。")
        else:status.set(f'转换中断，已完成页面保留。详情见：{job/"运行记录.txt"}')
    def show(kind):
        if not job:return
        folder=job/Path(source.get()).stem
        target=folder if kind=='folder' else folder/('转换报告.md' if kind=='report' else '转换中.md')
        if target.exists():open_path(target)
    start_button=ttk.Button(buttons,text='开始／继续转换',command=start);start_button.pack(side='left',padx=(0,8))
    stop_button=ttk.Button(buttons,text='停止',command=stop,state='disabled');stop_button.pack(side='left',padx=(0,8))
    for label,kind in (('打开结果','folder'),('转换中文档','live'),('转换报告','report')):
        ttk.Button(buttons,text=label,command=lambda k=kind:show(k)).pack(side='left',padx=(0,8))
    def close():stop();root.destroy()
    root.protocol('WM_DELETE_WINDOW',close);return root


def smoke_test(fixtures, output):
    root=launch_window();root.update();root.destroy()
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    os.environ['BOOKWEAVE_CACHE_DIR']=str(output/'cache')
    resources=Path(converter.__file__).resolve().parent
    for name in ('English-source.pdf','English-scan.pdf'):
        epub=converter.convert(Path(fixtures)/name,output/name.replace('.pdf',''),'BookWeave test','','en',resources)
        report=json.loads((epub.parent/'转换报告.json').read_text(encoding='utf-8'))
        assert report['validation']['status']=='passed' and not report['review_pages'],report
    (output/'passed.json').write_text(json.dumps({'ui':'passed','text_pdf':'passed','scan_ocr':'passed'}))


if __name__=='__main__':
    multiprocessing.freeze_support()
    if len(sys.argv)>1 and sys.argv[1]=='--convert':
        with Path(sys.argv[2]).open('a',encoding='utf-8',buffering=1) as log:
            sys.stdout=sys.stderr=log;sys.argv=[sys.argv[0],*sys.argv[3:]]
            try:converter.main()
            except Exception:traceback.print_exc();sys.exit(1)
    elif len(sys.argv)>1 and sys.argv[1]=='--smoke-test':
        with (Path(sys.argv[3]).with_suffix('.log')).open('w',encoding='utf-8',buffering=1) as log:
            sys.stdout=sys.stderr=log
            try:smoke_test(sys.argv[2],sys.argv[3])
            except Exception:traceback.print_exc();sys.exit(1)
    else:launch_window().mainloop()
