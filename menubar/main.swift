// Menu bar front end for the Deezer now-playing watcher.
//
// This is the bundle's executable. It spawns server/server.py as a child and
// shows what that reports.
//
// The server runs as a child of this bundle, and that still matters. macOS
// exposes only ONE now-playing client at a time, so whenever a browser plays a
// video Deezer becomes invisible to MediaRemote. The fallback reads Deezer's
// own window title through AppleScript, which needs Accessibility - and macOS
// attributes that grant to the responsible process, which is this bundle. So
// this app asks for the permission, and the child inherits the ability to use
// it.
//
// The grant is keyed to this binary's code-signature hash. The app is ad-hoc
// signed, so REBUILDING IT REVOKES THE PERMISSION and it has to be granted
// again. That is why the prompt below exists rather than a line in a README.
//
// LSUIElement is set, so there is no Dock icon - it lives in the menu bar as a
// background app, which is what it is.

import ApplicationServices
import Cocoa
import Foundation

let PORT = 8766

final class Controller: NSObject, NSApplicationDelegate {
    private var item: NSStatusItem!
    private var server: Process?
    private var timer: Timer?
    private var lastTitle = ""
    private var lastArtist = ""
    private var lastState = ""
    /// Set while we are killing the server on purpose, so the termination
    /// handler can tell an intentional stop from a crash.
    private var stopping = false
    /// Launch times of recent unplanned restarts, for the crash-loop guard.
    private var restarts: [Date] = []
    /// Whether we have already asked for Accessibility this launch.
    private var askedForAccessibility = false
    private var lastDetail = ""
    private var serveLAN = false          // is the frame endpoint reachable by the panel?
    private var lanAddress: String?       // this Mac's LAN IP, for the panel URL
    // Icon only by default. A 40-character track name crowded the menu bar so
    // hard that macOS started hiding items outright - including this one.
    private var showTitle = UserDefaults.standard.object(forKey: "showTitle") as? Bool ?? false
    private var followAny = false

    private let root: String = {
        // .../Now Playing.app/Contents/MacOS/NowPlaying -> project root
        var u = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        for _ in 0..<4 { u.deleteLastPathComponent() }
        return u.path
    }()

    func applicationDidFinishLaunching(_ n: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = item.button {
            let img = NSImage(systemSymbolName: "music.note",
                              accessibilityDescription: "Now Playing")
            img?.isTemplate = true
            b.image = img
            // Fall back to a glyph if the symbol is unavailable, or the button
            // has neither image nor title and collapses to nothing.
            if img == nil { b.title = "♪" }
        }
        rebuildMenu()
        startServer()
        timer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            self?.poll()
        }
        poll()
    }

    // MARK: - server lifecycle

    /// Clear anything squatting on our port before binding it.
    ///
    /// Orphaned servers are a real failure mode, not a theoretical one: a
    /// previous instance, or a stray started by something else, keeps the socket
    /// but stops answering. Without this the app binds nothing, dies silently,
    /// and the menu sits on "Starting…" forever with no clue why.
    private func clearPort() {
        let k = Process()
        k.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        k.arguments = ["-9", "-f", "server/server.py"]
        k.standardOutput = FileHandle.nullDevice
        k.standardError = FileHandle.nullDevice
        try? k.run()
        k.waitUntilExit()
        usleep(400_000)          // let the socket close
    }

    private func startServer() {
        clearPort()
        let script = root + "/server/server.py"
        guard FileManager.default.fileExists(atPath: script) else {
            lastState = "server/server.py not found"
            rebuildMenu()
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = [script, "--port", String(PORT)]
        p.currentDirectoryURL = URL(fileURLWithPath: root)
        // Append to a log rather than discarding. The menu can only show the
        // last state; when the server dies of a traceback that state is the
        // one thing that does not explain why. A file cannot block the child
        // the way an unread pipe would.
        if let log = logHandle() {
            p.standardOutput = log
            p.standardError = log
        } else {
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
        }
        do { try p.run(); server = p } catch {
            lastState = "could not start server"
            rebuildMenu()
            return
        }
        // A dead server used to stay dead: the menu said so and nothing acted
        // on it, so the panel kept re-fetching a refused port and held its last
        // image until somebody noticed the glyph. Bring it back instead, and
        // stop only if it is failing so fast that restarting cannot help.
        p.terminationHandler = { [weak self] proc in
            guard let self = self else { return }
            DispatchQueue.main.async {
                guard self.server === proc, !self.stopping else { return }
                self.lastTitle = ""; self.lastArtist = ""
                let code = proc.terminationStatus
                self.note("server exited (\(code))")

                let now = Date()
                self.restarts = self.restarts.filter { now.timeIntervalSince($0) < 60 }
                self.restarts.append(now)
                if self.restarts.count > 5 {
                    // Five deaths in a minute is a fault a restart will not
                    // clear; looping on it would just bury the reason.
                    self.lastState = "server keeps exiting (\(code)) - see log"
                    self.note("giving up after 5 restarts in 60 s")
                    self.updateButton(); self.rebuildMenu()
                    return
                }
                let delay = min(8.0, pow(2.0, Double(self.restarts.count - 1)))
                self.lastState = "server stopped (\(code)), restarting…"
                self.updateButton(); self.rebuildMenu()
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                    guard self.server === proc, !self.stopping else { return }
                    self.startServer()
                }
            }
        }
    }

    /// Ask for Accessibility the way any other app does: the system prompt,
    /// which carries its own "Open System Settings" button.
    ///
    /// Nothing can grant this on the user's behalf - that is the point of the
    /// permission - but the asking is worth automating, because the symptom
    /// otherwise reads as "nothing is playing" rather than "a permission is
    /// missing". macOS shows the prompt only once per binary, so if it has
    /// already been dismissed we open the pane directly instead.
    private func requestAccessibility() {
        guard !askedForAccessibility else { return }
        askedForAccessibility = true
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        if AXIsProcessTrustedWithOptions([key: true] as CFDictionary) {
            return                                  // already granted
        }
        note("accessibility not granted; prompted")
        // The prompt is fire-and-forget and may not appear. Give it a moment,
        // then open the pane so there is always somewhere to go.
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            guard let self = self, !AXIsProcessTrusted() else { return }
            self.note("opening the Accessibility pane")
            if let u = URL(string: "x-apple.systempreferences:"
                           + "com.apple.preference.security?Privacy_Accessibility") {
                NSWorkspace.shared.open(u)
            }
        }
    }

    /// Append-only log next to every other app log on the machine.
    private func logHandle() -> FileHandle? {
        let dir = FileManager.default.urls(for: .libraryDirectory,
                                           in: .userDomainMask)[0]
            .appendingPathComponent("Logs")
        let url = dir.appendingPathComponent("NowPlaying.log")
        try? FileManager.default.createDirectory(at: dir,
                                                 withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        guard let h = try? FileHandle(forWritingTo: url) else { return nil }
        // Keep it from growing without bound across months of uptime.
        if (try? h.seekToEnd()).map({ $0 > 2_000_000 }) == true {
            try? h.truncate(atOffset: 0)
        }
        return h
    }

    /// One line in the log, so the supervisor's own decisions are visible too.
    private func note(_ msg: String) {
        guard let h = logHandle() else { return }
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        _ = try? h.seekToEnd()
        h.write("[\(f.string(from: Date()))] menubar: \(msg)\n".data(using: .utf8)!)
        try? h.close()
    }

    @objc private func restartServer() {
        lastState = "restarting…"; rebuildMenu()
        stopServerProcess()
        startServer()
    }

    private func stopServerProcess() {
        stopping = true
        defer { stopping = false }
        guard let p = server, p.isRunning else { return }
        p.terminationHandler = nil
        p.terminate()
        let deadline = Date().addingTimeInterval(1.5)
        while p.isRunning && Date() < deadline { usleep(50_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }

    private func stopServer() {
        timer?.invalidate()
        stopServerProcess()
    }

    func applicationWillTerminate(_ n: Notification) { stopServer() }

    // MARK: - polling

    private func poll() {
        guard let url = URL(string: "http://127.0.0.1:\(PORT)/now") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 8
        URLSession.shared.dataTask(with: req) { [weak self] data, _, err in
            guard let self = self else { return }
            var title = "", artist = "", state = "", detail = ""
            if let d = data,
               let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                state = (j["state"] as? String) ?? ""
                detail = (j["detail"] as? String) ?? ""
                // paused and held carry a real track too - only "other" and
                // "idle" genuinely have nothing to show.
                if ["playing", "paused", "held"].contains(state),
                   let t = j["track"] as? [String: Any] {
                    title = (t["title"] as? String) ?? ""
                    artist = (t["artist"] as? String) ?? ""
                }
            } else if err != nil {
                state = "starting…"
            }
            DispatchQueue.main.async {
                self.lastTitle = title; self.lastArtist = artist
                self.lastState = state; self.lastDetail = detail
                // The server only reports this once it has actually tried to
                // read Deezer's window and been refused, so it is the right
                // moment to ask - rather than prompting at launch for a
                // permission that may never be needed.
                if state == "needs_accessibility" { self.requestAccessibility() }
                self.updateButton(); self.rebuildMenu()
            }
        }.resume()
        readConfig()
    }

    private func updateButton() {
        guard let b = item.button else { return }
        // imagePosition must track whether there is a title. With .imageLeading
        // and an empty title AppKit lays the button out for text it does not
        // have and collapses it to zero width, so the item is invisible while
        // the app carries on working.
        if !showTitle || lastTitle.isEmpty {
            b.title = ""
            b.imagePosition = b.image == nil ? .noImage : .imageOnly
            return
        }
        var s = lastTitle
        if s.count > 18 { s = String(s.prefix(17)).trimmingCharacters(in: .whitespaces) + "…" }
        b.title = " " + s
        b.imagePosition = b.image == nil ? .noImage : .imageLeading
    }

    // MARK: - menu

    private func rebuildMenu() {
        let m = NSMenu()

        if !lastTitle.isEmpty {
            let t = NSMenuItem(title: lastTitle, action: nil, keyEquivalent: "")
            t.attributedTitle = NSAttributedString(
                string: lastTitle,
                attributes: [.font: NSFont.boldSystemFont(ofSize: 13)])
            m.addItem(t)
            if !lastArtist.isEmpty {
                m.addItem(NSMenuItem(title: lastArtist, action: nil, keyEquivalent: ""))
            }
        } else {
            m.addItem(NSMenuItem(title: humanState(), action: nil, keyEquivalent: ""))
        }

        if !lastTitle.isEmpty && lastState != "playing" {
            let s = NSMenuItem(title: humanState(), action: nil, keyEquivalent: "")
            s.attributedTitle = NSAttributedString(
                string: humanState(),
                attributes: [.font: NSFont.systemFont(ofSize: 11),
                             .foregroundColor: NSColor.secondaryLabelColor])
            m.addItem(s)
        }

        m.addItem(.separator())
        m.addItem(menuItem("Open Now Playing", #selector(openUI)))
        // "Send to Panel" and "Follow Playback on Panel" used to live here.
        // Both drove the legacy USB path, which compiles the image into the
        // firmware and re-flashes the board - which overwrites the WiFi
        // firmware the panel now runs on. With the two of them active at once
        // the panel flipped between the two designs and appeared broken: it
        // would draw once, then stop, and on battery never update at all
        // because the USB firmware has nothing to fetch from. The panel now
        // follows playback by itself, so there is nothing here to press.
        let lan = toggleItem("Serve Frames over Wi\u{2011}Fi", #selector(toggleLAN),
                             on: serveLAN)
        lan.toolTip = serveLAN
            ? "The panel can fetch frames from this Mac over your local network."
            : "Off: the server only listens to this Mac, so the panel cannot reach it."
        m.addItem(lan)
        if serveLAN, let ip = lanAddress {
            let s = NSMenuItem(title: "", action: nil, keyEquivalent: "")
            s.attributedTitle = NSAttributedString(
                string: "    panel fetches from \(ip):\(PORT)",
                attributes: [.font: NSFont.systemFont(ofSize: 11),
                             .foregroundColor: NSColor.secondaryLabelColor])
            m.addItem(s)
        }
        m.addItem(toggleItem("Show Title in Menu Bar", #selector(toggleTitle),
                             on: showTitle))
        let any = toggleItem("Follow Any Player", #selector(toggleAny), on: followAny)
        any.toolTip = followAny
            ? "Any app with the audio session drives the panel."
            : "Only Deezer drives the panel. Other apps are ignored."
        m.addItem(any)
        m.addItem(menuItem("Restart Server", #selector(restartServer)))
        m.addItem(.separator())
        m.addItem(menuItem("Quit", #selector(quit), key: "q"))
        item.menu = m
    }

    private func humanState() -> String {
        switch lastState {
        case "playing":      return "Playing"
        case "paused":       return "Paused"
        case "held":         return "Last played"
        case "idle":         return "Nothing playing"
        case "other":        return lastDetail.isEmpty ? "Another app has the audio" : lastDetail
        case "needs_accessibility":
                             return "Needs Accessibility permission"
        case "starting", "": return "Starting…"
        default:             return lastState
        }
    }

    private func menuItem(_ title: String, _ sel: Selector, key: String = "") -> NSMenuItem {
        let i = NSMenuItem(title: title, action: sel, keyEquivalent: key)
        i.target = self
        return i
    }

    /// A menu item that reads as a switch whether it is on or off.
    ///
    /// macOS draws a checkmark for .on and NOTHING for .off, so a pair of
    /// toggles that both happen to be off looks like two ordinary commands -
    /// which is exactly how it was reported. Giving the off state its own glyph
    /// makes the control legible in both positions.
    private func toggleItem(_ title: String, _ sel: Selector, on: Bool) -> NSMenuItem {
        let i = menuItem(title, sel)
        i.state = on ? .on : .off
        i.onStateImage = NSImage(systemSymbolName: "checkmark.circle.fill",
                                 accessibilityDescription: "on")
        i.offStateImage = NSImage(systemSymbolName: "circle",
                                  accessibilityDescription: "off")
        return i
    }

    @objc private func openUI() {
        NSWorkspace.shared.open(URL(string: "http://localhost:\(PORT)")!)
    }



    /// Let the panel reach this Mac over the network.
    ///
    /// Off by default and deliberately explicit: turning it on binds the server
    /// to every interface, which puts the current track and the rendered frame
    /// on the local network. That is the user's call to make, not a default.
    @objc private func toggleLAN() {
        let want = !serveLAN
        guard let url = URL(string: "http://127.0.0.1:\(PORT)/config") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("http://127.0.0.1:\(PORT)", forHTTPHeaderField: "Origin")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["serve_lan": want])
        URLSession.shared.dataTask(with: req) { [weak self] _, _, _ in
            DispatchQueue.main.async {
                self?.serveLAN = want
                // The socket is already bound, so this only takes effect on restart.
                self?.restartServer()
            }
        }.resume()
    }

    private func readConfig() {
        guard let url = URL(string: "http://127.0.0.1:\(PORT)/config") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] d, _, _ in
            guard let d = d,
                  let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                  let v = j["serve_lan"] as? Bool else { return }
            let ip = j["lan_ip"] as? String
            let fa = (j["follow_any"] as? Bool) ?? false
            DispatchQueue.main.async {
                if self?.serveLAN != v || self?.lanAddress != ip || self?.followAny != fa {
                    self?.serveLAN = v; self?.lanAddress = ip; self?.followAny = fa
                    self?.rebuildMenu()
                }
            }
        }.resume()
    }

    /// Deezer-only, or whatever is playing.
    ///
    /// Deezer-only is the default: this panel exists to show album art, and a
    /// browser video taking the audio session should not take the wall with it.
    @objc private func toggleAny() {
        let want = !followAny
        guard let url = URL(string: "http://127.0.0.1:\(PORT)/config") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("http://127.0.0.1:\(PORT)", forHTTPHeaderField: "Origin")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["follow_any": want])
        URLSession.shared.dataTask(with: req) { [weak self] _, _, _ in
            DispatchQueue.main.async { self?.followAny = want; self?.rebuildMenu() }
        }.resume()
    }

    @objc private func toggleTitle() {
        showTitle = !showTitle
        UserDefaults.standard.set(showTitle, forKey: "showTitle")
        updateButton()
        rebuildMenu()
    }

    @objc private func quit() {
        stopServer()
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
let controller = Controller()
app.delegate = controller
app.setActivationPolicy(.accessory)   // menu bar only, no Dock icon
app.run()
