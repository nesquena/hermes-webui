import Cocoa

class SplashWindowController: NSWindowController {

    init(title: String) {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 380, height: 200),
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        window.isOpaque = false
        window.backgroundColor = .clear
        window.level = .floating
        window.center()
        super.init(window: window)

        let container = NSView(frame: window.contentView!.bounds)
        container.wantsLayer = true
        container.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        container.layer?.cornerRadius = 16
        window.contentView?.addSubview(container)

        let label = NSTextField(labelWithString: title)
        label.font = NSFont.systemFont(ofSize: 22, weight: .semibold)
        label.textColor = .labelColor
        label.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(label)

        let subtitle = NSTextField(labelWithString: "Establishing SSH tunnel…")
        subtitle.font = NSFont.systemFont(ofSize: 13)
        subtitle.textColor = .secondaryLabelColor
        subtitle.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(subtitle)

        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .regular
        spinner.isIndeterminate = true
        spinner.translatesAutoresizingMaskIntoConstraints = false
        spinner.startAnimation(nil)
        container.addSubview(spinner)

        NSLayoutConstraint.activate([
            spinner.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            spinner.centerYAnchor.constraint(equalTo: container.centerYAnchor, constant: 10),

            label.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            label.bottomAnchor.constraint(equalTo: spinner.topAnchor, constant: -16),

            subtitle.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            subtitle.topAnchor.constraint(equalTo: spinner.bottomAnchor, constant: 12),
        ])
    }

    required init?(coder: NSCoder) { fatalError() }
}
