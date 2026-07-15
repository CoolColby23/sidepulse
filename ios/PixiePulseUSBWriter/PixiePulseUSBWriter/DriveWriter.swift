import Foundation

enum DriveWriterError: LocalizedError {
    case noFolderSelected
    case bookmarkStale
    case accessDenied
    case textTooLarge(Int)
    case missingText

    var errorDescription: String? {
        switch self {
        case .noFolderSelected:
            return "Pick the USB drive folder first."
        case .bookmarkStale:
            return "The saved Files permission is stale. Pick the USB folder again."
        case .accessDenied:
            return "iOS did not grant access to the selected USB folder."
        case .textTooLarge(let byteCount):
            return "LEDS.TXT is \(byteCount) bytes. Keep it at or below 512 bytes."
        case .missingText:
            return "No LED text was provided."
        }
    }
}

final class DriveWriter {
    static let shared = DriveWriter()

    private let bookmarkKey = "usbFolderBookmark"
    private let fileNameKey = "ledFileName"
    private let defaultFileName = "LEDS.TXT"
    private let maxLEDBytes = 512

    private init() {}

    var fileName: String {
        get {
            let stored = UserDefaults.standard.string(forKey: fileNameKey) ?? defaultFileName
            let trimmed = stored.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? defaultFileName : trimmed
        }
        set {
            UserDefaults.standard.set(newValue, forKey: fileNameKey)
        }
    }

    var hasSavedFolder: Bool {
        UserDefaults.standard.data(forKey: bookmarkKey) != nil
    }

    var savedFolderDisplayName: String {
        guard let url = try? resolveFolderURL() else {
            return "No USB folder selected"
        }

        return url.path
    }

    func saveFolder(_ url: URL) throws {
        EventLog.append("Saving USB folder bookmark: \(url.lastPathComponent)")
        let startedAccess = url.startAccessingSecurityScopedResource()
        defer {
            if startedAccess {
                url.stopAccessingSecurityScopedResource()
            }
        }

        let bookmark = try url.bookmarkData(
            options: [],
            includingResourceValuesForKeys: nil,
            relativeTo: nil
        )
        UserDefaults.standard.set(bookmark, forKey: bookmarkKey)
        EventLog.append("Saved USB folder bookmark")
    }

    @discardableResult
    func write(_ text: String) throws -> URL {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            throw DriveWriterError.missingText
        }

        let byteCount = text.data(using: .utf8)?.count ?? 0
        guard byteCount <= maxLEDBytes else {
            throw DriveWriterError.textTooLarge(byteCount)
        }

        let folderURL = try resolveFolderURL()
        let startedAccess = folderURL.startAccessingSecurityScopedResource()
        guard startedAccess else {
            throw DriveWriterError.accessDenied
        }

        defer {
            folderURL.stopAccessingSecurityScopedResource()
        }

        let targetURL = folderURL.appendingPathComponent(fileName, isDirectory: false)
        let data = Data(text.utf8)
        try data.write(to: targetURL)
        EventLog.append("Wrote \(data.count) bytes to \(targetURL.lastPathComponent)")
        return targetURL
    }

    private func resolveFolderURL() throws -> URL {
        guard let bookmark = UserDefaults.standard.data(forKey: bookmarkKey) else {
            throw DriveWriterError.noFolderSelected
        }

        var isStale = false
        let url = try URL(
            resolvingBookmarkData: bookmark,
            options: [],
            relativeTo: nil,
            bookmarkDataIsStale: &isStale
        )

        if isStale {
            throw DriveWriterError.bookmarkStale
        }

        return url
    }
}
