import Foundation
import ScreenCaptureKit
import AVFoundation
import CoreMedia

// MyWhisper system-audio recorder.
// Captures all system audio via ScreenCaptureKit and writes it to a WAV file.
//   usage: mywhisper-sysaudio <output.wav>
// Prints "RECORDING" once capture is live; stops cleanly on SIGINT / SIGTERM.

@available(macOS 13.0, *)
final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private let outputURL: URL
    private var stream: SCStream?
    private var audioFile: AVAudioFile?
    private let writeQueue = DispatchQueue(label: "mywhisper.write")

    init(outputURL: URL) {
        self.outputURL = outputURL
        super.init()
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else {
            throw NSError(domain: "MyWhisper", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "No display available."])
        }
        let filter = SCContentFilter(display: display, excludingWindows: [])

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.sampleRate = 48000
        config.channelCount = 2
        // A tiny video stream is still required; its frames are ignored.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 6)
        config.queueDepth = 6

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .audio,
                                   sampleHandlerQueue: DispatchQueue(label: "mywhisper.audio"))
        try stream.addStreamOutput(self, type: .screen,
                                   sampleHandlerQueue: DispatchQueue(label: "mywhisper.screen"))
        try await stream.startCapture()
        self.stream = stream
        print("RECORDING")
        fflush(stdout)
    }

    func stop() async {
        if let stream = stream {
            try? await stream.stopCapture()
        }
        writeQueue.sync { self.audioFile = nil }
    }

    func stream(_ stream: SCStream,
                didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }
        guard let desc = sampleBuffer.formatDescription,
              var asbd = desc.audioStreamBasicDescription else { return }

        _ = try? sampleBuffer.withAudioBufferList { abl, _ in
            guard let format = AVAudioFormat(streamDescription: &asbd),
                  let pcm = AVAudioPCMBuffer(pcmFormat: format,
                                             bufferListNoCopy: abl.unsafePointer) else {
                return
            }
            writeQueue.sync {
                if self.audioFile == nil {
                    self.audioFile = try? AVAudioFile(
                        forWriting: self.outputURL,
                        settings: format.settings,
                        commonFormat: format.commonFormat,
                        interleaved: format.isInterleaved)
                }
                try? self.audioFile?.write(from: pcm)
            }
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write(
            ("stream stopped: \(error.localizedDescription)\n").data(using: .utf8)!)
    }
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

guard #available(macOS 13.0, *) else {
    fail("MyWhisper system-audio capture requires macOS 13 or later.")
}
guard CommandLine.arguments.count >= 2 else {
    fail("usage: mywhisper-sysaudio <output.wav>")
}

let outputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let recorder = Recorder(outputURL: outputURL)

signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
let sigint = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
let onStop: () -> Void = {
    Task {
        await recorder.stop()
        exit(0)
    }
}
sigint.setEventHandler(handler: onStop)
sigterm.setEventHandler(handler: onStop)
sigint.resume()
sigterm.resume()

Task {
    do {
        try await recorder.start()
    } catch {
        fail("capture failed: \(error.localizedDescription)")
    }
}

RunLoop.main.run()
