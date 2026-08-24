// Menu bar front end: spawns the server and shows what it reports.
//
// LSUIElement is set, so there is no Dock icon.

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
    private var lastDetail = ""
    private var serveLAN = false          // is the frame endpoint reachable by the panel?
    private var lanAddress: String?       // this Mac's LAN IP, for the panel URL
    // Icon only by default - a long track name crowds the bar enough that
    // macOS starts hiding items.
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
            b.imagePosition = .imageLeading
            // With neither image nor title the button has zero width.
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

    /// Clear anything squatting on the port before binding it. An orphaned
    /// server keeps the socket while no longer answering, which otherwise
    /// leaves the menu stuck on "Starting…" with no explanation.
    private func clearPort() {
        let k = Process()
        k.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        k.arguments = ["-9", "-f", "tools/nowplaying.py"]
        k.standardOutput = FileHandle.nullDevice
        k.standardError = FileHandle.nullDevice
        try? k.run()
        k.waitUntilExit()
        usleep(400_000)          // let the socket close
    }

    private func startServer() {
        clearPort()
        let script = root + "/tools/nowplaying.py"
        guard FileManager.default.fileExists(atPath: script) else {
            lastState = "tools/nowplaying.py not found"
            rebuildMenu()
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = [script, "--port", String(PORT)]
        p.currentDirectoryURL = URL(fileURLWithPath: root)
        // Discard output: an unread pipe would block the child once full.
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run(); server = p } catch {
            lastState = "could not start server"
            rebuildMenu()
            return
        }
        // If it exits immediately, say so rather than polling a corpse.
        p.terminationHandler = { [weak self] proc in
            guard let self = self else { return }
            DispatchQueue.main.async {
                if self.server === proc {
                    self.lastTitle = ""; self.lastArtist = ""
                    self.lastState = "server stopped (exit \(proc.terminationStatus))"
                    self.updateButton(); self.rebuildMenu()
                }
            }
        }
    }

    @objc private func restartServer() {
        lastState = "restarting…"; rebuildMenu()
        stopServerProcess()
        startServer()
    }

    private func stopServerProcess() {
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
                self.updateButton(); self.rebuildMenu()
            }
        }.resume()
        readConfig()
    }

    private func updateButton() {
        guard let b = item.button else { return }
        // Menu bar space is shared and finite: title only, hard-capped, and
        // switchable off entirely. The full track is in the menu.
        if !showTitle || lastTitle.isEmpty {
            b.title = ""
            return
        }
        var s = lastTitle
        if s.count > 18 { s = String(s.prefix(17)).trimmingCharacters(in: .whitespaces) + "…" }
        b.title = " " + s
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
        // The panel follows playback by itself over WiFi; there is nothing
        // here to press.
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
        case "starting", "": return "Starting…"
        default:             return lastState
        }
    }

    private func menuItem(_ title: String, _ sel: Selector, key: String = "") -> NSMenuItem {
        let i = NSMenuItem(title: title, action: sel, keyEquivalent: key)
        i.target = self
        return i
    }

    /// A menu item that reads as a switch in both positions. macOS draws a
    /// checkmark for .on and nothing for .off, so an off toggle would otherwise
    /// look like an ordinary command.
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



    /// Let the panel reach this Mac over the network. Off by default: turning
    /// it on binds every interface, putting the current track and the rendered
    /// frame on the local network.
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

    /// Deezer-only, or whatever is playing. Deezer-only by default so a
    /// browser video does not take over the panel.
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
