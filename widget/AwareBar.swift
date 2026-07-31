// AwareBar — the Spinther orb: a menu-bar switch and status window for Aware.
// Bare-binary AppKit app, compiled locally by `aware widget` (no Xcode project).
// The ⚡ is bright while Aware is listening, dimmed when the mic is released.

import AppKit
import Darwin
import UserNotifications

let exeURL = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
let awareRoot = exeURL.deletingLastPathComponent().deletingLastPathComponent()

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var statusItem: NSStatusItem!
    var timer: Timer?

    private var awareBin: String { awareRoot.appendingPathComponent("bin/aware").path }

    // The mind's name, read from config.toml so renames flow through.
    private lazy var persona: String = {
        let path = awareRoot.appendingPathComponent("config.toml")
        if let text = try? String(contentsOf: path, encoding: .utf8) {
            for raw in text.split(separator: "\n") {
                let line = raw.trimmingCharacters(in: .whitespaces)
                if line.hasPrefix("persona"), let q1 = line.firstIndex(of: "\"") {
                    let rest = line[line.index(after: q1)...]
                    if let q2 = rest.firstIndex(of: "\"") { return String(rest[..<q2]) }
                }
            }
        }
        return "Spinther"
    }()

    private func shellOut(_ path: String, _ args: [String]) -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return "" }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return String(data: data, encoding: .utf8) ?? ""
    }

    /// EXACTLY the CLI's algorithm (pidfile + identity check, pgrep fallback)
    /// so the orb and the terminal can never disagree about the mic.
    private var daemonPid: Int32? {
        let pidFile = awareRoot.appendingPathComponent("state/aware.pid")
        if let s = try? String(contentsOf: pidFile, encoding: .utf8),
           let pid = Int32(s.trimmingCharacters(in: .whitespacesAndNewlines)),
           kill(pid, 0) == 0,
           shellOut("/bin/ps", ["-o", "command=", "-p", String(pid)]).contains("-m aware start") {
            return pid
        }
        for token in shellOut("/usr/bin/pgrep", ["-f", "--", "-m aware start"])
            .split(whereSeparator: \.isNewline) {
            if let pid = Int32(token.trimmingCharacters(in: .whitespaces)) { return pid }
        }
        return nil
    }

    private func daemonUptimeSeconds(_ pid: Int32) -> Int {
        // ps etime formats: "mm:ss", "hh:mm:ss", "d-hh:mm:ss"
        var s = shellOut("/bin/ps", ["-o", "etime=", "-p", String(pid)])
            .trimmingCharacters(in: .whitespacesAndNewlines)
        var days = 0
        if let dash = s.firstIndex(of: "-") {
            days = Int(s[..<dash]) ?? 0
            s = String(s[s.index(after: dash)...])
        }
        var secs = 0
        for part in s.split(separator: ":") { secs = secs * 60 + (Int(part) ?? 0) }
        return days * 86400 + secs
    }

    /// Audio is provably flowing when the live chunk was written recently.
    private func captureAlive(window: TimeInterval = 45) -> Bool {
        let dir = awareRoot.appendingPathComponent("chunks")
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: dir, includingPropertiesForKeys: [.contentModificationDateKey])
        else { return false }
        for f in files where f.pathExtension == "wav" {
            if let d = try? f.resourceValues(forKeys: [.contentModificationDateKey])
                .contentModificationDate,
               Date().timeIntervalSince(d) < window { return true }
        }
        return false
    }

    private var micError: String? {
        let p = awareRoot.appendingPathComponent("state/capture_error")
        guard let text = try? String(contentsOf: p, encoding: .utf8) else { return nil }
        return text.split(separator: "\n").last.map(String.init)
    }

    private var todayTranscript: URL {
        let df = DateFormatter()
        df.locale = Locale(identifier: "en_US_POSIX")
        df.dateFormat = "yyyy-MM-dd"
        return awareRoot.appendingPathComponent("transcripts/\(df.string(from: Date())).jsonl")
    }

    /// Last non-empty line of today's transcript, read from the file tail
    /// so the 3-second poll stays cheap even late in a long day.
    private func lastTranscriptEntry() -> [String: Any]? {
        guard let fh = try? FileHandle(forReadingFrom: todayTranscript) else { return nil }
        defer { try? fh.close() }
        let size = (try? fh.seekToEnd()) ?? 0
        let back: UInt64 = 4096
        try? fh.seek(toOffset: size > back ? size - back : 0)
        guard let data = try? fh.readToEnd(),
              let text = String(data: data, encoding: .utf8),
              let line = text.split(separator: "\n").last(where: { !$0.isEmpty })
        else { return nil }
        return (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any]
    }

    /// True if words landed in the transcript within the last `window` seconds —
    /// Tim's "I see you, I hear you" glow.
    private func heardRecently(window: TimeInterval = 90) -> Bool {
        guard let entry = lastTranscriptEntry(),
              let ts = (entry["end"] ?? entry["ts"]) as? String else { return false }
        let df = DateFormatter()
        df.locale = Locale(identifier: "en_US_POSIX")
        df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        df.timeZone = TimeZone.current
        guard let when = df.date(from: ts) else { return false }
        return Date().timeIntervalSince(when) < window
    }

    // Byte offset into state/notify.jsonl — only entries appended after
    // launch are delivered, never replayed history.
    private var notifyOffset: UInt64 = 0
    private var notifyQueue: URL {
        awareRoot.appendingPathComponent("state/notify.jsonl")
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        UNUserNotificationCenter.current()
            .requestAuthorization(options: [.alert, .sound]) { _, _ in }
        if let size = try? FileManager.default
            .attributesOfItem(atPath: notifyQueue.path)[.size] as? UInt64 {
            notifyOffset = size
        }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.autoenablesItems = false
        menu.delegate = self
        statusItem.menu = menu
        refreshIcon()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            self?.refreshIcon()
            self?.deliverQueuedNotifications()
        }
    }

    private func deliverQueuedNotifications() {
        guard let fh = try? FileHandle(forReadingFrom: notifyQueue) else { return }
        defer { try? fh.close() }
        let size = (try? fh.seekToEnd()) ?? 0
        if size < notifyOffset { notifyOffset = 0 }  // file was rotated/reset
        guard size > notifyOffset else { return }
        try? fh.seek(toOffset: notifyOffset)
        guard let data = try? fh.readToEnd(),
              let text = String(data: data, encoding: .utf8) else { return }
        notifyOffset = size
        for line in text.split(separator: "\n") where !line.isEmpty {
            guard let json = (try? JSONSerialization.jsonObject(with: Data(line.utf8)))
                    as? [String: Any],
                  let message = json["message"] as? String else { continue }
            let content = UNMutableNotificationContent()
            content.title = (json["title"] as? String) ?? "Aware"
            content.body = message
            content.sound = .default
            UNUserNotificationCenter.current().add(UNNotificationRequest(
                identifier: UUID().uuidString, content: content, trigger: nil))
        }
    }

    private enum OrbState { case off, blocked, hearing, quiet }

    private func orbState() -> OrbState {
        guard let pid = daemonPid else { return .off }
        if micError != nil { return .blocked }
        if daemonUptimeSeconds(pid) > 60 && !captureAlive() { return .blocked }
        return heardRecently() ? .hearing : .quiet
    }

    func refreshIcon() {
        guard let button = statusItem.button else { return }
        let state = orbState()
        // dim = off · red = says ON but audio is NOT flowing · ember = "I hear
        // you" · normal = listening, quiet room.
        let color: NSColor
        let tip: String
        switch state {
        case .off:     color = .tertiaryLabelColor; tip = "Aware is off — mic released"
        case .blocked: color = .systemRed
                       tip = micError ?? "Listening but no audio flowing — check mic/permission"
        case .hearing: color = .systemOrange;       tip = "Aware hears you"
        case .quiet:   color = .controlTextColor;   tip = "Aware is listening (quiet)"
        }
        // U+FE0E forces the text glyph so the color actually applies.
        button.attributedTitle = NSAttributedString(
            string: "⚡\u{FE0E}",
            attributes: [
                .foregroundColor: color,
                .font: NSFont.systemFont(ofSize: 15,
                                         weight: state == .hearing ? .bold : .medium),
            ])
        button.toolTip = tip
    }

    // MARK: - Menu

    private func info(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    private func action(_ title: String, _ sel: Selector, _ key: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: sel, keyEquivalent: key)
        item.target = self
        item.isEnabled = true
        return item
    }

    func menuWillOpen(_ menu: NSMenu) {
        menu.removeAllItems()
        let state = orbState()
        let on = state != .off

        switch state {
        case .off:     menu.addItem(info("Off — microphone released"))
        case .blocked: menu.addItem(info("⚠ ON but audio is NOT flowing"))
                       menu.addItem(info(micError ?? "Check mic permission in System Settings"))
        case .hearing: menu.addItem(info("⚡ Listening — hears you"))
        case .quiet:   menu.addItem(info("⚡ Listening (quiet room)"))
        }
        if let heard = lastHeard() { menu.addItem(info("Heard \(heard)")) }
        if let note = lastNotification() { menu.addItem(info("\(persona): \(note)")) }

        menu.addItem(.separator())
        menu.addItem(action(on ? "Turn Off (release mic)" : "Turn On (start listening)",
                            #selector(toggleAware), "t"))
        let wake = action(isWaking ? "Waking…" : "Wake \(persona) now", #selector(wakeMind), "w")
        wake.isEnabled = !isWaking
        menu.addItem(wake)
        menu.addItem(action("Open today's transcript", #selector(openTranscript), "l"))
        menu.addItem(.separator())
        menu.addItem(action("Quit Aware Widget", #selector(quitWidget), "q"))
    }

    private func lastHeard() -> String? {
        guard let json = lastTranscriptEntry(),
              let heard = json["text"] as? String, let ts = json["ts"] as? String
        else { return nil }
        let time = ts.count >= 16 ? String(ts.dropFirst(11).prefix(5)) : ""
        return "\(time): \(heard.prefix(46))\(heard.count > 46 ? "…" : "")"
    }

    private func lastNotification() -> String? {
        let path = awareRoot.appendingPathComponent("state/brain.json")
        guard let data = try? Data(contentsOf: path),
              let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let notes = json["recent_notifications"] as? [String],
              let last = notes.last
        else { return nil }
        return "\(last.prefix(54))\(last.count > 54 ? "…" : "")"
    }

    // MARK: - Actions

    private func runAware(_ args: [String]) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: awareBin)
        p.arguments = args
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return }
        DispatchQueue.global().async { [weak self] in
            p.waitUntilExit()
            DispatchQueue.main.async { self?.refreshIcon() }
        }
    }

    @objc func toggleAware() { runAware(["toggle"]) }

    // One wake at a time — a double-click must not spawn two minds.
    private var isWaking = false

    @objc func wakeMind() {
        guard !isWaking else { return }
        isWaking = true
        let p = Process()
        p.executableURL = URL(fileURLWithPath: awareBin)
        p.arguments = ["brain"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { isWaking = false; return }
        DispatchQueue.global().async { [weak self] in
            p.waitUntilExit()
            DispatchQueue.main.async {
                self?.isWaking = false
                self?.refreshIcon()
            }
        }
    }

    @objc func openTranscript() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        p.arguments = ["-e", todayTranscript.path]  // TextEdit handles .jsonl fine
        try? p.run()
    }

    @objc func quitWidget() { NSApp.terminate(nil) }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
