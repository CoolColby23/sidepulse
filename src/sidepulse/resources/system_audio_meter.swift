import Foundation
import ScreenCaptureKit
import CoreMedia
import CoreAudio

private final class AudioOutput: NSObject, SCStreamOutput {
    private var lastEmission = ContinuousClock.now
    private let emissionInterval = Duration.milliseconds(80)

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of outputType: SCStreamOutputType
    ) {
        guard outputType == .audio, sampleBuffer.isValid else { return }
        let now = ContinuousClock.now
        guard now - lastEmission >= emissionInterval else { return }
        lastEmission = now

        var requiredSize = 0
        var retainedBlockBuffer: CMBlockBuffer?
        let sizingStatus = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: &requiredSize,
            bufferListOut: nil,
            bufferListSize: 0,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: UInt32(kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment),
            blockBufferOut: &retainedBlockBuffer
        )
        guard sizingStatus == noErr || requiredSize > 0 else { return }

        let storage = UnsafeMutableRawPointer.allocate(
            byteCount: requiredSize,
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        defer { storage.deallocate() }
        let audioBufferList = storage.bindMemory(to: AudioBufferList.self, capacity: 1)
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: audioBufferList,
            bufferListSize: requiredSize,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: UInt32(kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment),
            blockBufferOut: &retainedBlockBuffer
        )
        guard status == noErr else { return }

        var sumSquares = 0.0
        var sampleCount = 0
        for buffer in UnsafeMutableAudioBufferListPointer(audioBufferList) {
            guard let data = buffer.mData else { continue }
            let count = Int(buffer.mDataByteSize) / MemoryLayout<Float>.size
            let samples = data.assumingMemoryBound(to: Float.self)
            for index in 0..<count {
                let value = Double(samples[index])
                if value.isFinite {
                    sumSquares += value * value
                    sampleCount += 1
                }
            }
        }
        guard sampleCount > 0 else { return }
        let rms = sqrt(sumSquares / Double(sampleCount))
        print(String(format: "%.8f", rms))
        fflush(stdout)
    }
}

@main
private struct SidePulseSystemAudioMeter {
    static func main() async {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(
                false,
                onScreenWindowsOnly: false
            )
            guard let display = content.displays.first else {
                fputs("No display is available for system-audio capture.\n", stderr)
                exit(2)
            }

            let filter = SCContentFilter(display: display, excludingWindows: [])
            let configuration = SCStreamConfiguration()
            configuration.width = 2
            configuration.height = 2
            configuration.queueDepth = 1
            configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            configuration.capturesAudio = true
            configuration.excludesCurrentProcessAudio = true
            configuration.sampleRate = 48_000
            configuration.channelCount = 2

            let output = AudioOutput()
            let stream = SCStream(filter: filter, configuration: configuration, delegate: nil)
            let queue = DispatchQueue(label: "io.sidepulse.system-audio")
            try stream.addStreamOutput(output, type: .audio, sampleHandlerQueue: queue)
            try await stream.startCapture()

            withExtendedLifetime((stream, output)) {
                RunLoop.main.run()
            }
        } catch {
            fputs("System audio capture failed: \(error)\n", stderr)
            exit(1)
        }
    }
}
