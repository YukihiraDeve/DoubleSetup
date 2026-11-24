"""Fenêtre native macOS : barre de titre transparente + WKWebView (style CleanMyMac)."""

from __future__ import annotations

import AppKit
import objc
import WebKit
from Foundation import NSURL, NSMakeRect, NSObject, NSURLRequest

WIDTH, HEIGHT = 880, 700
TITLEBAR = 52
DRAG_WIDTH = 170  # le logo « SetupDouble » est là ; rien de cliquable dessous


class DragStrip(AppKit.NSView):
    """Bande invisible en haut : permet de déplacer la fenêtre malgré la page web."""

    def mouseDown_(self, event):
        if event.clickCount() == 2:
            self.window().performZoom_(None)
        else:
            self.window().performWindowDragWithEvent_(event)

    def acceptsFirstMouse_(self, event):
        return True


class AppDelegate(NSObject):
    def initWithURL_offline_(self, url, offline):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.url = url
        self.offline = offline
        return self

    def applicationDidFinishLaunching_(self, notification):
        self._build_menu()
        style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
                 | AppKit.NSWindowStyleMaskMiniaturizable | AppKit.NSWindowStyleMaskResizable
                 | AppKit.NSWindowStyleMaskFullSizeContentView)
        win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), style, AppKit.NSBackingStoreBuffered, False)
        win.setTitle_("SetupDouble")
        win.setTitlebarAppearsTransparent_(True)
        win.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        win.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        win.setBackgroundColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
            0.11, 0.08, 0.22, 1.0))
        win.setMinSize_((640, 560))
        win.setFrameAutosaveName_("SetupDoubleMain")
        win.setReleasedWhenClosed_(False)

        content = win.contentView()
        bounds = content.bounds()
        conf = WebKit.WKWebViewConfiguration.alloc().init()
        web = WebKit.WKWebView.alloc().initWithFrame_configuration_(bounds, conf)
        web.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        web.setCustomUserAgent_("Mozilla/5.0 (Macintosh) AppleWebKit SetupDouble")
        web.setValue_forKey_(False, "drawsBackground")  # pas de flash blanc au chargement
        content.addSubview_(web)

        # Zone de déplacement limitée au coin haut-gauche (à côté des boutons de fenêtre) :
        # elle ne doit jamais recouvrir un bouton de la page (roue dentée, pastille…).
        strip = DragStrip.alloc().initWithFrame_(
            NSMakeRect(78, bounds.size.height - TITLEBAR, DRAG_WIDTH, TITLEBAR))
        strip.setAutoresizingMask_(AppKit.NSViewMinYMargin | AppKit.NSViewMaxXMargin)
        content.addSubview_positioned_relativeTo_(strip, AppKit.NSWindowAbove, web)

        base = NSURL.URLWithString_(self.url)
        if self.offline:
            web.loadHTMLString_baseURL_(self.offline, base)
        else:
            web.loadRequest_(NSURLRequest.requestWithURL_(base))

        self.window, self.web = win, web
        if not win.setFrameUsingName_("SetupDoubleMain"):
            win.center()
        win.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)

    def _build_menu(self):
        bar = AppKit.NSMenu.alloc().init()
        app_item = AppKit.NSMenuItem.alloc().init()
        bar.addItem_(app_item)
        app_menu = AppKit.NSMenu.alloc().init()
        app_menu.addItemWithTitle_action_keyEquivalent_("Recharger", "reload:", "r")
        app_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        app_menu.addItemWithTitle_action_keyEquivalent_("Masquer SetupDouble", "hide:", "h")
        app_menu.addItemWithTitle_action_keyEquivalent_("Quitter SetupDouble", "terminate:", "q")
        app_item.setSubmenu_(app_menu)
        win_item = AppKit.NSMenuItem.alloc().init()
        bar.addItem_(win_item)
        win_menu = AppKit.NSMenu.alloc().initWithTitle_("Fenêtre")
        win_menu.addItemWithTitle_action_keyEquivalent_("Fermer", "performClose:", "w")
        win_menu.addItemWithTitle_action_keyEquivalent_("Réduire", "performMiniaturize:", "m")
        win_item.setSubmenu_(win_menu)
        AppKit.NSApp.setMainMenu_(bar)

    def reload_(self, sender):
        self.web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(self.url)))

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
        if not visible:
            self.window.makeKeyAndOrderFront_(None)
        return True


def run_window(url: str, offline_html: str | None = None) -> None:
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    delegate = AppDelegate.alloc().initWithURL_offline_(url, offline_html)
    app.setDelegate_(delegate)
    app.run()
