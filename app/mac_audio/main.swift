// sck_audio — захват системного звука macOS через ScreenCaptureKit (macOS 13+), без BlackHole.
// Пишет в stdout непрерывный поток: float32, mono, 16000 Гц (little-endian).
// Ошибки — в stderr одной строкой с префиксом "ERROR:" и выход с кодом 1 (нет разрешения) / 2 (прочее).
//
//   sck_audio                         — весь системный звук (кроме собственного звука приложения)
//   sck_audio --apps us.zoom.xos,...  — только звук указанных приложений (bundle id), если они запущены
//
// Сборка:  swiftc -O -target arm64-apple-macos13.0 main.swift -o sck_audio

import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

setvbuf(stdout, nil, _IONBF, 0)

func fail(_ msg: String, code: Int32) -> Never {
    FileHandle.standardError.write(("ERROR: " + msg + "\n").data(using: .utf8)!)
    exit(code)
}

@available(macOS 13.0, *)
final class Capturer: NSObject, SCStreamOutput, SCStreamDelegate {
    private var stream: SCStream?
    private let out = FileHandle.standardOutput
    private let queue = DispatchQueue(label: "sck.audio")

    func start(bundleIDs: [String]) async {
        let content: SCShareableContent
        do {
            content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        } catch {
            fail("нет разрешения на запись экрана и системного звука (\(error.localizedDescription))", code: 1)
        }
        guard let display = content.displays.first else { fail("не найден дисплей", code: 2) }

        let me = ProcessInfo.processInfo.processIdentifier
        let wanted = content.applications.filter { bundleIDs.contains($0.bundleIdentifier) }
        let filter: SCContentFilter
        if !bundleIDs.isEmpty && !wanted.isEmpty {
            filter = SCContentFilter(display: display, including: wanted, exceptingWindows: [])
        } else {
            let mine = content.applications.filter { $0.processID == me }
            filter = SCContentFilter(display: display, excludingApplications: mine, exceptingWindows: [])
        }

        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.excludesCurrentProcessAudio = true
        cfg.sampleRate = 16000
        cfg.channelCount = 1
        // видео нам не нужно: минимальный кадр раз в секунду
        cfg.width = 2
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        cfg.queueDepth = 3

        let s = SCStream(filter: filter, configuration: cfg, delegate: self)
        do {
            try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
            try await s.startCapture()
        } catch {
            fail("не удалось начать захват звука (\(error.localizedDescription))", code: 2)
        }
        stream = s
        FileHandle.standardError.write("READY\n".data(using: .utf8)!)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, CMSampleBufferIsValid(sb), CMSampleBufferGetNumSamples(sb) > 0 else { return }

        var size = 0
        CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sb, bufferListSizeNeededOut: &size, bufferListOut: nil, bufferListSize: 0,
            blockBufferAllocator: nil, blockBufferMemoryAllocator: nil, flags: 0, blockBufferOut: nil)
        guard size > 0 else { return }
        let raw = UnsafeMutableRawPointer.allocate(byteCount: size, alignment: 16)
        defer { raw.deallocate() }
        let ablPtr = raw.bindMemory(to: AudioBufferList.self, capacity: 1)
        var block: CMBlockBuffer?
        let st = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sb, bufferListSizeNeededOut: nil, bufferListOut: ablPtr, bufferListSize: size,
            blockBufferAllocator: nil, blockBufferMemoryAllocator: nil,
            flags: UInt32(kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment), blockBufferOut: &block)
        guard st == noErr else { return }

        let buffers = UnsafeMutableAudioBufferListPointer(ablPtr)
        if buffers.count == 1, let p = buffers[0].mData {
            out.write(Data(bytes: p, count: Int(buffers[0].mDataByteSize)))
            return
        }
        // несколько каналов раздельно — усредняем в mono
        let frames = Int(buffers[0].mDataByteSize) / MemoryLayout<Float>.size
        var mono = [Float](repeating: 0, count: frames)
        for b in buffers {
            guard let p = b.mData?.assumingMemoryBound(to: Float.self) else { continue }
            for i in 0..<min(frames, Int(b.mDataByteSize) / MemoryLayout<Float>.size) { mono[i] += p[i] }
        }
        let k = 1 / Float(buffers.count)
        for i in 0..<frames { mono[i] *= k }
        mono.withUnsafeBufferPointer { out.write(Data(buffer: $0)) }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fail("захват остановлен: \(error.localizedDescription)", code: 2)
    }
}

var apps: [String] = []
let args = CommandLine.arguments
if let i = args.firstIndex(of: "--apps"), i + 1 < args.count {
    apps = args[i + 1].split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) }
}

signal(SIGPIPE, SIG_IGN)
if #available(macOS 13.0, *) {
    let c = Capturer()
    Task { await c.start(bundleIDs: apps) }
    // родитель (Python) закрыл stdout или умер — выходим
    DispatchQueue.global().async {
        while true {
            sleep(2)
            if getppid() == 1 { exit(0) }
        }
    }
    withExtendedLifetime(c) { dispatchMain() }
} else {
    fail("нужна macOS 13 или новее", code: 2)
}
