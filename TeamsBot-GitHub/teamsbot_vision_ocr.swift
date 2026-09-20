import AppKit
import Foundation
import Vision

// A deliberately small, separate-process OCR worker. Keeping Vision outside
// the Tk/Python process prevents a stalled framework request from holding up
// the app's diagnostics loop.
guard CommandLine.arguments.count >= 2 else {
    FileHandle.standardError.write(Data("Usage: teamsbot_vision_ocr <image-path> [fast]\n".utf8))
    exit(64)
}

let imagePath = CommandLine.arguments[1]
let useFastRecognition = CommandLine.arguments.dropFirst().contains("fast")
let includeBoxes = CommandLine.arguments.dropFirst().contains("boxes")

guard let image = NSImage(contentsOfFile: imagePath),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write(Data("Could not load OCR image.\n".utf8))
    exit(65)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = useFastRecognition ? .fast : .accurate
request.usesLanguageCorrection = false

do {
    let handler = VNImageRequestHandler(cgImage: cgImage, orientation: .up, options: [:])
    try handler.perform([request])
    var boxedResults: [[String: Any]] = []
    for observation in request.results ?? [] {
        if let candidate = observation.topCandidates(1).first {
            if includeBoxes {
                let box = observation.boundingBox
                boxedResults.append([
                    "text": candidate.string,
                    "x": box.origin.x,
                    "y": box.origin.y,
                    "width": box.size.width,
                    "height": box.size.height,
                ])
            } else {
                print(candidate.string)
            }
        }
    }
    if includeBoxes,
       let data = try? JSONSerialization.data(withJSONObject: boxedResults),
       let output = String(data: data, encoding: .utf8) {
        print(output)
    }
} catch {
    FileHandle.standardError.write(Data("Vision OCR failed: \(error.localizedDescription)\n".utf8))
    exit(1)
}
