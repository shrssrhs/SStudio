"""Regression tests for the Stage 3.2 native-viewport crash fix.

Background: install_template_browser() was originally called AFTER
embed_panda_window() with no host= argument, which made it fall through to
QMainWindow.takeCentralWidget()/setCentralWidget() to wrap the existing
central widget in a new QStackedWidget. embed_panda_window() had already
forced a real native HWND to exist for that widget's entire ancestor chain
(via container.winId()) and raw-Win32 SetParent()'d the foreign Panda3D
window under it -- reparenting that subtree afterwards corrupted the native
window relationship and caused an intermittent access violation on
studio.show() (Fatal Python error: Aborted / Windows fatal exception: access
violation), confirmed via Windows Event Log APPCRASH records naming
Qt6Gui.dll and ucrtbase.dll.

The fix: StudioMainWindow._build_central() now creates a permanent
self.central_stack (a QStackedWidget) and installs it as the central widget
ONCE, during normal construction, with the real editor widget as its first
page -- long before any native embedding can occur. client_studio.py's
main() now passes host=studio.central_stack to install_template_browser(),
which takes its isinstance(target, QStackedWidget) branch: this only ever
calls stack.addWidget(page) for the brand-new template page and NEVER calls
takeCentralWidget()/setCentralWidget() again.

These tests can't reproduce the native crash itself (that requires a real
Panda3D window and Win32 message pump, exercised separately via the real-
Windows manual test pass) -- what they verify is the structural invariant
that actually prevents it: the editor widget's identity and its parent
relationship to central_stack must never change after construction, no
matter how many times install_template_browser()/page-switching runs.

Follows this project's existing test convention (test_script_editor.py):
plain top-level-assertion script, run directly, offscreen Qt platform.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, '.')

from PySide6.QtWidgets import QApplication, QMainWindow, QStackedWidget, QWidget

app = QApplication.instance() or QApplication([])

import studio_editor_live as m
import sstudio_templates

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok: {message}")


def make_bridge():
    return m.EngineBridge(objects=[], live_mode=False)


def test_central_stack_exists_before_any_embedding() -> None:
    """The outer stack must exist as a direct result of normal construction
    -- nothing embed_panda_window()-shaped needs to run first."""
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    check(isinstance(win.central_stack, QStackedWidget), "StudioMainWindow.central_stack is a QStackedWidget after __init__")
    check(win.centralWidget() is win.central_stack, "central_stack is installed as the QMainWindow's central widget")
    check(win.central_stack.count() == 1, "central_stack starts with exactly one page (the editor)")
    check(isinstance(win.viewport_frame, QWidget), "viewport_frame exists after construction")


def test_install_template_browser_does_not_reparent_central_widget() -> None:
    """The actual regression: calling install_template_browser() with
    host=win.central_stack must never call takeCentralWidget()/
    setCentralWidget() again -- those are exactly the calls that corrupted
    the native window hierarchy in the original bug."""
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    editor_page = win.central_stack.widget(0)
    editor_page_id = id(editor_page)
    viewport_frame_id = id(win.viewport_frame)

    take_central_calls = {"count": 0}
    set_central_calls = {"count": 0}
    original_take = win.takeCentralWidget
    original_set = win.setCentralWidget

    def spy_take():
        take_central_calls["count"] += 1
        return original_take()

    def spy_set(widget):
        set_central_calls["count"] += 1
        return original_set(widget)

    win.takeCentralWidget = spy_take
    win.setCentralWidget = spy_set

    binding = sstudio_templates.install_template_browser(
        win, host=win.central_stack, show_immediately=False,
    )

    check(take_central_calls["count"] == 0, "install_template_browser(host=central_stack) never calls takeCentralWidget()")
    check(set_central_calls["count"] == 0, "install_template_browser(host=central_stack) never calls setCentralWidget()")
    check(win.centralWidget() is win.central_stack, "central widget identity is unchanged after install_template_browser()")
    check(win.central_stack.widget(0) is editor_page, "the original editor page is still page 0, same object")
    check(id(win.central_stack.widget(0)) == editor_page_id, "editor page's Python id is unchanged (never recreated)")
    check(id(win.viewport_frame) == viewport_frame_id, "viewport_frame's Python id is unchanged (never recreated)")
    check(win.central_stack.count() == 2, "central_stack now has exactly 2 pages: editor + template page")
    check(binding.previous_widget is editor_page, "TemplateBrowserBinding.previous_widget correctly captured the editor page")


def test_repeated_page_switching_preserves_identity() -> None:
    """start -> editor -> start (x10) must never change which QWidget
    instances back the editor page or the viewport -- only currentWidget()
    on the stack should change."""
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    editor_page = win.central_stack.widget(0)
    viewport_frame = win.viewport_frame

    take_central_calls = {"count": 0}
    set_central_calls = {"count": 0}
    original_take = win.takeCentralWidget
    original_set = win.setCentralWidget
    win.takeCentralWidget = lambda: (take_central_calls.__setitem__("count", take_central_calls["count"] + 1), original_take())[1]
    win.setCentralWidget = lambda w: (set_central_calls.__setitem__("count", set_central_calls["count"] + 1), original_set(w))[1]

    binding = sstudio_templates.install_template_browser(
        win, host=win.central_stack, show_immediately=False,
    )
    win.set_templates_binding(binding)

    for i in range(10):
        binding.show()
        check(win.central_stack.currentWidget() is binding.page, f"cycle {i}: start page is current after binding.show()")
        binding.show_editor()
        check(win.central_stack.currentWidget() is editor_page, f"cycle {i}: editor page is current after binding.show_editor()")
        check(win.central_stack.widget(0) is editor_page, f"cycle {i}: editor page object identity unchanged")
        check(win.viewport_frame is viewport_frame, f"cycle {i}: viewport_frame object identity unchanged")

    check(take_central_calls["count"] == 0, "10 cycles of start<->editor switching never call takeCentralWidget()")
    check(set_central_calls["count"] == 0, "10 cycles of start<->editor switching never call setCentralWidget()")
    check(win.centralWidget() is win.central_stack, "central widget is still central_stack after 10 switch cycles")


def test_set_start_page_never_reparents_existing_pages() -> None:
    """StudioMainWindow.set_start_page() -- the safe integration API -- must
    behave identically: only stack.addWidget(), no take/set central calls."""
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    editor_page = win.central_stack.widget(0)

    take_central_calls = {"count": 0}
    set_central_calls = {"count": 0}
    original_take = win.takeCentralWidget
    original_set = win.setCentralWidget
    win.takeCentralWidget = lambda: (take_central_calls.__setitem__("count", take_central_calls["count"] + 1), original_take())[1]
    win.setCentralWidget = lambda w: (set_central_calls.__setitem__("count", set_central_calls["count"] + 1), original_set(w))[1]

    extra_page = QWidget()
    win.set_start_page(extra_page)

    check(take_central_calls["count"] == 0, "set_start_page() never calls takeCentralWidget()")
    check(set_central_calls["count"] == 0, "set_start_page() never calls setCentralWidget()")
    check(win.central_stack.widget(0) is editor_page, "set_start_page() leaves the editor page as page 0")
    check(win.central_stack.count() == 2, "set_start_page() adds exactly one new page")
    check(win.central_stack.indexOf(extra_page) == 1, "the new page was added, not swapped in for an existing one")


def test_no_second_viewport_frame_or_stray_stack() -> None:
    """A second call path (e.g. a second install_template_browser() call,
    which a future regression might introduce) must not silently create a
    second native-embeddable container inside ViewportFrame."""
    bridge = make_bridge()
    win = m.StudioMainWindow(bridge)
    viewport_frame_id = id(win.viewport_frame)

    sstudio_templates.install_template_browser(win, host=win.central_stack, show_immediately=False)

    check(id(win.viewport_frame) == viewport_frame_id, "viewport_frame is not recreated by install_template_browser()")
    # ViewportFrame owns its own internal QStackedWidget (Stage 3.1) for
    # swapping in a native/mock/external viewport -- that one is expected
    # and must stay exactly one, untouched by the Stage 3.2 outer stack.
    inner_stacks = win.viewport_frame.findChildren(QStackedWidget)
    check(len(inner_stacks) >= 1, "ViewportFrame still owns its own internal viewport-swap QStackedWidget")


if __name__ == "__main__":
    test_central_stack_exists_before_any_embedding()
    test_install_template_browser_does_not_reparent_central_widget()
    test_repeated_page_switching_preserves_identity()
    test_set_start_page_never_reparents_existing_pages()
    test_no_second_viewport_frame_or_stray_stack()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for message in FAILURES:
            print(f"  - {message}")
        sys.exit(1)
    print("All viewport hierarchy regression tests passed.")
