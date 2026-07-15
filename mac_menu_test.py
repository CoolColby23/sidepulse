#!/usr/bin/env python3
"""Tiny native macOS status-bar UI smoke test.

If needed:
    python3 -m pip install pyobjc-framework-Cocoa
"""

try:
    import objc
    from AppKit import (
        NSApp,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSButton,
        NSFont,
        NSMakeRect,
        NSMinYEdge,
        NSPopover,
        NSPopoverBehaviorTransient,
        NSPopUpButton,
        NSStatusBar,
        NSTextField,
        NSVariableStatusItemLength,
        NSView,
        NSViewController,
    )
    from Foundation import NSObject
except ImportError as exc:
    raise SystemExit(
        "This status-bar test requires PyObjC:\n"
        "  python3 -m pip install pyobjc-framework-Cocoa"
    ) from exc


class PopoverController(NSViewController):
    def init(self):
        self = objc.super(PopoverController, self).init()
        if self is None:
            return None

        self.status_label = None
        return self

    def loadView(self):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 200, 200))

        title = NSTextField.labelWithString_("Status Bar UI")
        title.setFrame_(NSMakeRect(16, 162, 168, 24))
        title.setFont_(NSFont.boldSystemFontOfSize_(14))
        view.addSubview_(title)

        dropdown = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(16, 120, 168, 28), False
        )
        dropdown.addItemsWithTitles_(["One", "Two", "Three"])
        dropdown.setTarget_(self)
        dropdown.setAction_("dropdownChanged:")
        view.addSubview_(dropdown)

        button_one = NSButton.alloc().initWithFrame_(NSMakeRect(16, 82, 168, 30))
        button_one.setTitle_("Button 1")
        button_one.setTarget_(self)
        button_one.setAction_("buttonOneClicked:")
        view.addSubview_(button_one)

        button_two = NSButton.alloc().initWithFrame_(NSMakeRect(16, 46, 168, 30))
        button_two.setTitle_("Button 2")
        button_two.setTarget_(self)
        button_two.setAction_("buttonTwoClicked:")
        view.addSubview_(button_two)

        self.status_label = NSTextField.labelWithString_("Pick an action")
        self.status_label.setFrame_(NSMakeRect(16, 14, 168, 20))
        view.addSubview_(self.status_label)

        self.setView_(view)

    @objc.IBAction
    def dropdownChanged_(self, sender):
        self.status_label.setStringValue_(f"Selected {sender.titleOfSelectedItem()}")

    @objc.IBAction
    def buttonOneClicked_(self, _sender):
        self.status_label.setStringValue_("Button 1 clicked")

    @objc.IBAction
    def buttonTwoClicked_(self, _sender):
        self.status_label.setStringValue_("Button 2 clicked")


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, _notification):
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        self.popover_controller = PopoverController.alloc().init()
        self.popover = NSPopover.alloc().init()
        self.popover.setBehavior_(NSPopoverBehaviorTransient)
        self.popover.setContentViewController_(self.popover_controller)

        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        status_button = self.status_item.button()
        status_button.setTitle_("UI")
        status_button.setTarget_(self)
        status_button.setAction_("togglePopover:")

    @objc.IBAction
    def togglePopover_(self, sender):
        if self.popover.isShown():
            self.popover.performClose_(sender)
            return

        self.popover.showRelativeToRect_ofView_preferredEdge_(
            sender.bounds(), sender, NSMinYEdge
        )
        NSApp.activateIgnoringOtherApps_(True)


def main() -> None:
    app = NSApplication.sharedApplication()
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
