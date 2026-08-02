"""AI Bridge 3.0 Desktop — PyQt6 + WebEngine wrapper for dashboard.py."""

import sys
import os
import subprocess
import time
import threading
from urllib.request import urlopen
from urllib.error import URLError

from PyQt6.QtCore import Qt, QUrl, QTimer, pyqtSignal, QObject, QPropertyAnimation, QEasingCurve
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QSystemTrayIcon, QMenu,
    QLabel, QVBoxLayout, QHBoxLayout, QWidget, QSplashScreen, QGraphicsOpacityEffect,
)
from PyQt6.QtGui import QIcon, QPixmap, QAction, QPainter, QColor, QFont, QLinearGradient, QPen
from PyQt6.QtWebEngineWidgets import QWebEngineView

DASHBOARD_URL = "http://localhost:8888"
WSL_DISTRO = "Ubuntu"
START_SCRIPT = "/mnt/d/AI/claudecode-telegram-main/windows/start.sh"
MAX_WAIT_SECONDS = 30
APP_NAME = "AI Bridge"
APP_VERSION = "3.0"

# Icon paths (relative to script dir)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ICON_ICO = os.path.join(_SCRIPT_DIR, "icon.ico")
_ICON_PNG = os.path.join(_SCRIPT_DIR, "icon_128.png")


class StatusSignal(QObject):
    ready = pyqtSignal()
    failed = pyqtSignal(str)
    log = pyqtSignal(str)


def _dashboard_ready() -> bool:
    """Return True when the local dashboard is already accepting requests."""
    try:
        with urlopen(DASHBOARD_URL, timeout=2) as response:
            return response.status == 200
    except (URLError, OSError):
        return False


def _create_icon():
    """Load app icon from icon.ico file."""
    if os.path.exists(_ICON_ICO):
        return QIcon(_ICON_ICO)
    # Fallback: programmatic icon
    pix = QPixmap(64, 64)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(101, 230, 209))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(2, 2, 60, 60)
    p.setPen(QColor(7, 17, 15))
    p.setFont(QFont("Arial", 22, QFont.Weight.Bold))
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "AI")
    p.end()
    return QIcon(pix)


def _wait_for_dashboard(signal: StatusSignal):
    """Background thread: launch WSL services, then poll dashboard until ready."""
    signal.log.emit("Checking local dashboard...")
    if _dashboard_ready():
        signal.log.emit("Dashboard is already running.")
        signal.ready.emit()
        return

    signal.log.emit("Starting WSL services...")

    # Launch start.sh inside WSL
    try:
        proc = subprocess.Popen(
            ["wsl", "-d", WSL_DISTRO, "--", "bash", START_SCRIPT],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        signal.failed.emit("WSL not found. Please install WSL and Ubuntu.")
        return
    except Exception as e:
        signal.failed.emit(f"Failed to start WSL: {e}")
        return

    # Read early output for logging
    def _drain():
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode(errors="replace").strip()
                if line:
                    signal.log.emit(line)
        except Exception:
            pass

    threading.Thread(target=_drain, daemon=True).start()

    # Poll dashboard
    signal.log.emit("Waiting for dashboard to be ready...")
    for i in range(MAX_WAIT_SECONDS):
        time.sleep(1)
        if _dashboard_ready():
            signal.log.emit("Dashboard is ready!")
            signal.ready.emit()
            return

    signal.failed.emit(f"Dashboard did not respond within {MAX_WAIT_SECONDS}s.\nCheck WSL and start.sh logs.")


class PulsingDot(QWidget):
    """A single dot that pulses opacity for loading animation."""

    def __init__(self, color="#b4a0d8", size=10, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._color = color
        self._size = size
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(0.3)
        self.setGraphicsEffect(self._opacity_effect)
        self._anim = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._anim.setDuration(800)
        self._anim.setStartValue(0.3)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._anim.setLoopCount(-1)  # infinite

    def start(self, delay_ms=0):
        QTimer.singleShot(delay_ms, self._anim.start)

    def stop(self):
        self._anim.stop()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(self._color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(0, 0, self._size, self._size)
        p.end()


class SplashWidget(QWidget):
    """Modern loading screen matching the AI Bridge 3.0 dashboard."""

    def __init__(self):
        super().__init__()
        self.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                           "stop:0 #111516, stop:0.55 #0d1112, stop:1 #090c0d);")

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setContentsMargins(40, 60, 40, 60)

        # Avatar image
        avatar_label = QLabel()
        avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if os.path.exists(_ICON_PNG):
            avatar_pix = QPixmap(_ICON_PNG).scaled(
                120, 120, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            avatar_label.setPixmap(avatar_pix)
        avatar_label.setStyleSheet("background: transparent; margin-bottom: 8px;")

        # Title
        title = QLabel(APP_NAME)
        title.setStyleSheet(
            "color: #e6ecea; font-size: 32px; font-weight: bold; "
            "font-family: 'Segoe UI', 'SF Pro Display', Arial; "
            "background: transparent; letter-spacing: 1px;"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Subtitle
        subtitle = QLabel("connecting your local intelligence...")
        subtitle.setStyleSheet(
            "color: #778382; font-size: 14px; "
            "font-family: 'Segoe UI', Arial; background: transparent;"
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Animated loading dots
        dots_container = QWidget()
        dots_container.setStyleSheet("background: transparent;")
        dots_layout = QHBoxLayout(dots_container)
        dots_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dots_layout.setSpacing(8)
        self._dots = []
        for i in range(4):
            dot = PulsingDot(color="#65e6d1", size=10, parent=dots_container)
            dots_layout.addWidget(dot)
            self._dots.append(dot)

        # Status text
        self.status = QLabel("Initializing...")
        self.status.setStyleSheet(
            "color: #acb7b5; font-size: 13px; "
            "font-family: 'Cascadia Code', 'Consolas', monospace; "
            "background: transparent; padding: 8px 16px; "
            "border: 1px solid #283032; border-radius: 8px;"
        )
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        self.status.setMaximumWidth(400)

        # Version / footer
        footer = QLabel(f"VERSION {APP_VERSION} · LOCAL INTELLIGENCE CONSOLE")
        footer.setStyleSheet(
            "color: #65706f; font-size: 11px; font-family: 'Cascadia Code', 'Consolas', monospace; "
            "background: transparent;"
        )
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addStretch(2)
        layout.addWidget(avatar_label)
        layout.addSpacing(16)
        layout.addWidget(title)
        layout.addSpacing(6)
        layout.addWidget(subtitle)
        layout.addSpacing(24)
        layout.addWidget(dots_container)
        layout.addSpacing(24)
        layout.addWidget(self.status, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(3)
        layout.addWidget(footer)

        # Start dot animations with stagger
        for i, dot in enumerate(self._dots):
            dot.start(delay_ms=i * 200)

    def set_status(self, text: str):
        self.status.setText(text)

    def stop_animation(self):
        for dot in self._dots:
            dot.stop()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setWindowIcon(_create_icon())

        # Use the desktop layout when space permits, while remaining usable on smaller screens.
        screen = QApplication.primaryScreen().availableGeometry()
        width = min(1240, max(760, int(screen.width() * 0.82)))
        height = min(920, max(640, int(screen.height() * 0.88)))
        self.resize(width, height)
        self.setMinimumSize(760, 640)
        x = screen.x() + (screen.width() - width) // 2
        y = screen.y() + (screen.height() - height) // 2
        self.move(x, y)

        # Splash (loading screen)
        self.splash = SplashWidget()
        self.setCentralWidget(self.splash)

        # WebView (created but not shown yet)
        self.webview = QWebEngineView()
        self.webview.setUrl(QUrl(DASHBOARD_URL))

        # System tray
        self._setup_tray()

        # Start services in background
        self.signal = StatusSignal()
        self.signal.ready.connect(self._on_ready)
        self.signal.failed.connect(self._on_failed)
        self.signal.log.connect(self._on_log)

        self._starter = threading.Thread(target=_wait_for_dashboard, args=(self.signal,), daemon=True)
        self._starter.start()

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(_create_icon(), self)
        menu = QMenu()

        show_action = QAction("Show Window", self)
        show_action.triggered.connect(self._show_window)
        menu.addAction(show_action)

        menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._full_quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_clicked)
        self.tray.setToolTip(f"{APP_NAME} {APP_VERSION}")
        self.tray.show()

    def _on_ready(self):
        """Dashboard is up — switch from splash to webview."""
        self.webview.setUrl(QUrl(DASHBOARD_URL))
        self.setCentralWidget(self.webview)

    def _on_failed(self, msg: str):
        self.splash.set_status(f"ERROR: {msg}")

    def _on_log(self, msg: str):
        self.splash.set_status(msg)

    def _show_window(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _tray_clicked(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_window()

    def closeEvent(self, event):
        """Minimize to tray instead of closing."""
        event.ignore()
        self.hide()
        self.tray.showMessage(
            APP_NAME,
            "已最小化到系统托盘，右键托盘图标可退出。",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )

    def _full_quit(self):
        """Stop all WSL services and exit."""
        self.tray.hide()
        try:
            subprocess.Popen(
                [
                    "wsl", "-d", WSL_DISTRO, "--", "bash", "-c",
                    "pkill -f dashboard.py; pkill -f bridge.py; pkill -f litellm; "
                    "pkill -f feishu_bridge.py; "
                    "tmux kill-session -t claude 2>/dev/null; tmux kill-session -t bridge 2>/dev/null; "
                    "tmux kill-session -t tunnel 2>/dev/null"
                ],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
        QApplication.quit()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # keep alive in tray

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
