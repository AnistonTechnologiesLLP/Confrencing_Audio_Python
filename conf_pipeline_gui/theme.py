"""The "Conduit" design system: two palette dicts -> one QSS template.

QSS is a constrained CSS subset (no variables / box-shadow / transitions), so
the palette substitution happens here in Python. ``canvas_*`` roles are read by
the canvas painter directly (QSS cannot style custom QPainter content).
"""
from __future__ import annotations

PALETTES = {
    "dark": dict(
        bg="#0a0a0e", surface="#0e0e13", surface2="#13131a", surface3="#181820", hover="#1f1f2a", elev="#1a1a23",
        chrome="#08080c",
        border="#262633", border_soft="#1c1c26", border_strong="#33333f",
        text="#edeef4", text_dim="#c4c5d2", muted="#9197ab", faint="#646a82",
        accent="#6d8bff", accent_bright="#85a0ff", accent_press="#5a78f0", on_accent="#070a16",
        sel="#1b2138", ok="#3ddc97", warn="#f7c948", err="#ff6b81",
        canvas_bg="#0a0e18", canvas_grid="#172238", canvas_axis="#2c3760",
        canvas_text="#c4c5d2", canvas_text_dim="#8a93ad",
    ),
    "light": dict(
        bg="#f5f6f9", surface="#ffffff", surface2="#f7f8fb", surface3="#eef0f5", hover="#e7eaf1", elev="#ffffff",
        chrome="#eceef4",
        border="#e1e3eb", border_soft="#eaecf1", border_strong="#cfd2dd",
        text="#14151c", text_dim="#33353f", muted="#5b6075", faint="#777c90",
        accent="#5871f2", accent_bright="#4a63ec", accent_press="#4a63ec", on_accent="#ffffff",
        sel="#e6ebfd", ok="#0fae72", warn="#b8860b", err="#e23b59",
        canvas_bg="#e9edf5", canvas_grid="#d4dae8", canvas_axis="#b9c2da",
        canvas_text="#33353f", canvas_text_dim="#5b6075",
    ),
}

_QSS_TEMPLATE = """
QMainWindow, QWidget {{ background: {bg}; color: {text}; }}
QToolTip {{ background: {elev}; color: {text}; border: 1px solid {border_strong}; padding: 4px 7px; border-radius: 6px; }}

QToolButton {{ background: transparent; color: {text_dim}; border: 1px solid transparent; border-radius: 8px; padding: 6px 11px; font-weight: 500; }}
QToolButton:hover {{ background: {hover}; color: {text}; }}
QToolButton:pressed {{ background: {surface3}; }}
QToolButton:checked {{ background: {surface3}; color: {accent_bright}; border: 1px solid {border_strong}; }}
QToolButton:disabled {{ color: {faint}; }}
QToolButton::menu-indicator {{ image: none; }}

QPushButton {{ background: {surface2}; color: {text_dim}; border: 1px solid {border}; border-radius: 8px; padding: 6px 12px; font-weight: 500; }}
QPushButton:hover {{ background: {hover}; border-color: {border_strong}; color: {text}; }}
QPushButton:pressed {{ background: {surface3}; }}
QPushButton:disabled {{ color: {faint}; }}
QPushButton[accent="true"] {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {accent_bright}, stop:1 {accent}); color: {on_accent}; border: 1px solid {accent}; font-weight: 700; }}
QPushButton[accent="true"]:hover {{ background: {accent_bright}; }}
QPushButton[accent="true"]:pressed {{ background: {accent_press}; }}

QMenu {{ background: {elev}; color: {text}; border: 1px solid {border_strong}; border-radius: 9px; padding: 5px; }}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {sel}; color: {text}; }}
QMenu::item:disabled {{ color: {faint}; }}
QMenu::separator {{ height: 1px; background: {border}; margin: 5px 8px; }}

QGroupBox {{ background: {surface2}; border: 1px solid {border}; border-radius: 12px; margin-top: 15px; padding: 11px 12px 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 12px; top: 1px; padding: 1px 6px; color: {muted}; font-weight: 600; background: {surface3}; border: 1px solid {border}; border-radius: 5px; }}

QLineEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox {{ background: {surface3}; color: {text}; border: 1px solid {border}; border-radius: 8px; padding: 6px 8px; selection-background-color: {accent}; selection-color: {on_accent}; }}
QLineEdit:hover, QComboBox:hover, QAbstractSpinBox:hover {{ border-color: {border_strong}; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {{ border-color: {accent}; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right; width: 18px; border: 0; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 0; height: 0; border: 0; }}
QComboBox QAbstractItemView {{ background: {elev}; color: {text}; border: 1px solid {border_strong}; border-radius: 8px; padding: 3px; selection-background-color: {accent}; selection-color: {on_accent}; outline: 0; }}

QListWidget {{ background: {surface3}; border: 1px solid {border}; border-radius: 9px; padding: 4px; outline: 0; }}
QListWidget::item {{ background: transparent; color: {text}; border: 1px solid transparent; border-radius: 7px; padding: 7px 9px; margin: 2px 1px; }}
QListWidget::item:hover {{ background: {hover}; }}
QListWidget::item:selected {{ background: {sel}; color: {text}; border: 1px solid {accent}; }}

QTabWidget::pane {{ border: 1px solid {border}; border-radius: 0; top: -1px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: {muted}; padding: 9px 13px; border: 0; margin-right: 2px; font-weight: 600; }}
QTabBar::tab:hover {{ color: {text}; }}
QTabBar::tab:selected {{ color: {text}; border-bottom: 2px solid {accent}; }}

QCheckBox {{ color: {text}; spacing: 7px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {border_strong}; background: {surface}; }}
QCheckBox::indicator:hover {{ border-color: {accent}; }}
QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent}; }}
QRadioButton {{ color: {text}; spacing: 7px; }}
QRadioButton::indicator {{ width: 15px; height: 15px; border-radius: 8px; border: 1px solid {border_strong}; background: {surface}; }}
QRadioButton::indicator:checked {{ background: {accent}; border-color: {accent}; }}

QSlider::groove:horizontal {{ height: 4px; background: {surface}; border: 1px solid {border}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {text_dim}; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px; border: 1px solid {border_strong}; }}
QSlider::handle:horizontal:hover {{ background: {accent_bright}; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {border_strong}; min-height: 28px; border-radius: 5px; margin: 2px; }}
QScrollBar::handle:vertical:hover {{ background: {muted}; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {border_strong}; min-width: 28px; border-radius: 5px; margin: 2px; }}
QScrollBar::handle:horizontal:hover {{ background: {muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QLabel {{ color: {text_dim}; }}
QFrame[card="true"] {{ border: 1px solid {border}; border-radius: 7px; }}
QFrame[frameShape="4"], QFrame[frameShape="5"] {{ color: {border}; background: {border}; max-height: 1px; }}
QStatusBar {{ background: {surface}; color: {muted}; border-top: 1px solid {border}; }}
QStatusBar::item {{ border: 0; }}

/* ---- Stagebar shell chrome ---- */
QFrame[topbar="true"] {{ background: {surface}; border-bottom: 1px solid {border}; }}
QFrame[topbar="true"] QToolButton {{ padding: 5px 10px; }}
QToolButton[modeButton="true"] {{ background: transparent; color: {muted}; border: 1px solid transparent; border-radius: 8px; padding: 5px 13px; font-weight: 700; }}
QToolButton[modeButton="true"]:hover {{ background: {hover}; color: {text}; }}
QToolButton[modeButton="true"]:checked {{ background: {surface3}; color: {accent_bright}; border: 1px solid {border_strong}; }}

QFrame[toolrail="true"] {{ background: {surface}; border-right: 1px solid {border}; }}
QToolButton[railButton="true"] {{ border-radius: 9px; padding: 4px 2px; font-size: 8pt; font-weight: 600; color: {muted}; }}
QToolButton[railButton="true"]:hover {{ background: {hover}; color: {text}; }}
QToolButton[railButton="true"]:checked {{ background: {sel}; color: {accent_bright}; border: 1px solid {accent}; }}

QToolButton[pill="true"] {{ background: {surface2}; color: {text_dim}; border: 1px solid {border}; border-radius: 12px; padding: 4px 13px; font-weight: 600; }}
QToolButton[pill="true"]:hover {{ background: {hover}; }}
QToolButton[pill="true"][level="ok"] {{ border-color: {ok}; color: {ok}; }}
QToolButton[pill="true"][level="warn"] {{ border-color: {warn}; color: {warn}; }}
QToolButton[pill="true"][level="error"] {{ border-color: {err}; color: {err}; }}

QFrame[viewbar="true"] {{ background: {elev}; border: 1px solid {border_strong}; border-radius: 9px; }}
QFrame[viewbar="true"] QToolButton {{ padding: 3px 9px; font-weight: 600; }}

QLabel[hintChip="true"] {{ background: {sel}; color: {accent_bright}; border: 1px solid {accent}; border-radius: 9px; padding: 3px 10px; font-size: 8pt; font-weight: 600; }}
QLabel[panelTitle="true"] {{ color: {text}; font-size: 12pt; font-weight: 800; }}
QLabel[sectionLabel="true"] {{ color: {faint}; font-size: 9px; font-weight: 800; letter-spacing: 1px; }}

QFrame[transport="true"] {{ background: {surface}; border-top: 1px solid {border_strong}; }}
QFrame[drawer="true"] {{ background: {surface2}; border-left: 1px solid {border_strong}; }}
QPushButton[cardHeader="true"] {{ background: transparent; border: 0; color: {text}; font-weight: 700; text-align: left; padding: 6px 4px; }}
QPushButton[cardHeader="true"]:hover {{ color: {accent_bright}; }}

QFrame[guidePanel="true"] {{ background: {surface2}; border-bottom: 1px solid {border}; }}
QLabel[guideHeader="true"] {{ color: {text}; font-weight: 800; font-size: 12px; }}
QLabel[guideSub="true"] {{ color: {muted}; font-size: 11px; padding-left: 8px; }}
QLabel[guideArrow="true"] {{ color: {faint}; font-size: 13px; }}
QWidget[guideStep="true"] {{ background: {surface3}; border: 1px solid {border}; border-radius: 9px; }}
QWidget[guideStep="true"][done="true"] {{ background: {sel}; border: 1px solid {ok}; }}
QLabel[guideDot="true"] {{ color: {muted}; font-size: 13px; font-weight: 700; }}
QLabel[guideTitle="true"] {{ color: {text_dim}; font-weight: 600; }}
QLabel[guideTitle="true"][done="true"] {{ color: {ok}; }}
QWidget[guideStep="true"] QPushButton {{ background: {accent}; color: {on_accent}; border: 0; border-radius: 6px; padding: 4px 9px; font-weight: 700; }}
QWidget[guideStep="true"] QPushButton:hover {{ background: {accent_bright}; }}

QLabel[inspectorBanner="true"] {{ background: {surface2}; border-bottom: 1px solid {border}; font-size: 12px; }}
QLabel[inspectorBanner="true"][level="error"] {{ background: {surface2}; border-bottom: 1px solid {err}; }}
QLabel[inspectorBanner="true"][level="warn"] {{ background: {surface2}; border-bottom: 1px solid {warn}; }}
QLabel[inspectorBanner="true"][level="ok"] {{ background: {surface2}; border-bottom: 1px solid {ok}; }}
"""


def build_qss(theme: str) -> str:
    return _QSS_TEMPLATE.format(**PALETTES[theme])


def palette(theme: str) -> dict:
    return PALETTES[theme]


DARK_QSS = build_qss("dark")
LIGHT_QSS = build_qss("light")
