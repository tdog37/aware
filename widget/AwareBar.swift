// AwareBar — the Spinther orb: a menu-bar switch and status window for Aware.
// Bare-binary AppKit app, compiled locally by `aware widget` (no Xcode project).
// The ⚡ is bright while Aware is listening, dimmed when the mic is released.

import AppKit
import Darwin

// Find the Aware project root by walking UP from the executable until we see
// its fingerprint. Never count directory levels: the binary lives at
// widget/AwareBar when bare and widget/Aware.app/Contents/MacOS/AwareBar when
// bundled, and a wrong root silently points every file read at nothing.
let awareRoot: URL = {
    var dir = URL(fileURLWithPath: CommandLine.arguments[0])
        .resolvingSymlinksInPath()
        .deletingLastPathComponent()
    for _ in 0..<8 {
        let fm = FileManager.default
        if fm.fileExists(atPath: dir.appendingPathComponent("config.toml").path),
           fm.fileExists(atPath: dir.appendingPathComponent("bin/aware").path) {
            return dir
        }
        dir = dir.deletingLastPathComponent()
    }
    return URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("aware")
}()

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

    /// Audio is provably flowing when a chunk was written recently — or when
    /// words landed recently. The window is generous on purpose: chunk
    /// rollover, whisper runtime, and post-transcription cleanup leave normal
    /// gaps, and a warning that cries wolf is worse than no warning at all.
    private func captureAlive(window: TimeInterval = 150) -> Bool {
        if heardRecently(window: window) { return true }
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
            let title = (json["title"] as? String) ?? "Aware"
            // Our own card, unconditionally. macOS will not register
            // Notification Center for a locally-built app, and a message
            // Tim never sees is the same as no message at all.
            showCard(title: title, message: message)
        }
    }

    // MARK: - The card (permission-free notification)

    private var cards: [NSWindow] = []

    func showCard(title: String, message: String) {
        let width: CGFloat = 400
        let textWidth = width - 38
        let font = NSFont.systemFont(ofSize: 13.5)

        // Measure the WRAPPED height ourselves. sizeToFit() on a wrapping
        // label under-reports it, which silently clipped long messages.
        let para = NSMutableParagraphStyle()
        para.lineBreakMode = .byWordWrapping
        let measured = (message as NSString).boundingRect(
            with: NSSize(width: textWidth, height: .greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            attributes: [.font: font, .paragraphStyle: para]).height
        let textHeight = ceil(measured) + 4

        let text = NSTextField(wrappingLabelWithString: message)
        text.font = font
        // Explicit near-white; never a semantic color, which would flip to
        // black-on-black or white-on-white with the system theme.
        text.textColor = NSColor(calibratedRed: 0.96, green: 0.94, blue: 0.91, alpha: 1)
        text.drawsBackground = false
        text.preferredMaxLayoutWidth = textWidth
        text.frame = NSRect(x: 0, y: 0, width: textWidth, height: textHeight)

        let head = NSTextField(labelWithString: "⚡  \(title)")
        head.font = .systemFont(ofSize: 12.5, weight: .semibold)
        head.textColor = NSColor(calibratedRed: 1.0, green: 0.71, blue: 0.33, alpha: 1)
        head.drawsBackground = false
        head.sizeToFit()

        let hint = NSTextField(labelWithString: "click to dismiss")
        hint.font = .systemFont(ofSize: 10)
        hint.textColor = NSColor(calibratedRed: 0.55, green: 0.50, blue: 0.44, alpha: 1)
        hint.drawsBackground = false
        hint.sizeToFit()

        let height = max(78, textHeight + 50)
        let card = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: width, height: height),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered, defer: false)
        card.isFloatingPanel = true
        card.level = .statusBar
        card.backgroundColor = .clear
        card.isOpaque = false
        card.hasShadow = true
        card.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        // A plain layer-backed view, NOT NSVisualEffectView: its material
        // follows the system theme, which turned cream-on-charcoal into
        // white-on-white in light mode and made messages unreadable.
        let bg = NSView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        bg.wantsLayer = true
        bg.layer?.cornerRadius = 14
        bg.layer?.borderWidth = 1.5
        bg.layer?.borderColor = NSColor(calibratedRed: 1.0, green: 0.71, blue: 0.33, alpha: 0.8).cgColor
        bg.layer?.backgroundColor = NSColor(calibratedRed: 0.08, green: 0.065, blue: 0.055, alpha: 1.0).cgColor

        head.frame.origin = NSPoint(x: 18, y: height - 26)
        hint.frame.origin = NSPoint(x: width - hint.frame.width - 16, y: height - 24)
        text.frame.origin = NSPoint(x: 19, y: 14)
        bg.addSubview(head)
        bg.addSubview(hint)
        bg.addSubview(text)
        card.contentView = bg

        // Always the main screen, under the menu bar — predictable beats clever.
        if let f = NSScreen.main?.visibleFrame {
            let y = f.maxY - height - 12 - CGFloat(cards.count) * (height + 10)
            card.setFrameOrigin(NSPoint(x: f.maxX - width - 16, y: y))
        }
        // Visible immediately — no fade-in to fail silently.
        card.alphaValue = 1
        card.orderFrontRegardless()
        NSSound(named: "Glass")?.play()
        cards.append(card)
        unread += 1
        refreshIcon()

        // Click anywhere on the card to dismiss it.
        let click = NSClickGestureRecognizer(target: self, action: #selector(dismissCard(_:)))
        bg.addGestureRecognizer(click)

        // Stays put for two minutes — a message you never saw is a message
        // that never arrived. The menu-bar badge outlives even this.
        Timer.scheduledTimer(withTimeInterval: 120, repeats: false) { [weak self] _ in
            self?.close(card)
        }
    }

    private func close(_ card: NSWindow) {
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.3
            card.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            card.orderOut(nil)
            self?.cards.removeAll { $0 == card }
            self?.restack()
        })
    }

    @objc private func dismissCard(_ sender: NSGestureRecognizer) {
        if let card = sender.view?.window { close(card) }
    }

    private func restack() {
        guard let f = (NSScreen.main?.visibleFrame) else { return }
        for (i, card) in cards.enumerated() {
            let h = card.frame.height
            let y = f.maxY - h - 12 - CGFloat(i) * (h + 10)
            card.setFrameOrigin(NSPoint(x: f.maxX - card.frame.width - 16, y: y))
        }
    }

    // Messages shown but not yet acknowledged by opening the menu.
    private var unread = 0

    private enum OrbState { case off, blocked, hearing, quiet }

    // A transient gap must not flash red: the fault has to persist.
    private var unhealthySince: Date?

    private func orbState() -> OrbState {
        guard let pid = daemonPid else { unhealthySince = nil; return .off }
        let unhealthy = micError != nil
            || (daemonUptimeSeconds(pid) > 180 && !captureAlive())
        if unhealthy {
            let since = unhealthySince ?? Date()
            unhealthySince = since
            if Date().timeIntervalSince(since) > 90 { return .blocked }
        } else {
            unhealthySince = nil
        }
        return heardRecently() ? .hearing : .quiet
    }

    func refreshIcon() {
        guard let button = statusItem.button else { return }
        let state = orbState()
        // Distinct SYMBOL per state, not just a tint — a color shift on the
        // menu bar is too easy to miss.
        let symbol: String, color: NSColor, tip: String
        switch state {
        case .off:
            symbol = "bolt.slash"; color = .tertiaryLabelColor
            tip = "Aware is off — mic released"
        case .blocked:
            symbol = "exclamationmark.triangle.fill"; color = .systemRed
            tip = micError ?? "Listening but no audio flowing — check mic/permission"
        case .hearing:
            symbol = "waveform"; color = .systemOrange
            tip = "Aware hears you"
        case .quiet:
            symbol = "bolt"; color = .controlTextColor
            tip = "Aware is listening (quiet room)"
        }
        // An unread message outranks every other state: the icon must say
        // "I have something for you" until Tim actually looks.
        let sym = unread > 0 ? "bell.badge.fill" : symbol
        let tint = unread > 0 ? NSColor.systemOrange : color
        if let image = NSImage(systemSymbolName: sym, accessibilityDescription: tip) {
            let config = NSImage.SymbolConfiguration(pointSize: 15, weight: .medium)
                .applying(.init(paletteColors: [tint]))
            button.image = image.withSymbolConfiguration(config)
            button.attributedTitle = NSAttributedString(string: "")
        } else {  // symbol unavailable: fall back to a glyph
            button.image = nil
            button.attributedTitle = NSAttributedString(
                string: unread > 0 ? "●\u{FE0E}" : "⚡\u{FE0E}",
                attributes: [.foregroundColor: tint,
                             .font: NSFont.systemFont(ofSize: 15, weight: .medium)])
        }
        button.toolTip = unread > 0
            ? "\(unread) new message\(unread == 1 ? "" : "s") from \(persona)" : tip
    }

    /// Last few messages, newest first — so a missed card is never lost.
    private func recentMessages(_ limit: Int = 5) -> [(String, String)] {
        guard let text = try? String(contentsOf: notifyQueue, encoding: .utf8) else { return [] }
        var out: [(String, String)] = []
        for line in text.split(separator: "\n").suffix(limit).reversed() {
            guard let json = (try? JSONSerialization.jsonObject(with: Data(line.utf8)))
                    as? [String: Any],
                  let msg = json["message"] as? String else { continue }
            let ts = (json["ts"] as? String) ?? ""
            out.append((ts.count >= 16 ? String(ts.dropFirst(11).prefix(5)) : "", msg))
        }
        return out
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

        let recent = recentMessages()
        if !recent.isEmpty {
            menu.addItem(.separator())
            menu.addItem(info("From \(persona):"))
            for (time, msg) in recent {
                // Wrap long messages so nothing is cut off mid-thought.
                let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
                let text = NSMutableAttributedString(
                    string: "\(time)  ", attributes: [
                        .foregroundColor: NSColor.tertiaryLabelColor,
                        .font: NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .regular)])
                text.append(NSAttributedString(string: msg, attributes: [
                    .foregroundColor: NSColor.labelColor,
                    .font: NSFont.systemFont(ofSize: 12)]))
                let para = NSMutableParagraphStyle()
                para.lineBreakMode = .byWordWrapping
                para.maximumLineHeight = 15
                text.addAttribute(.paragraphStyle, value: para,
                                  range: NSRange(location: 0, length: text.length))
                item.attributedTitle = text
                item.isEnabled = false
                menu.addItem(item)
            }
        }

        // Opening the menu counts as reading them.
        unread = 0
        refreshIcon()

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
        var ago = ""
        if let end = (json["end"] ?? json["ts"]) as? String {
            let df = DateFormatter()
            df.locale = Locale(identifier: "en_US_POSIX")
            df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
            if let when = df.date(from: end) {
                let secs = Int(Date().timeIntervalSince(when))
                ago = secs < 90 ? " (\(secs)s ago)"
                    : (secs < 5400 ? " (\(secs / 60)m ago)" : "")
            }
        }
        return "\(time)\(ago): \(heard.prefix(44))\(heard.count > 44 ? "…" : "")"
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
