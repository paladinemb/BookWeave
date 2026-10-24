import AppKit
import SwiftUI
import UniformTypeIdentifiers
import PDFKit

struct SegmentProgress: Decodable, Identifiable {
    let number: Int
    let first: Int
    let last: Int
    let completed: Int
    let reviewCount: Int
    let status: String
    var id: Int { number }
    var count: Int { last - first + 1 }
    var label: String {
        switch status {
        case "processing": return "处理中"
        case "complete": return "已完成"
        case "review": return "已完成 · \(reviewCount) 页需校对"
        case "paused": return "已暂停"
        default: return "等待处理"
        }
    }
}

struct ConversionProgress: Decodable {
    struct Validation: Decodable { let status: String }
    let totalPages: Int
    let completed: Int
    let cacheHits: Int
    let currentPage: Int?
    let currentPart: Int?
    let pageStartedAt: Double?
    let reviewPages: [Int]
    let segmentProgress: [SegmentProgress]
    let stage: String?
    let validation: Validation?
    static func decode(_ data: Data) throws -> ConversionProgress {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(ConversionProgress.self, from: data)
    }
    static func preview(_ count: Int) -> ConversionProgress {
        guard count > 0 else { return .init(totalPages: 0, completed: 0, cacheHits: 0, currentPage: nil, currentPart: nil,
            pageStartedAt: nil, reviewPages: [], segmentProgress: [], stage: nil, validation: nil) }
        let parts = (count + 299) / 300
        var first = 1
        var rows: [SegmentProgress] = []
        for number in 1...parts {
            let size = count / parts + (number <= count % parts ? 1 : 0)
            rows.append(.init(number: number, first: first, last: first+size-1, completed: 0, reviewCount: 0, status: "waiting"))
            first += size
        }
        return .init(totalPages: count, completed: 0, cacheHits: 0, currentPage: nil, currentPart: nil,
            pageStartedAt: nil, reviewPages: [], segmentProgress: rows, stage: "planning", validation: nil)
    }
    var stageLabel: String {
        switch stage {
        case "cleaning": return "正在检查重复页边装饰"
        case "preparing": return "正在清理页边并准备高清页面"
        case "recognizing": return "正在识别文字，异常页会自动跳过"
        case "packaging": return "正在汇总分段并生成 EPUB"
        case "validating": return "正在自动校验页数、图片和电子书结构"
        case "complete": return "全部分段处理完成"
        default: return "自动依次处理各段，无需逐段操作"
        }
    }
}

func metadataAnswers(title: String, author: String, language: String) -> Data {
    func line(_ text: String) -> String {
        text.components(separatedBy: .newlines).joined(separator: " ")
            .unicodeScalars.filter { !CharacterSet.controlCharacters.contains($0) }
            .map(String.init).joined().trimmingCharacters(in: .whitespaces)
    }
    return ([line(title), line(author), UUID().uuidString, language, "", "", "", "n"]
        .joined(separator: "\n") + "\n").data(using: .utf8)!
}

func newOutputFolder(parent: URL, input: URL) throws -> URL {
    try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
    let stem = String(input.deletingPathExtension().lastPathComponent.prefix(60))
    let date = DateFormatter()
    date.dateFormat = "yyyyMMdd-HHmmss"
    let folder = parent.appendingPathComponent("\(stem)-\(date.string(from: Date()))-\(UUID().uuidString.prefix(8))")
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false)
    return folder
}

func selfTest() throws {
    let answers = String(data: metadataAnswers(title: "中文\n书名", author: "作者\r\n姓名", language: "zh"), encoding: .utf8)!
        .split(separator: "\n", omittingEmptySubsequences: false)
    precondition(answers.count == 9 && answers[0] == "中文 书名" && answers[3] == "zh" && answers[7] == "n")
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let pdf = root.appendingPathComponent("中文测试.pdf")
    let first = try newOutputFolder(parent: root, input: pdf)
    let sentinel = first.appendingPathComponent("已编辑.epub")
    try Data("preserve".utf8).write(to: sentinel)
    let second = try newOutputFolder(parent: root, input: pdf)
    precondition(first != second && first.deletingLastPathComponent().path == root.path)
    let preserved = try Data(contentsOf: sentinel)
    precondition(preserved == Data("preserve".utf8))
    let plan = ConversionProgress.preview(1027)
    precondition(plan.segmentProgress.map(\.count) == [257, 257, 257, 256])
    precondition(ConversionProgress.preview(800).segmentProgress.map(\.count) == [267, 267, 266])
    precondition(ConversionProgress.preview(0).segmentProgress.isEmpty)
    let snapshot = try ConversionProgress.decode(Data("""
    {"total_pages":1027,"completed":57,"cache_hits":0,"current_page":58,"current_part":1,"page_started_at":1,
    "review_pages":[57],"stage":"recognizing","segment_progress":[{"number":1,"first":1,"last":257,"completed":57,"review_count":1,"status":"processing"}]}
    """.utf8))
    precondition(snapshot.currentPage == 58 && snapshot.reviewPages == [57] && snapshot.segmentProgress[0].label == "处理中")
    precondition(SegmentProgress(number: 1, first: 1, last: 257, completed: 257, reviewCount: 1, status: "review").label == "已完成 · 1 页需校对")
    print("PASS: metadata, output protection, segment planning and anomaly progress")
}

@MainActor
final class Converter: ObservableObject {
    static let shared = Converter()
    @Published var input: URL?
    @Published var title = ""
    @Published var author = ""
    @Published var language = "zh"
    @Published var progress: ConversionProgress?
    @Published var pageElapsed = 0
    @Published var showLog = false
    @Published var outputParent = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("PDF转EPUB/转换结果")
    @Published var running = false
    @Published var status = "选择一本 PDF，转换成可编辑的 EPUB。"
    @Published var result: URL?
    @Published var folder: URL?
    @Published var log = ""
    private var progressTimer: Timer?
    private var process: Process?
    private var cancelled = false

    func choosePDF() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.pdf]
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.message = "选择要转换的 PDF"
        if panel.runModal() == .OK, let url = panel.url {
            input = url
            title = url.deletingPathExtension().lastPathComponent
            result = nil
            if let count = PDFDocument(url: url)?.pageCount {
                progress = .preview(count)
            } else { progress = nil }
            status = "已选择 PDF，可以开始转换。"
        }
    }

    func chooseOutput() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.message = "选择保存转换结果的文件夹"
        if panel.runModal() == .OK, let url = panel.url { outputParent = url }
    }

    func start() {
        guard !running, let input else { return }
        result = nil
        log = ""
        cancelled = false
        do {
            guard FileManager.default.isReadableFile(atPath: input.path), input.pathExtension.lowercased() == "pdf" else {
                throw NSError(domain: "PDFtoEPUB", code: 1, userInfo: [NSLocalizedDescriptionKey: "无法读取 PDF，请重新选择文件。"])
            }
            let resources = Bundle.main.resourceURL!
            let runFolder = try newOutputFolder(parent: outputParent, input: input)
            folder = runFolder
            let logURL = runFolder.appendingPathComponent("转换记录.txt")
            guard FileManager.default.createFile(atPath: logURL.path, contents: nil) else {
                throw CocoaError(.fileWriteUnknown)
            }
            let output = try FileHandle(forWritingTo: logURL)
            let pipe = Pipe()
            let worker = Process()
            worker.executableURL = resources.appendingPathComponent("python-runtime/bin/python3.12")
            worker.arguments = ["-B", "-u", "-I", resources.appendingPathComponent("paddle_converter.py").path, input.path, runFolder.path]
            worker.currentDirectoryURL = runFolder
            worker.standardInput = pipe
            worker.standardOutput = output
            worker.standardError = output
            var environment = ProcessInfo.processInfo.environment
            environment["MODEL_CACHE_DIR"] = resources.appendingPathComponent("models").path
            environment["PYTHONUNBUFFERED"] = "1"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
            environment["HF_HUB_OFFLINE"] = "1"
            environment["TRANSFORMERS_OFFLINE"] = "1"
            worker.environment = environment
            let stem = input.deletingPathExtension().lastPathComponent
            let epub = runFolder.appendingPathComponent(stem).appendingPathComponent(stem + ".epub")
            worker.terminationHandler = { process in
                try? output.close()
                let code = process.terminationStatus
                let text = (try? String(contentsOf: logURL, encoding: .utf8)) ?? "无法读取运行记录。"
                DispatchQueue.main.async {
                    self.progressTimer?.invalidate()
                    self.progressTimer = nil
                    self.running = false
                    self.process = nil
                    self.log = text
                    self.refreshProgress()
                    if self.cancelled {
                        self.status = "已保存进度；再次转换同一份 PDF 会自动继续。"
                    } else if code == 0 && FileManager.default.fileExists(atPath: epub.path) {
                        self.result = epub
                        let review = (try? String(contentsOf: epub.deletingLastPathComponent().appendingPathComponent("需校对页面.txt"), encoding: .utf8)) ?? ""
                        let reviewCount = review.split(separator: "\n").count
                        self.status = reviewCount == 0 ? "转换完成，自动校验通过。" : "转换完成，\(reviewCount) 页需校对，异常详情已写入转换报告。"
                    } else {
                        self.status = text.components(separatedBy: .newlines).last(where: { $0.hasPrefix("转换失败：") }) ?? "转换异常结束（状态码 \(code)），请查看运行详情。"
                    }
                }
            }
            process = worker
            do { try worker.run() } catch {
                try? output.close()
                process = nil
                throw error
            }
            progressTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
                Task { @MainActor in
                    guard self.running else { return }
                    self.refreshProgress()
                    if self.showLog, let file = try? FileHandle(forReadingFrom: logURL) {
                        defer { try? file.close() }
                        let size = (try? file.seekToEnd()) ?? 0
                        try? file.seek(toOffset: size > 16000 ? size-16000 : 0)
                        if let bytes = try? file.readToEnd() { self.log = String(decoding: bytes, as: UTF8.self) }
                    }
                }
            }
            running = true
            status = "正在识别并生成电子书… 大文件需要较长时间。"
            let bookTitle = title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? stem : title
            pipe.fileHandleForWriting.write(metadataAnswers(title: bookTitle, author: author, language: language))
            try? pipe.fileHandleForWriting.close()
        } catch {
            status = "无法开始转换：\(error.localizedDescription)"
        }
    }

    func refreshProgress() {
        guard let folder, let input else { return }
        let file = folder.appendingPathComponent(input.deletingPathExtension().lastPathComponent).appendingPathComponent("任务状态.json")
        if let bytes = try? Data(contentsOf: file), let value = try? ConversionProgress.decode(bytes) {
            progress = value
            pageElapsed = max(0, Int(Date().timeIntervalSince1970 - (value.pageStartedAt ?? Date().timeIntervalSince1970)))
            if running { status = value.stageLabel }
        }
    }

    func stop() {
        guard let process, process.isRunning else { return }
        cancelled = true
        status = "正在停止转换…"
        // The runner owns a process group so OCR subprocesses stop with it.
        if kill(-process.processIdentifier, SIGTERM) != 0 { process.terminate() }
    }

    func edit() {
        guard let result else { return }
        NSWorkspace.shared.open([result], withApplicationAt: URL(fileURLWithPath: "/Applications/Sigil.app"),
            configuration: NSWorkspace.OpenConfiguration()) { _, error in
            if let error { DispatchQueue.main.async { self.status = "无法打开 Sigil：\(error.localizedDescription)" } }
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var exiting = false
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func allowExit() -> Bool {
        guard Converter.shared.running, !exiting else { return true }
        let alert = NSAlert()
        alert.messageText = "PDF 正在转换"
        alert.informativeText = "退出会停止本次转换，原始 PDF 不受影响。"
        alert.addButton(withTitle: "继续转换")
        alert.addButton(withTitle: "停止并退出")
        if alert.runModal() == .alertSecondButtonReturn {
            exiting = true
            Converter.shared.stop()
            return true
        }
        return false
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        allowExit() ? .terminateNow : .terminateCancel
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { allowExit() }
}

@main
struct PDFtoEPUBApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model = Converter.shared
    init() {
        if CommandLine.arguments.contains("--self-test") {
            do { try selfTest(); exit(0) } catch { print(error); exit(1) }
        }
    }
    var body: some Scene {
        Window("BookWeave", id: "main") {
            VStack(alignment: .leading, spacing: 22) {
                HStack(spacing: 14) {
                    Image(nsImage: NSImage(named: NSImage.applicationIconName) ?? NSImage()).resizable().frame(width: 54, height: 54)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("BookWeave").font(.largeTitle.bold())
                        Text("在本机识别文字，生成可编辑的电子书。").foregroundStyle(.secondary)
                    }
                }
                GroupBox {
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(model.input?.lastPathComponent ?? "尚未选择 PDF").font(.headline).lineLimit(1)
                            Text(model.input?.deletingLastPathComponent().path ?? "支持文字版 PDF 和扫描版 PDF")
                                .font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                        }
                        Spacer()
                        Button("选择 PDF…") { model.choosePDF() }.disabled(model.running)
                    }.padding(10)
                }
                Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 12) {
                    GridRow { Text("书名"); TextField("默认使用文件名", text: $model.title).textFieldStyle(.roundedBorder) }
                    GridRow { Text("作者"); TextField("可选", text: $model.author).textFieldStyle(.roundedBorder) }
                    GridRow {
                        Text("电子书语言")
                        Picker("电子书语言", selection: $model.language) {
                            Text("中文").tag("zh")
                            Text("English").tag("en")
                        }.labelsHidden().frame(width: 150)
                    }
                    GridRow {
                        Text("保存到")
                        HStack {
                            Text(model.outputParent.path).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                            Spacer()
                            Button("更改…") { model.chooseOutput() }
                        }
                    }
                }.disabled(model.running)
                Divider()
                if let progress = model.progress, progress.totalPages > 0 {
                    GroupBox("自动分段处理") {
                        VStack(alignment: .leading, spacing: 10) {
                            HStack {
                                Text("已处理 \(progress.completed) / \(progress.totalPages) 页").font(.headline)
                                Spacer()
                                Text("共 \(progress.segmentProgress.count) 段").foregroundStyle(.secondary)
                            }
                            ProgressView(value: Double(progress.completed), total: Double(progress.totalPages))
                            if model.running, let page = progress.currentPage, let part = progress.currentPart,
                               progress.stage == "preparing" || progress.stage == "recognizing" {
                                Text("第 \(part) 段 · PDF 第 \(page) 页 · 本页 \(model.pageElapsed) 秒")
                                    .font(.caption).monospacedDigit()
                            }
                            ScrollView {
                                VStack(spacing: 8) {
                                    ForEach(progress.segmentProgress) { part in
                                        HStack {
                                            VStack(alignment: .leading, spacing: 3) {
                                                Text("第 \(part.number) 段 · PDF 第 \(part.first)–\(part.last) 页")
                                                ProgressView(value: Double(part.completed), total: Double(part.count))
                                            }
                                            Spacer(minLength: 12)
                                            Text(part.label).font(.caption)
                                                .foregroundStyle(part.reviewCount > 0 ? .orange : part.status == "complete" ? .green : .secondary)
                                                .frame(minWidth: 100, alignment: .trailing)
                                        }
                                    }
                                }
                            }.frame(height: min(CGFloat(progress.segmentProgress.count)*42, 168))
                            HStack {
                                Text("复用 \(progress.cacheHits) 页 · 需校对 \(progress.reviewPages.count) 页")
                                Spacer()
                                if progress.validation?.status == "passed" { Text("自动校验通过").foregroundStyle(.green) }
                            }.font(.caption).foregroundStyle(.secondary)
                            Text("异常页自动跳过并标记，全部分段完成后自动校验。页码从原 PDF 封面起计。")
                                .font(.caption).foregroundStyle(.secondary)
                        }.padding(8)
                    }
                }
                HStack(spacing: 12) {
                    if model.running { ProgressView().controlSize(.small) }
                    Text(model.status).font(.callout).textSelection(.enabled)
                    Spacer(minLength: 0)
                }
                HStack {
                    if model.running {
                        Button("停止转换", role: .destructive) { model.stop() }
                    } else {
                        Button("开始／继续转换") { model.start() }.buttonStyle(.borderedProminent)
                            .disabled(model.input == nil).keyboardShortcut(.defaultAction)
                    }
                    if let folder = model.folder {
                        Button("打开结果文件夹") { NSWorkspace.shared.open(folder) }
                        if let input = model.input {
                            let book = folder.appendingPathComponent(input.deletingPathExtension().lastPathComponent)
                            let live = book.appendingPathComponent(model.running ? "转换中.md" : "转换报告.md")
                            if FileManager.default.fileExists(atPath: live.path) {
                                Button(model.running ? "查看转换中文档" : "查看转换报告") { NSWorkspace.shared.open(live) }
                            }
                        }
                    }
                    Spacer()
                    if let result = model.result {
                        Button("打开 EPUB") { NSWorkspace.shared.open(result) }
                        Button("用 Sigil 编辑") { model.edit() }
                    }
                }
                if !model.log.isEmpty {
                    DisclosureGroup("运行详情", isExpanded: $model.showLog) {
                        ScrollView { Text(String(model.log.suffix(12000))).font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                            .frame(height: 125)
                    }.font(.caption).foregroundStyle(.secondary)
                }
                Text("每次转换单独保存，保留 EPUB、Markdown 和扫描页参考图。原始 PDF 不会被修改。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .padding(24).frame(width: 720)
            .onAppear { NSApp.windows.first?.delegate = delegate; NSApp.activate(ignoringOtherApps: true) }
        }
        .windowResizability(.contentSize)
    }
}
