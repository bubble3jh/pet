import ctypes
import os
import traceback
from .bubble import SpeechBubble
from .messaging import send_message, fetch_undelivered

from PyQt6 import QtWidgets, QtGui, QtCore

from .credits import BrowserWindow
from .petworker import PetWorker, load_pets
from .remove_alpha import GifHelper
from .selector import PetSelector
from .settings import Settings
from .size import SizeSettings
from .windows_API import POINT, SIZE, BLENDFUNCTION, Windows, is_full_screen

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

BASE_DIR = os.path.dirname(__file__)
LOGO_DIR = os.path.join(BASE_DIR, "logo.ico")

ULW_ALPHA = 0x2

STATE_FULLSCREEN = False


def draw_pet_frame(pet, frame_image):
    try:
        if getattr(pet, "hwnd", None):
            hbitmap = GifHelper.pil_to_hbitmap(frame_image)
            draw_frame(pet, hbitmap)
            gdi32.DeleteObject(hbitmap)
    except Exception as e:
        print(e)
        traceback.print_exc()


def draw_frame(self, hbitmap):
    global STATE_FULLSCREEN
    try:
        full_screen_now = is_full_screen()
        if full_screen_now != STATE_FULLSCREEN:
            STATE_FULLSCREEN = full_screen_now
            if getattr(self, "main_window", None):
                self.main_window.start_refresh()

        hdc_screen = user32.GetDC(None)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        gdi32.SelectObject(hdc_mem, hbitmap)

        blend = BLENDFUNCTION()
        blend.BlendOp = 0
        blend.BlendFlags = 0
        blend.SourceConstantAlpha = 255
        blend.AlphaFormat = 1

        pt_pos = POINT(self.x, self.y)
        size = SIZE(self.width, self.height)
        pt_src = POINT(0, 0)

        user32.UpdateLayeredWindow(self.hwnd, hdc_screen, ctypes.byref(pt_pos),
                                   ctypes.byref(size), hdc_mem, ctypes.byref(pt_src),
                                   0, ctypes.byref(blend), ULW_ALPHA)

        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)

    except Exception as e:
        print(e)
        traceback.print_exc()


def close(pet):
    try:
        if getattr(pet, "hbitmaps", None):
            for hb in pet.hbitmaps:
                try:
                    gdi32.DeleteObject(hb)
                except Exception:
                    pass
        pet.hbitmaps = []

        if getattr(pet, "hwnd", None):
            ctypes.windll.user32.DestroyWindow(pet.hwnd)
            pet.hwnd = None

        pet.current_frame = 0
    except Exception as e:
        print(e)
        traceback.print_exc()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, app, me=None, partner=None, shared_dir=None):
        try:
            super().__init__()
            # Messaging config (optional)
            self.me = me
            self.partner = partner
            self.shared_dir = shared_dir  # Path or None

            # Bubble overlay
            self.bubble = SpeechBubble()
            self._bubble_follow_timer = QtCore.QTimer(self)
            self._bubble_follow_timer.setInterval(120)
            self._bubble_follow_timer.timeout.connect(self._follow_first_pet)
            self._bubble_follow_timer.start()

            # Inbox polling timer
            self._msg_timer = QtCore.QTimer(self)
            self._msg_timer.setInterval(30_000)  # 30 sec
            self._msg_timer.timeout.connect(self.check_messages)
            self._msg_timer.start()
            self.setWindowTitle("Pet Manager")
            self.resize(800, 600)
            self.setWindowIcon(QtGui.QIcon(LOGO_DIR))
            app.setWindowIcon(QtGui.QIcon(LOGO_DIR))
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(u"DeskPets")

            # Tabs
            self.tabs = QtWidgets.QTabWidget()
            self.setCentralWidget(self.tabs)
            self.tab_add_pet = QtWidgets.QWidget()
            self.tab_size_pet = QtWidgets.QWidget()
            self.tab_settings = QtWidgets.QWidget()
            self.tab_credits = QtWidgets.QWidget()
            self.tabs.addTab(self.tab_add_pet, "Add Pets")
            self.tabs.addTab(self.tab_size_pet, "Size Pets")
            self.tabs.addTab(self.tab_settings, "Settings")
            self.tabs.addTab(self.tab_credits, "Credits")

            self.tab_add_pet.setLayout(QtWidgets.QVBoxLayout())
            self.pet_selector = PetSelector(self)
            self.tab_add_pet.layout().addWidget(self.pet_selector)

            self.tab_size_pet.setLayout(QtWidgets.QVBoxLayout())
            self.size_settings = SizeSettings(self)
            self.tab_size_pet.layout().addWidget(self.size_settings)

            self.tab_settings.setLayout(QtWidgets.QVBoxLayout())
            self.settings_settings = Settings(self)
            self.tab_settings.layout().addWidget(self.settings_settings)

            self.tab_credits.setLayout(QtWidgets.QVBoxLayout())
            self.browser_window = BrowserWindow()
            self.tab_credits.layout().addWidget(self.browser_window)

            # Tray
            self.tray_icon = QtWidgets.QSystemTrayIcon(self)
            self.tray_icon.setIcon(QtGui.QIcon(LOGO_DIR))
            show_action = QtGui.QAction("Show", self)
            refresh_action = QtGui.QAction("Refresh", self)
            quit_action = QtGui.QAction("Quit", self)
            # added
            send_msg_action = QtGui.QAction("Send Message", self)
            check_msg_action = QtGui.QAction("Check Messages", self)

            send_msg_action.triggered.connect(self.send_message_ui)
            check_msg_action.triggered.connect(self.check_messages)

            show_action.triggered.connect(self.show_window)
            refresh_action.triggered.connect(self.start_refresh)
            quit_action.triggered.connect(QtWidgets.QApplication.instance().quit)
            tray_menu = QtWidgets.QMenu()
            tray_menu.addAction(show_action)
            tray_menu.addAction(refresh_action)
            tray_menu.addSeparator()
            tray_menu.addAction(quit_action)
            #added
            tray_menu.addAction(send_msg_action)
            tray_menu.addAction(check_msg_action)
            tray_menu.addSeparator()

            self.tray_icon.setContextMenu(tray_menu)
            self.tray_icon.show()

            # Pets
            self.pets = []
            self.worker = None
        except Exception as e:
            print(e)
            traceback.print_exc()
            
    def _first_pet(self):
        pets = getattr(self, "pets", []) or []
        return pets[0] if pets else None

    def _follow_first_pet(self):
        """
        Keep bubble near the first pet while visible.
        """
        try:
            if not self.bubble.isVisible():
                return
            pet = self._first_pet()
            if not pet:
                return
            # Pet coordinates are screen coords (WinAPI)
            bx = int(pet.x + pet.width + 10)
            by = int(pet.y - 10)
            self.bubble.set_anchor(bx, by)
        except Exception:
            pass

    def send_message_ui(self):
        """
        Tray action: prompt and write to partner inbox.
        Requires: --me, --partner, --shared
        """
        try:
            if not self.shared_dir or not self.me or not self.partner:
                self.tray_icon.showMessage(
                    "DeskPets Messaging",
                    "Messaging not configured.\nRun with --me --partner --shared.",
                    QtWidgets.QSystemTrayIcon.MessageIcon.Warning,
                    4000,
                )
                return

            text, ok = QtWidgets.QInputDialog.getMultiLineText(
                self, "Send Message", f"To {self.partner}:", ""
            )
            if not ok:
                return
            text = (text or "").strip()
            if not text:
                return

            send_message(self.shared_dir, sender=self.me, receiver=self.partner, text=text)
            self.tray_icon.showMessage(
                "DeskPets Messaging",
                f"Sent to {self.partner}.",
                QtWidgets.QSystemTrayIcon.MessageIcon.Information,
                2000,
            )
        except Exception as e:
            try:
                self.tray_icon.showMessage(
                    "DeskPets Messaging",
                    f"Send failed: {e}",
                    QtWidgets.QSystemTrayIcon.MessageIcon.Critical,
                    4000,
                )
            except Exception:
                pass

    def check_messages(self, force_feedback: bool = False):
        """
        Poll inbox and display new messages as a bubble near the first pet.
        """
        try:
            if not self.shared_dir or not self.me:
                if force_feedback:
                    self.tray_icon.showMessage(
                        "DeskPets Messaging",
                        "Messaging not configured.\nRun with --me --partner --shared.",
                        QtWidgets.QSystemTrayIcon.MessageIcon.Warning,
                        4000,
                    )
                return

            msgs = fetch_undelivered(self.shared_dir, user_id=self.me)
            if not msgs:
                if force_feedback:
                    self.bubble.show_text("Inbox empty.", seconds=3.0)
                return

            # Build bubble text (cap length to avoid enormous bubble)
            chunks = []
            for m in msgs[:5]:
                sender = m.get("sender", "?")
                text = (m.get("text", "") or "").strip()
                if text:
                    chunks.append(f"From {sender}:\n{text}")

            if not chunks:
                return

            bubble_text = "\n\n---\n\n".join(chunks)
            if len(bubble_text) > 800:
                bubble_text = bubble_text[:800] + "\n...\n"

            pet = self._first_pet()
            if pet:
                bx = int(pet.x + pet.width + 10)
                by = int(pet.y - 10)
                self.bubble.set_anchor(bx, by)

            self.bubble.show_text(bubble_text, seconds=12.0)
        except Exception:
            # keep silent to avoid crashing the app loop
            pass

    def start_refresh(self):
        try:
            if self.worker:
                self.worker.stop()
            for pet in getattr(self, "pets", []):
                close(pet)
            # self.pets = load_pets() or []

            self.pets = load_pets() or []
            for pet in self.pets:
                pet.main_window = self

            self.worker = PetWorker(self.pets)
            self.worker.frame_ready.connect(draw_pet_frame)
            self.worker.start()
        except Exception as e:
            print(e)
            traceback.print_exc()

    def closeEvent(self, event):
        try:
            event.ignore()
            self.hide()
            self.tray_icon.showMessage(
                "Pet Manager", "Application minimized to tray",
                QtWidgets.QSystemTrayIcon.MessageIcon.Information, 2000
            )
        except Exception as e:
            print(e)
            traceback.print_exc()

    def show_window(self):
        try:
            self.show()
            self.raise_()
            self.activateWindow()
        except Exception as e:
            print(e)
            traceback.print_exc()
