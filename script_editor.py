"""
Stage 3.1: integrated Lua code editor.

Architecture summary (see the Stage 3.1 final report for full rationale):

- ScriptEditorWorkspace is a QTabWidget that becomes the new central widget.
  Tab 0 is the EXISTING ViewportFrame instance (never recreated, never
  reparented away and back -- just added as a tab page like any other
  QWidget) so the accepted native Panda3D window embedding is completely
  untouched; every other tab is a ScriptEditorDocument for one open
  Script/LocalScript/ModuleScript, keyed by stable instance ID.
- LuaCodeEditor is a QPlainTextEdit subclass with a line-number gutter,
  current-line highlight, soft-tab indentation, bracket matching, and a
  diagnostics gutter fed by lua_runtime.py's ScriptDiagnostic objects
  (via LuaRuntimeManager.add_diagnostic_listener -- see Stage 3.0/3.1
  final reports). Ctrl+Z/Ctrl+Y inside it use QPlainTextEdit's own text
  undo stack; the scene CommandManager is never involved for keystrokes.
- LuaSyntaxHighlighter uses QSyntaxHighlighter block state (0 = normal,
  1 = inside a [[ ]] long string, 2 = inside a --[[ ]] long comment) so
  multiline constructs are tracked correctly across lines rather than
  re-derived per line from regex alone. Nested/leveled long brackets
  ([=[ ]=], [==[ ]==], ...) are NOT distinguished -- documented
  simplification, see the highlighter's docstring.
- Dirty state is tracked via QPlainTextEdit's own `document().isModified()`
  flag: every programmatic text-setting call (initial load, a clean
  remote refresh, a scene-Undo-triggered refresh) is paired with an
  explicit `setModified(False)` right after, so the flag only ever
  reflects real user edits since the last authoritative save/refresh --
  matching the spec's "current text != last saved Source" definition of
  dirty without a full-text diff on every keystroke.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from PySide6.QtCore import QRect, QRegularExpression, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QShortcut,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

# ============================================================
# THEME -- matches DARK_STYLE in studio_editor_live.py. Centralized here
# so a later branding pass only has to edit this one dict.
# ============================================================

EDITOR_PALETTE = {
    "background": "#1c1d1f",
    "gutter_background": "#202122",
    "gutter_text": "#5c6066",
    "current_line": "#26282b",
    "text": "#d0d3d6",
    "selection": "#0877b9",
    "keyword": "#c586c0",
    "boolean_nil": "#2daee9",
    "number": "#d19a66",
    "string": "#98c379",
    "comment": "#6a737d",
    "builtin": "#4fc1e9",
    "function_call": "#e5c07b",
    "error": "#e06c75",
    "warning": "#d19a66",
    "bracket_match": "#3a3d41",
    "border": "#343639",
}

MONOSPACE_FONT_FAMILY = "Consolas"
DEFAULT_TAB_WIDTH_SPACES = 4

MAX_SOURCE_SIZE = 20000  # kept in sync with shared/object_registry.py's Script.Source PropertySpec("string", 20000) -- see Stage 3.1 report on why this isn't imported directly (no import cycle back into shared from this Qt-side module).

_LUA_KEYWORDS = (
    "and", "break", "do", "else", "elseif", "end", "for", "function", "goto",
    "if", "in", "local", "not", "or", "repeat", "return", "then", "until", "while",
)
_LUA_BOOLEAN_NIL = ("true", "false", "nil")
_LUA_BUILTINS = (
    "game", "workspace", "script", "Vector3", "Color3", "Instance", "task",
    "require", "print", "warn", "typeof",
)


def _format(color: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(color))
    if bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    if italic:
        fmt.setFontItalic(True)
    return fmt


# ============================================================
# SYNTAX HIGHLIGHTER
# ============================================================

class LuaSyntaxHighlighter:
    """Not a QSyntaxHighlighter subclass directly -- see LuaCodeEditor,
    which owns an internal _Highlighter(QSyntaxHighlighter) instance so
    this module's public surface stays plain Python. block state: 0 =
    normal, 1 = inside a [[ ]] long string, 2 = inside a --[[ ]] long
    comment. Leveled long brackets ([=[ ]=]) are treated the same as
    plain [[ ]] -- a documented simplification; scripts using leveled
    long brackets will see the highlighter close at the first ]] rather
    than the matching ]=]="""

    STATE_NORMAL = -1  # QSyntaxHighlighter's default "no previous state"
    STATE_STRING = 1
    STATE_COMMENT = 2

    def __init__(self) -> None:
        self.keyword_re = re.compile(r"\b(" + "|".join(_LUA_KEYWORDS) + r")\b")
        self.boolean_re = re.compile(r"\b(" + "|".join(_LUA_BOOLEAN_NIL) + r")\b")
        self.builtin_re = re.compile(r"\b(" + "|".join(_LUA_BUILTINS) + r")\b")
        self.number_re = re.compile(r"\b0x[0-9a-fA-F]+\b|\b\d+\.?\d*([eE][+-]?\d+)?\b")
        self.function_call_re = re.compile(r"\b([A-Za-z_]\w*)(?=\s*\()")
        self.short_string_re = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
        self.line_comment_re = re.compile(r"--(?!\[\[).*$")
        self.long_comment_open_re = re.compile(r"--\[\[")
        self.long_string_open_re = re.compile(r"\[\[")


class _LuaQSyntaxHighlighter(QSyntaxHighlighter):
    """The real QSyntaxHighlighter. LuaSyntaxHighlighter above just holds
    the compiled regexes/state constants so they exist independent of any
    particular QTextDocument; this class does the actual per-block work
    and is what LuaCodeEditor instantiates."""

    def __init__(self, document: Any) -> None:
        super().__init__(document)
        self._rules = LuaSyntaxHighlighter()

    def highlightBlock(self, text: str) -> None:
        rules = self._rules
        prev_state = self.previousBlockState()
        pos = 0
        length = len(text)

        if prev_state in (LuaSyntaxHighlighter.STATE_STRING, LuaSyntaxHighlighter.STATE_COMMENT):
            close_idx = text.find("]]")
            color = EDITOR_PALETTE["string"] if prev_state == LuaSyntaxHighlighter.STATE_STRING else EDITOR_PALETTE["comment"]
            italic = prev_state == LuaSyntaxHighlighter.STATE_COMMENT
            if close_idx == -1:
                self.setFormat(0, length, _format(color, italic=italic))
                self.setCurrentBlockState(prev_state)
                return
            self.setFormat(0, close_idx + 2, _format(color, italic=italic))
            pos = close_idx + 2

        self.setCurrentBlockState(LuaSyntaxHighlighter.STATE_NORMAL)
        remaining = text[pos:]

        # Does a long comment/string open somewhere in the rest of this
        # line (and never close before end of line)? If so, everything
        # from the opener to end-of-line is that construct, and state
        # carries into the next block.
        comment_open = rules.long_comment_open_re.search(remaining)
        string_open = None if comment_open else rules.long_string_open_re.search(remaining)
        tail_start = None
        tail_state = LuaSyntaxHighlighter.STATE_NORMAL
        tail_is_comment = False
        if comment_open is not None:
            after = remaining[comment_open.end():]
            if "]]" not in after:
                tail_start = pos + comment_open.start()
                tail_state = LuaSyntaxHighlighter.STATE_COMMENT
                tail_is_comment = True
        elif string_open is not None:
            after = remaining[string_open.end():]
            if "]]" not in after:
                tail_start = pos + string_open.start()
                tail_state = LuaSyntaxHighlighter.STATE_STRING

        head_end = tail_start if tail_start is not None else length
        self._highlight_single_line(rules, text[pos:head_end], pos)

        if tail_start is not None:
            color = EDITOR_PALETTE["comment"] if tail_is_comment else EDITOR_PALETTE["string"]
            self.setFormat(tail_start, length - tail_start, _format(color, italic=tail_is_comment))
            self.setCurrentBlockState(tail_state)

    def _highlight_single_line(self, rules: "LuaSyntaxHighlighter", text: str, base_offset: int) -> None:
        # Short strings first (so keywords inside strings don't get
        # relit), then a line comment ends the line -- nothing after `--`
        # (not followed by `[[`) is Lua code.
        covered: list[tuple[int, int]] = []

        for match in rules.short_string_re.finditer(text):
            self.setFormat(base_offset + match.start(), match.end() - match.start(), _format(EDITOR_PALETTE["string"]))
            covered.append((match.start(), match.end()))

        comment_match = rules.line_comment_re.search(text)
        comment_start = comment_match.start() if comment_match else None
        if comment_start is not None and not any(start <= comment_start < end for start, end in covered):
            self.setFormat(base_offset + comment_start, len(text) - comment_start, _format(EDITOR_PALETTE["comment"], italic=True))
            text = text[:comment_start]  # don't apply further rules past the comment

        def _in_covered(idx: int) -> bool:
            return any(start <= idx < end for start, end in covered)

        for match in rules.number_re.finditer(text):
            if _in_covered(match.start()):
                continue
            self.setFormat(base_offset + match.start(), match.end() - match.start(), _format(EDITOR_PALETTE["number"]))

        for match in rules.function_call_re.finditer(text):
            if _in_covered(match.start()):
                continue
            word = match.group(1)
            if word in _LUA_KEYWORDS or word in _LUA_BOOLEAN_NIL:
                continue
            self.setFormat(base_offset + match.start(1), len(word), _format(EDITOR_PALETTE["function_call"]))

        for match in rules.builtin_re.finditer(text):
            if _in_covered(match.start()):
                continue
            self.setFormat(base_offset + match.start(), match.end() - match.start(), _format(EDITOR_PALETTE["builtin"], bold=True))

        for match in rules.boolean_re.finditer(text):
            if _in_covered(match.start()):
                continue
            self.setFormat(base_offset + match.start(), match.end() - match.start(), _format(EDITOR_PALETTE["boolean_nil"]))

        for match in rules.keyword_re.finditer(text):
            if _in_covered(match.start()):
                continue
            self.setFormat(base_offset + match.start(), match.end() - match.start(), _format(EDITOR_PALETTE["keyword"], bold=True))


# ============================================================
# LINE NUMBER / DIAGNOSTIC GUTTER
# ============================================================

class LineNumberArea(QWidget):
    def __init__(self, editor: "LuaCodeEditor") -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802 -- Qt override
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event: Any) -> None:  # noqa: N802 -- Qt override
        self.editor.line_number_area_paint_event(event)


_BRACKET_PAIRS = {"(": ")", "[": "]", "{": "}"}
_BRACKET_CLOSERS = {v: k for k, v in _BRACKET_PAIRS.items()}

_BLOCK_OPENERS_RE = re.compile(r"\b(then|do|repeat)\s*$|\bfunction\s*[\w.:]*\s*\([^)]*\)\s*$")
_BLOCK_CLOSERS_RE = re.compile(r"^\s*(end|else|elseif|until)\b")


class LuaCodeEditor(QPlainTextEdit):
    """One Lua source buffer. Text Undo/Redo is QPlainTextEdit's own
    built-in QTextDocument stack -- never routed through the scene
    CommandManager (see ScriptEditorDocument/ScriptEditorWorkspace for
    where the two are kept separate at the Edit-menu level)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        font = QFont(MONOSPACE_FONT_FAMILY)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(10)
        self.setFont(font)
        self.setTabChangesFocus(False)
        metrics = self.fontMetrics()
        self.setTabStopDistance(metrics.horizontalAdvance(" ") * DEFAULT_TAB_WIDTH_SPACES)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setStyleSheet(
            f"QPlainTextEdit {{ background: {EDITOR_PALETTE['background']}; "
            f"color: {EDITOR_PALETTE['text']}; border: none; "
            f"selection-background-color: {EDITOR_PALETTE['selection']}; }}"
        )

        self.highlighter = _LuaQSyntaxHighlighter(self.document())
        self.line_number_area = LineNumberArea(self)
        self._diagnostics_by_line: dict[int, list[Any]] = {}

        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self.cursorPositionChanged.connect(self._on_cursor_moved)
        self._update_line_number_area_width(0)
        self._on_cursor_moved()

    # ---------------- line number gutter ----------------

    def line_number_area_width(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        return 10 + self.fontMetrics().horizontalAdvance("9") * digits + 12

    def _update_line_number_area_width(self, _new_block_count: int) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_line_number_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_line_number_area_width(0)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 -- Qt override
        super().resizeEvent(event)
        rect = self.contentsRect()
        self.line_number_area.setGeometry(QRect(rect.left(), rect.top(), self.line_number_area_width(), rect.height()))

    def line_number_area_paint_event(self, event: Any) -> None:
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor(EDITOR_PALETTE["gutter_background"]))

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                line_no = block_number + 1
                painter.setPen(QColor(EDITOR_PALETTE["gutter_text"]))
                painter.drawText(
                    0, top, self.line_number_area.width() - 8, self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight, str(line_no),
                )
                diags = self._diagnostics_by_line.get(line_no)
                if diags:
                    worst = "error" if any(d.severity == "error" for d in diags) else "warning"
                    color = QColor(EDITOR_PALETTE[worst])
                    marker_size = 6
                    marker_y = top + (self.fontMetrics().height() - marker_size) // 2
                    painter.setBrush(color)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawEllipse(2, marker_y, marker_size, marker_size)
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1

    def event(self, event: Any) -> bool:  # noqa: N802 -- Qt override
        if event.type() == event.Type.ToolTip:
            cursor = self.cursorForPosition(event.pos())
            line_no = cursor.blockNumber() + 1
            diags = self._diagnostics_by_line.get(line_no)
            if diags:
                QToolTip.showText(event.globalPos(), "\n".join(d.message for d in diags), self)
                return True
        return super().event(event)

    # ---------------- current line + bracket matching ----------------

    def _on_cursor_moved(self) -> None:
        selections = []
        current_line = self._current_line_selection()
        if current_line is not None:
            selections.append(current_line)
        selections.extend(self._bracket_match_selections())
        self.setExtraSelections(selections)

    def _current_line_selection(self):
        if self.isReadOnly():
            return None
        # QPlainTextEdit has no ExtraSelection of its own in this PySide6
        # build -- setExtraSelections() accepts QTextEdit.ExtraSelection
        # instances regardless of which of the two text-widget classes it's
        # called on.
        selection = QTextEdit.ExtraSelection()
        selection.format.setBackground(QColor(EDITOR_PALETTE["current_line"]))
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.cursor = self.textCursor()
        selection.cursor.clearSelection()
        return selection

    def _bracket_match_selections(self) -> list:
        cursor = self.textCursor()
        doc = self.document()
        pos = cursor.position()
        text = doc.toPlainText()

        candidates = []
        if pos < len(text) and text[pos] in _BRACKET_PAIRS:
            candidates.append((pos, text[pos], True))
        if pos > 0 and text[pos - 1] in _BRACKET_CLOSERS:
            candidates.append((pos - 1, text[pos - 1], False))
        if pos > 0 and text[pos - 1] in _BRACKET_PAIRS:
            candidates.append((pos - 1, text[pos - 1], True))
        if pos < len(text) and text[pos] in _BRACKET_CLOSERS:
            candidates.append((pos, text[pos], False))

        for start, char, is_opener in candidates:
            match_pos = self._find_matching_bracket(text, start, char, is_opener)
            if match_pos is None:
                continue
            result = []
            for p in (start, match_pos):
                sel = QTextEdit.ExtraSelection()
                sel.format.setBackground(QColor(EDITOR_PALETTE["bracket_match"]))
                sel.cursor = QTextCursor(doc)
                sel.cursor.setPosition(p)
                sel.cursor.setPosition(p + 1, QTextCursor.MoveMode.KeepAnchor)
                result.append(sel)
            return result
        return []

    @staticmethod
    def _find_matching_bracket(text: str, start: int, char: str, is_opener: bool) -> Optional[int]:
        if is_opener:
            close = _BRACKET_PAIRS[char]
            depth = 1
            for i in range(start + 1, len(text)):
                if text[i] == char:
                    depth += 1
                elif text[i] == close:
                    depth -= 1
                    if depth == 0:
                        return i
            return None
        opener = _BRACKET_CLOSERS[char]
        depth = 1
        for i in range(start - 1, -1, -1):
            if text[i] == char:
                depth += 1
            elif text[i] == opener:
                depth -= 1
                if depth == 0:
                    return i
        return None

    # ---------------- indentation ----------------

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 -- Qt override
        key = event.key()
        cursor = self.textCursor()

        if key == Qt.Key.Key_Tab and cursor.hasSelection():
            self._indent_selection(dedent=False)
            return
        if key == Qt.Key.Key_Backtab or (key == Qt.Key.Key_Tab and event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._indent_selection(dedent=True)
            return
        if key == Qt.Key.Key_Tab:
            cursor.insertText(" " * DEFAULT_TAB_WIDTH_SPACES)
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._insert_auto_indented_newline()
            return
        super().keyPressEvent(event)

    def _indent_selection(self, *, dedent: bool) -> None:
        cursor = self.textCursor()
        start, end = sorted((cursor.selectionStart(), cursor.selectionEnd()))
        cursor.beginEditBlock()
        cursor.setPosition(start)
        start_block = cursor.blockNumber()
        cursor.setPosition(end)
        end_block = cursor.blockNumber()

        doc = self.document()
        for block_no in range(start_block, end_block + 1):
            block = doc.findBlockByNumber(block_no)
            block_cursor = QTextCursor(block)
            block_cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            if dedent:
                line = block.text()
                strip_count = 0
                while strip_count < DEFAULT_TAB_WIDTH_SPACES and strip_count < len(line) and line[strip_count] == " ":
                    strip_count += 1
                if strip_count:
                    block_cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, strip_count)
                    block_cursor.removeSelectedText()
            else:
                block_cursor.insertText(" " * DEFAULT_TAB_WIDTH_SPACES)
        cursor.endEditBlock()

    def _insert_auto_indented_newline(self) -> None:
        cursor = self.textCursor()
        current_line = cursor.block().text()
        leading = re.match(r"[ \t]*", current_line).group(0)
        extra = ""
        before_cursor = current_line[: cursor.positionInBlock()]
        if _BLOCK_OPENERS_RE.search(before_cursor.rstrip()):
            extra = " " * DEFAULT_TAB_WIDTH_SPACES
        cursor.insertText("\n" + leading + extra)

    # ---------------- diagnostics ----------------

    def set_diagnostics(self, diagnostics: list) -> None:
        self._diagnostics_by_line = {}
        for diag in diagnostics:
            if diag.line is None:
                continue
            self._diagnostics_by_line.setdefault(diag.line, []).append(diag)
        self.line_number_area.update()
        self._on_cursor_moved()

    def clear_diagnostics(self) -> None:
        self.set_diagnostics([])

    def goto_line(self, line: int, column: int = 1) -> None:
        block = self.document().findBlockByNumber(max(0, line - 1))
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.MoveAnchor, max(0, column - 1))
        self.setTextCursor(cursor)
        self.centerCursor()
        self.setFocus()


# ============================================================
# FIND / REPLACE BAR
# ============================================================

class FindReplaceBar(QWidget):
    """One per ScriptEditorDocument. Ctrl+F shows it in find-only mode;
    Ctrl+H additionally reveals the replace row. Stage-3.1-scoped:
    per-document only, no project-wide search (see spec section 16)."""

    closed = Signal()

    def __init__(self, editor: LuaCodeEditor, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.editor = editor
        self.setStyleSheet(
            f"background: {EDITOR_PALETTE['gutter_background']}; border-top: 1px solid {EDITOR_PALETTE['border']};"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(3)

        find_row = QHBoxLayout()
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Find")
        self.find_edit.textChanged.connect(self._on_find_text_changed)
        self.find_edit.returnPressed.connect(self.find_next)
        self.match_label = QLabel("")
        self.match_label.setMinimumWidth(70)
        self.case_check = QCheckBox("Aa")
        self.case_check.setToolTip("Case-sensitive")
        self.case_check.toggled.connect(lambda _v: self._on_find_text_changed(self.find_edit.text()))
        self.word_check = QCheckBox("Word")
        self.word_check.setToolTip("Whole word")
        self.word_check.toggled.connect(lambda _v: self._on_find_text_changed(self.find_edit.text()))
        prev_button = QToolButton()
        prev_button.setText("˄")
        prev_button.setToolTip("Previous (Shift+F3)")
        prev_button.clicked.connect(self.find_previous)
        next_button = QToolButton()
        next_button.setText("˅")
        next_button.setToolTip("Next (F3)")
        next_button.clicked.connect(self.find_next)
        close_button = QToolButton()
        close_button.setText("×")
        close_button.clicked.connect(self.close_bar)

        find_row.addWidget(self.find_edit, 1)
        find_row.addWidget(self.match_label)
        find_row.addWidget(self.case_check)
        find_row.addWidget(self.word_check)
        find_row.addWidget(prev_button)
        find_row.addWidget(next_button)
        find_row.addWidget(close_button)
        outer.addLayout(find_row)

        self.replace_row_widget = QWidget()
        replace_row = QHBoxLayout(self.replace_row_widget)
        replace_row.setContentsMargins(0, 0, 0, 0)
        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText("Replace")
        replace_button = QPushButton("Replace")
        replace_button.clicked.connect(self.replace_current)
        replace_all_button = QPushButton("Replace All")
        replace_all_button.clicked.connect(self.replace_all)
        replace_row.addWidget(self.replace_edit, 1)
        replace_row.addWidget(replace_button)
        replace_row.addWidget(replace_all_button)
        outer.addWidget(self.replace_row_widget)
        self.replace_row_widget.setVisible(False)

    def show_for_find(self) -> None:
        self.replace_row_widget.setVisible(False)
        self.setVisible(True)
        self.find_edit.setFocus()
        self.find_edit.selectAll()

    def show_for_replace(self) -> None:
        self.replace_row_widget.setVisible(True)
        self.setVisible(True)
        self.find_edit.setFocus()
        self.find_edit.selectAll()

    def close_bar(self) -> None:
        self.setVisible(False)
        self.closed.emit()
        self.editor.setFocus()

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 -- Qt override
        if event.key() == Qt.Key.Key_Escape:
            self.close_bar()
            return
        super().keyPressEvent(event)

    def _find_flags(self, *, backward: bool = False):
        from PySide6.QtGui import QTextDocument
        flags = QTextDocument.FindFlag(0)
        if self.case_check.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if self.word_check.isChecked():
            flags |= QTextDocument.FindFlag.FindWholeWords
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        return flags

    def _on_find_text_changed(self, text: str) -> None:
        if not text:
            self.match_label.setText("")
            return
        count = len(re.findall(re.escape(text), self.editor.toPlainText(), 0 if self.case_check.isChecked() else re.IGNORECASE))
        self.match_label.setText(f"{count} match{'es' if count != 1 else ''}")

    def find_next(self) -> None:
        self._find(backward=False)

    def find_previous(self) -> None:
        self._find(backward=True)

    def _find(self, *, backward: bool) -> bool:
        text = self.find_edit.text()
        if not text:
            return False
        found = self.editor.find(text, self._find_flags(backward=backward))
        if not found:
            # Wrap around.
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start)
            self.editor.setTextCursor(cursor)
            found = self.editor.find(text, self._find_flags(backward=backward))
        return found

    def replace_current(self) -> None:
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            cursor.insertText(self.replace_edit.text())
            self.editor.setTextCursor(cursor)
        self.find_next()

    def replace_all(self) -> None:
        find_text = self.find_edit.text()
        if not find_text:
            return
        edit_cursor = self.editor.textCursor()
        edit_cursor.beginEditBlock()
        move = QTextCursor(self.editor.document())
        move.movePosition(QTextCursor.MoveOperation.Start)
        self.editor.setTextCursor(move)
        count = 0
        while self.editor.find(find_text, self._find_flags(backward=False)):
            found_cursor = self.editor.textCursor()
            found_cursor.insertText(self.replace_edit.text())
            self.editor.setTextCursor(found_cursor)
            count += 1
        edit_cursor.endEditBlock()
        self.match_label.setText(f"replaced {count}")


# ============================================================
# SCRIPT EDITOR DOCUMENT -- one open Script/LocalScript/ModuleScript tab.
# ============================================================

class ScriptEditorDocument(QWidget):
    """Owns one LuaCodeEditor + its Find/Replace bar + status line +
    conflict banner. Identity is `instance_id`, never `display_name` --
    see ScriptEditorWorkspace, which is the only thing that indexes
    documents, always by instance_id."""

    dirty_changed = Signal(str, bool)  # instance_id, is_dirty
    saved = Signal(str)  # instance_id

    def __init__(
        self,
        instance_id: str,
        class_name: str,
        display_name: str,
        source: str,
        bridge: Any,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.instance_id = instance_id
        self.class_name = class_name
        self.display_name = display_name
        self.bridge = bridge
        self.last_saved_source = source
        self.conflicted = False
        self.deleted = False
        self._pending_remote_source: Optional[str] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.conflict_banner = self._build_conflict_banner()
        self.conflict_banner.setVisible(False)
        layout.addWidget(self.conflict_banner)

        self.editor = LuaCodeEditor()
        self.editor.setPlainText(source)
        self.editor.document().setModified(False)
        self.editor.document().modificationChanged.connect(self._on_modification_changed)
        self.editor.cursorPositionChanged.connect(self._update_status)
        self.editor.textChanged.connect(self._update_status)
        layout.addWidget(self.editor, 1)

        self.find_bar = FindReplaceBar(self.editor)
        self.find_bar.setVisible(False)
        layout.addWidget(self.find_bar)

        self.status_label = QLabel()
        self.status_label.setContentsMargins(8, 2, 8, 2)
        layout.addWidget(self.status_label)
        self._update_status()

        # WidgetWithChildrenShortcut, not the QShortcut default of
        # WindowShortcut -- with the default context every open tab's
        # identical Ctrl+F shortcut would all be simultaneously "active"
        # for the whole StudioMainWindow at once (they don't stop existing
        # just because their tab isn't current), and Qt reports that as an
        # ambiguous shortcut and fires NEITHER. Scoping each one to this
        # document's own editor (and its children, e.g. the find bar)
        # means only the actually-focused document's shortcuts are live.
        # Ctrl+S is deliberately NOT registered here: StudioMainWindow's
        # File>Save QAction (Ctrl+S, WindowShortcut context) already
        # checks ScriptEditorWorkspace.focused_document() and calls
        # .save() itself (see _on_save_triggered) -- adding a second,
        # per-document Ctrl+S QShortcut here would make Ctrl+S ambiguous
        # (WindowShortcut + WidgetWithChildrenShortcut both "active" at
        # once) the instant any tab has focus, and Qt would fire NEITHER,
        # exactly the failure mode this comment describes for Ctrl+F.
        for sequence, slot in (
            ("Ctrl+F", self.find_bar.show_for_find),
            ("Ctrl+H", self.find_bar.show_for_replace),
            ("F3", self.find_bar.find_next),
            ("Shift+F3", self.find_bar.find_previous),
            ("Ctrl+G", self._prompt_goto_line),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self.editor, slot)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)

    # ---------------- conflict banner ----------------

    def _build_conflict_banner(self) -> QWidget:
        banner = QWidget()
        banner.setStyleSheet(f"background: {EDITOR_PALETTE['warning']}; color: #1a1a1a;")
        row = QHBoxLayout(banner)
        row.setContentsMargins(8, 4, 8, 4)
        label = QLabel("Another client changed this Script's Source while you had unsaved edits.")
        label.setWordWrap(True)
        reload_button = QPushButton("Reload Remote")
        reload_button.clicked.connect(self._resolve_conflict_reload_remote)
        keep_button = QPushButton("Keep Local")
        keep_button.clicked.connect(self._resolve_conflict_keep_local)
        row.addWidget(label, 1)
        row.addWidget(reload_button)
        row.addWidget(keep_button)
        return banner

    def _resolve_conflict_reload_remote(self) -> None:
        remote_source = self._pending_remote_source
        self._pending_remote_source = None
        self.conflicted = False
        self.conflict_banner.setVisible(False)
        if remote_source is not None:
            self._set_text_clean(remote_source)
            self.last_saved_source = remote_source

    def _resolve_conflict_keep_local(self) -> None:
        # The user must save intentionally afterward to actually overwrite
        # the remote version through the normal authoritative path -- this
        # only clears the banner and lets editing continue.
        self._pending_remote_source = None
        self.conflicted = False
        self.conflict_banner.setVisible(False)

    # ---------------- dirty tracking ----------------

    def _on_modification_changed(self, modified: bool) -> None:
        self.dirty_changed.emit(self.instance_id, modified)

    def is_dirty(self) -> bool:
        return self.editor.document().isModified()

    def _set_text_clean(self, text: str) -> None:
        """Every programmatic text-setting call goes through here so the
        modified flag never spuriously flips to dirty (see module
        docstring)."""
        cursor = self.editor.textCursor()
        scroll = self.editor.verticalScrollBar().value() if self.editor.verticalScrollBar() else 0
        self.editor.setPlainText(text)
        self.editor.document().setModified(False)
        try:
            self.editor.setTextCursor(cursor)
            if self.editor.verticalScrollBar():
                self.editor.verticalScrollBar().setValue(scroll)
        except Exception:
            pass

    # ---------------- status line ----------------

    def _update_status(self) -> None:
        cursor = self.editor.textCursor()
        line = cursor.blockNumber() + 1
        col = cursor.columnNumber() + 1
        length = len(self.editor.toPlainText())
        over_limit = length > MAX_SOURCE_SIZE
        near_limit = length > int(MAX_SOURCE_SIZE * 0.9)
        text = f"Line {line}, Col {col}     {length} / {MAX_SOURCE_SIZE} characters"
        if over_limit:
            text += "  (over limit -- cannot save)"
        self.status_label.setText(text)
        color = EDITOR_PALETTE["error"] if over_limit else (EDITOR_PALETTE["warning"] if near_limit else EDITOR_PALETTE["gutter_text"])
        self.status_label.setStyleSheet(f"color: {color};")

    # ---------------- goto line ----------------

    def _prompt_goto_line(self) -> None:
        max_line = self.editor.document().blockCount()
        line, ok = QInputDialog.getInt(self, "Go to Line", "Line number:", 1, 1, max_line, 1)
        if ok:
            self.editor.goto_line(line)

    # ---------------- save ----------------

    def save(self) -> bool:
        text = self.editor.toPlainText()
        if len(text) > MAX_SOURCE_SIZE:
            self.bridge.log(
                "error",
                f"Cannot save {self.display_name}: Source is {len(text)} characters, over the {MAX_SOURCE_SIZE}-character limit.",
            )
            return False
        # Fire-and-forget through the SAME authoritative path every other
        # Inspector field already uses (bridge.set_property ->
        # MultiplayerStudioAdapter.set_property -> exactly one
        # PropertyEditCommand, see Stage 2.5/3.0) -- marked clean
        # optimistically per the project's existing optimistic
        # property-update policy (UPDATE_PROPERTY has no reject path).
        self.bridge.set_property(self.instance_id, "properties.Source", text)
        self.last_saved_source = text
        self.editor.document().setModified(False)
        self.saved.emit(self.instance_id)
        self.bridge.log("info", f"Saved Source for {self.display_name} ({len(text)} characters).")
        return True

    # ---------------- diagnostics ----------------

    def apply_diagnostics(self, diagnostics: list) -> None:
        self.editor.set_diagnostics(diagnostics)

    def goto_line(self, line: int) -> None:
        self.editor.goto_line(line)
        self.editor.setFocus()

    # ---------------- remote refresh ----------------

    def refresh_source(self, new_source: str) -> None:
        """Called when scene_changed reveals the authoritative Source no
        longer matches what this document last saved/loaded."""
        if new_source == self.last_saved_source:
            return
        if not self.is_dirty():
            self._set_text_clean(new_source)
            self.last_saved_source = new_source
            return
        # Dirty: never silently overwrite unsaved edits.
        self._pending_remote_source = new_source
        self.conflicted = True
        self.conflict_banner.setVisible(True)

    def mark_deleted(self) -> None:
        self.deleted = True
        self.display_name = f"{self.display_name} (Deleted)"
        self.editor.setReadOnly(False)  # keep editable so the user can still copy/save-as-new later


# ============================================================
# DIAGNOSTIC ENTRY -- Qt-signal-crossing shape for one ScriptDiagnostic.
# ============================================================

@dataclass
class DiagnosticEntry:
    """lua_runtime.py's ScriptDiagnostic can't cross the Qt signal
    boundary as-is (EngineBridge.lua_diagnostic carries its fields as
    plain signal arguments, not the dataclass instance) -- this is the
    shape ScriptEditorWorkspace reassembles them into, matching exactly
    what LuaCodeEditor.set_diagnostics()/_diagnostics_by_line already
    expect (`.severity`, `.message`, `.line`)."""

    severity: str
    message: str
    line: Optional[int]


# ============================================================
# SCRIPT EDITOR WORKSPACE -- the central document tab container.
# ============================================================

class ScriptEditorWorkspace(QTabWidget):
    """Tab 0 is the permanent Scene document (the pre-existing
    ViewportFrame instance, added as a tab page and never recreated --
    see module docstring on why this leaves native viewport embedding
    completely untouched). Every other tab is a ScriptEditorDocument
    keyed by stable instance ID, never by name/path."""

    def __init__(self, bridge: Any, scene_widget: QWidget, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.scene_widget = scene_widget
        self.setTabsClosable(True)
        self.setMovable(False)
        self.setDocumentMode(True)
        self.tabCloseRequested.connect(self._on_tab_close_requested)

        self._documents: dict[str, ScriptEditorDocument] = {}
        self._icon_provider: Optional[Callable[[str], Any]] = None
        self._document_opened_callback: Optional[Callable[[ScriptEditorDocument], None]] = None

        # Stage 3.1 diagnostics: keyed by instance_id (script_id), never by
        # name -- two Scripts sharing a display name get two independent
        # entries here since their instance_id differs. _current_lua_session
        # is set only by _on_lua_session_started (never by an incoming
        # diagnostic itself), so a diagnostic tagged with any other
        # session_id is a straggler from an already-superseded Play session
        # and is dropped rather than misattributed to the current one.
        self._pending_diagnostics: dict[str, list[DiagnosticEntry]] = {}
        self._current_lua_session: int = 0

        scene_index = self.addTab(scene_widget, "Scene")
        # The Scene tab can never be closed -- remove its close button
        # specifically (setTabsClosable(True) puts one on every tab by
        # default).
        self.tabBar().setTabButton(scene_index, QTabBar.ButtonPosition.RightSide, None)

        bridge.object_deleted.connect(self._on_instance_deleted)
        bridge.scene_changed.connect(self._on_scene_changed)
        bridge.lua_diagnostic.connect(self._on_lua_diagnostic)
        bridge.lua_session_started.connect(self._on_lua_session_started)

    def set_icon_provider(self, provider: Callable[[str], Any]) -> None:
        """Optional hook so studio_editor_live.py (which owns IconFactory)
        can supply per-class-name tab icons without this module importing
        anything from there and creating a circular import."""
        self._icon_provider = provider

    def set_on_document_opened(self, callback: Callable[[ScriptEditorDocument], None]) -> None:
        """Optional hook run once per newly-opened ScriptEditorDocument
        (not on re-focusing an already-open one) -- lets
        StudioMainWindow keep its Undo/Redo action labels live as THIS
        SPECIFIC editor's text-undo-stack availability changes, without
        this module reaching back into studio_editor_live.py."""
        self._document_opened_callback = callback

    # ---------------- open / close ----------------

    def open_script(self, instance_id: str) -> bool:
        existing = self._documents.get(instance_id)
        if existing is not None:
            self.setCurrentWidget(existing)
            existing.editor.setFocus()
            return True
        obj = self.bridge.get_object(instance_id)
        if obj is None:
            return False
        source = str(obj.properties.get("Source", ""))
        doc = ScriptEditorDocument(instance_id, obj.object_type, obj.name, source, self.bridge)
        doc.dirty_changed.connect(lambda _id, _dirty: self._update_tab_title(instance_id))
        doc.saved.connect(lambda _id: self._update_tab_title(instance_id))
        self._documents[instance_id] = doc
        icon = self._icon_provider(obj.object_type) if self._icon_provider is not None else None
        index = self.addTab(doc, icon, obj.name) if icon is not None else self.addTab(doc, obj.name)
        self._update_tab_title(instance_id)
        self.setCurrentIndex(index)
        doc.editor.setFocus()
        pending = self._pending_diagnostics.get(instance_id)
        if pending:
            doc.apply_diagnostics(pending)
        if self._document_opened_callback is not None:
            self._document_opened_callback(doc)
        return True

    def is_scene_tab(self, index: int) -> bool:
        return self.widget(index) is self.scene_widget

    def _update_tab_title(self, instance_id: str) -> None:
        doc = self._documents.get(instance_id)
        if doc is None:
            return
        index = self.indexOf(doc)
        if index == -1:
            return
        title = doc.display_name
        if doc.is_dirty():
            title += "*"
        self.setTabText(index, title)

    def _on_tab_close_requested(self, index: int) -> None:
        widget = self.widget(index)
        if widget is self.scene_widget:
            return  # Scene can never be permanently closed
        instance_id = self._instance_id_for_widget(widget)
        if instance_id is None:
            return
        doc = self._documents[instance_id]
        if doc.is_dirty():
            decision = self._prompt_save_discard_cancel(doc.display_name)
            if decision == "cancel":
                return
            if decision == "save":
                if not doc.save():
                    return  # validation failed (e.g. over size limit) -- keep tab open
        self._close_document(instance_id)

    def _instance_id_for_widget(self, widget: QWidget) -> Optional[str]:
        for instance_id, doc in self._documents.items():
            if doc is widget:
                return instance_id
        return None

    def _prompt_save_discard_cancel(self, display_name: str) -> str:
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved Changes")
        box.setText(f"'{display_name}' has unsaved changes.")
        save_button = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_button:
            return "save"
        if clicked is discard_button:
            return "discard"
        return "cancel"

    def _close_document(self, instance_id: str) -> None:
        doc = self._documents.pop(instance_id, None)
        if doc is None:
            return
        index = self.indexOf(doc)
        if index != -1:
            self.removeTab(index)
        doc.deleteLater()
        if self.currentWidget() is None or self.count() == 0:
            self.setCurrentWidget(self.scene_widget)

    # ---------------- instance lifecycle ----------------

    def _on_instance_deleted(self, instance_id: str) -> None:
        self._handle_missing_instance(instance_id)

    def _handle_missing_instance(self, instance_id: str) -> None:
        doc = self._documents.get(instance_id)
        if doc is None:
            return
        if doc.is_dirty():
            doc.mark_deleted()
            self._update_tab_title(instance_id)
            self.bridge.log("warning", f"'{doc.display_name}' was deleted, but has unsaved edits -- kept open for recovery.")
        else:
            self._close_document(instance_id)

    def _on_scene_changed(self) -> None:
        for instance_id, doc in list(self._documents.items()):
            if doc.deleted:
                continue
            obj = self.bridge.get_object(instance_id)
            if obj is None:
                # Stage 3.2: a mass world replace (new Place, Open Place)
                # never fires object_deleted for the OLD world's instances
                # -- EngineBridge.replace_scene()/sync_scene() only emit
                # scene_changed. Without this, a Script tab from a
                # replaced-away world would stay open forever pointing at
                # nothing. Single-instance deletion already reaches here
                # too (scene_changed always follows object_deleted), but by
                # then _on_instance_deleted already closed/marked it, so
                # this is a no-op for that case -- see that handler's own
                # bridge.object_deleted connection above.
                self._handle_missing_instance(instance_id)
                continue
            if obj.name != doc.display_name.rstrip("*") and not doc.conflicted:
                doc.display_name = obj.name
                self._update_tab_title(instance_id)
            authoritative_source = str(obj.properties.get("Source", ""))
            doc.refresh_source(authoritative_source)

    # ---------------- save all / dirty queries ----------------

    def dirty_documents(self) -> list[ScriptEditorDocument]:
        return [doc for doc in self._documents.values() if doc.is_dirty()]

    def has_dirty_documents(self) -> bool:
        return bool(self.dirty_documents())

    def save_all(self) -> int:
        return sum(1 for doc in self.dirty_documents() if doc.save())

    def prompt_save_all_before_closing(self) -> bool:
        """For application exit with unsaved Script tabs. Returns True if
        it is safe to proceed with closing the application."""
        dirty = self.dirty_documents()
        if not dirty:
            return True
        names = ", ".join(doc.display_name for doc in dirty)
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved Scripts")
        box.setText(f"You have unsaved changes in: {names}")
        save_all_button = box.addButton("Save All", QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton("Discard All", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_all_button:
            self.save_all()
            return True
        if clicked is discard_button:
            return True
        return False

    # ---------------- focus-aware Undo/Redo ----------------

    def focused_editor(self) -> Optional[LuaCodeEditor]:
        """The LuaCodeEditor with actual keyboard focus, or None if focus
        is elsewhere (Scene tab, Explorer, Inspector, ...). Used by
        StudioMainWindow to route Ctrl+Z/Ctrl+Y to text undo instead of
        the scene CommandManager -- see Stage 3.1 report."""
        widget = QApplication.focusWidget()
        for doc in self._documents.values():
            if widget is doc.editor:
                return doc.editor
        return None

    def focused_document(self) -> Optional[ScriptEditorDocument]:
        """Companion to focused_editor() for Ctrl+S/Save routing -- the
        owning ScriptEditorDocument (which has .save()), not just its
        LuaCodeEditor."""
        widget = QApplication.focusWidget()
        for doc in self._documents.values():
            if widget is doc.editor:
                return doc
        return None

    # ---------------- diagnostics ----------------

    def apply_diagnostics_by_script(self, diagnostics_by_script: dict[str, list]) -> None:
        for instance_id, doc in self._documents.items():
            doc.apply_diagnostics(diagnostics_by_script.get(instance_id, []))

    def clear_all_diagnostics(self) -> None:
        for doc in self._documents.values():
            doc.apply_diagnostics([])

    def navigate_to_diagnostic(self, instance_id: str, line: Optional[int]) -> None:
        if instance_id not in self._documents:
            if not self.open_script(instance_id):
                QMessageBox.information(self, "Script not found", "This Script no longer exists.")
                return
        else:
            self.setCurrentWidget(self._documents[instance_id])
        if line is not None:
            self._documents[instance_id].goto_line(line)

    def _on_lua_diagnostic(self, script_id: str, severity: str, message: str, line: Any, session_id: int) -> None:
        """Dropped (not accumulated) if session_id doesn't match the
        current Play session -- see _current_lua_session's docstring in
        __init__. Always keyed by script_id (== instance_id), never by
        display name, so two Scripts sharing a name never cross-pollute
        each other's gutter markers."""
        if session_id != self._current_lua_session:
            return
        entry = DiagnosticEntry(severity=severity, message=message, line=line)
        self._pending_diagnostics.setdefault(script_id, []).append(entry)
        doc = self._documents.get(script_id)
        if doc is not None:
            doc.apply_diagnostics(self._pending_diagnostics[script_id])

    def _on_lua_session_started(self, session_id: int) -> None:
        """Fires once per Play, whether or not that session ever reports a
        single diagnostic -- without this, a clean Play after an errored
        one would leave the previous session's stale gutter markers on
        screen forever (nothing would ever arrive to clear them), and a
        late straggler diagnostic from an already-Stopped session could
        get misattributed to whatever is currently showing."""
        self._current_lua_session = session_id
        self._pending_diagnostics.clear()
        self.clear_all_diagnostics()
