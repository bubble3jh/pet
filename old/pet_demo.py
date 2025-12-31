import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, List

from PySide6 import QtCore, QtGui, QtWidgets


# -----------------------------
# Shared inbox (JSONL)
# -----------------------------
def now_ts() -> float:
    return time.time()


def atomic_append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def overwrite_jsonl(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


# -----------------------------
# DeskPets asset mapping loader
# -----------------------------
@dataclass
class DeskPetsMapping:
    species: str
    variant: str
    states: Dict[str, str]     # state -> relative path
    defaults: Dict[str, dict]  # state -> params


def load_deskpets_mapping(desk_root: Path, species: str, variant: str) -> DeskPetsMapping:
    data_path = desk_root / "pets_data.json"
    if not data_path.exists():
        raise FileNotFoundError(f"pets_data.json not found at: {data_path}")

    data = json.loads(data_path.read_text(encoding="utf-8"))
    if species not in data:
        raise KeyError(f"species '{species}' not found in pets_data.json. Available: {list(data.keys())}")

    sp = data[species]
    states_by_variant = sp.get("states", {})
    if variant not in states_by_variant:
        raise KeyError(
            f"variant '{variant}' not found for species '{species}'. "
            f"Available variants: {list(states_by_variant.keys())}"
        )

    states = states_by_variant[variant]
    defaults = sp.get("defaults", {})

    # Validate referenced files exist
    for st, rel in states.items():
        p = (desk_root / rel).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Missing asset for state '{st}': {rel} (resolved: {p})")

    return DeskPetsMapping(species=species, variant=variant, states=states, defaults=defaults)


# -----------------------------
# Pet widget
# -----------------------------
class DeskPetWidget(QtWidgets.QWidget):
    def __init__(self, me: str, partner: str, shared_dir: Path, desk_root: Path, mapping: DeskPetsMapping):
        super().__init__()
        self.me = me
        self.partner = partner
        self.shared_dir = shared_dir
        self.inbox_path = self.shared_dir / f"inbox_{self.me}.jsonl"

        self.desk_root = desk_root
        self.mapping = mapping

        # Overlay window flags
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.Tool
            | QtCore.Qt.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)

        # UI elements: sprite label
        self.sprite = QtWidgets.QLabel(self)
        self.sprite.setStyleSheet("background: transparent;")
        self.sprite.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.sprite.setScaledContents(True)

        # speech bubble (painted)
        self._speech_text: Optional[str] = None
        self._speech_until: float = 0.0

        # Load movies per state (lazy init)
        self._movies: Dict[str, QtGui.QMovie] = {}
        self._current_state: str = "idle"
        self._forced_until: float = 0.0  # while forced, don't auto-switch

        # geometry
        self.pet_w = 96
        self.pet_h = 96
        self.bubble_w = 320
        self.bubble_h = 120
        self.resize(self.pet_w + self.bubble_w + 50, max(self.pet_h, self.bubble_h) + 50)
        self._place_initial()
        self._update_sprite_geometry()

        # movement (use DeskPets defaults if present)
        walk_speed = float(self.mapping.defaults.get("walk", {}).get("movement_speed", 3))
        self.vx = walk_speed * 0.35
        self.vy = walk_speed * 0.25

        self._dragging = False
        self._drag_offset = QtCore.QPoint(0, 0)

        # timers
        self.walk_timer = QtCore.QTimer(self)
        self.walk_timer.setInterval(20)
        self.walk_timer.timeout.connect(self._tick)
        self.walk_timer.start()

        # context menu
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._open_context_menu)

        # start idle
        self._set_state("idle", force_seconds=0.0)

        # check inbox once on launch
        QtCore.QTimer.singleShot(350, lambda: self.check_inbox(force_feedback=False))

    # ---------- window positioning ----------
    def _place_initial(self) -> None:
        screen = QtGui.QGuiApplication.primaryScreen()
        if not screen:
            self.move(400, 400)
            return
        geo = screen.availableGeometry()
        self.move(geo.x() + int(geo.width() * 0.78), geo.y() + int(geo.height() * 0.65))

    def _update_sprite_geometry(self) -> None:
        x = 18
        y = self.height() - self.pet_h - 18
        self.sprite.setGeometry(x, y, self.pet_w, self.pet_h)
        # scale current movie to label size
        mv = self._movies.get(self._current_state)
        if mv:
            mv.setScaledSize(QtCore.QSize(self.pet_w, self.pet_h))

    def resizeEvent(self, e: QtGui.QResizeEvent) -> None:
        super().resizeEvent(e)
        self._update_sprite_geometry()

    # ---------- state / animation ----------
    def _get_movie(self, state: str) -> Optional[QtGui.QMovie]:
        if state not in self.mapping.states:
            return None
        if state in self._movies:
            return self._movies[state]

        path = (self.desk_root / self.mapping.states[state]).resolve()
        mv = QtGui.QMovie(str(path))
        if not mv.isValid():
            return None

        # speed tuning: DeskPets defaults has speed_animation (float)
        speed_anim = self.mapping.defaults.get(state, {}).get("speed_animation", 1.0)
        mv.setSpeed(int(100 * float(speed_anim)))

        mv.setScaledSize(QtCore.QSize(self.pet_w, self.pet_h))
        self._movies[state] = mv
        return mv

    def _set_state(self, state: str, force_seconds: float = 0.0) -> None:
        mv = self._get_movie(state)
        if mv is None:
            return  # silently ignore missing state

        if self._current_state == state:
            return

        # stop previous
        prev = self._movies.get(self._current_state)
        if prev:
            prev.stop()

        self._current_state = state
        self.sprite.setMovie(mv)
        mv.start()

        if force_seconds > 0:
            self._forced_until = now_ts() + force_seconds

    # ---------- bubble ----------
    def _say(self, text: str, seconds: float = 8.0) -> None:
        self._speech_text = text
        self._speech_until = now_ts() + seconds
        self.update()

    # ---------- message ----------
    def _open_context_menu(self, pos: QtCore.QPoint) -> None:
        menu = QtWidgets.QMenu(self)
        send_action = menu.addAction("Send message...")
        check_action = menu.addAction("Check inbox now")
        menu.addSeparator()
        quit_action = menu.addAction("Quit")

        action = menu.exec(self.mapToGlobal(pos))
        if action == send_action:
            self._ui_send_message()
        elif action == check_action:
            self.check_inbox(force_feedback=True)
        elif action == quit_action:
            QtWidgets.QApplication.quit()

    def _ui_send_message(self) -> None:
        text, ok = QtWidgets.QInputDialog.getMultiLineText(
            self, "Send message", f"Message to {self.partner}:", ""
        )
        if not ok:
            return
        text = (text or "").strip()
        if not text:
            return

        partner_inbox = self.shared_dir / f"inbox_{self.partner}.jsonl"
        atomic_append_jsonl(
            partner_inbox,
            {
                "msg_id": str(uuid.uuid4()),
                "sender": self.me,
                "receiver": self.partner,
                "created_at": now_ts(),
                "text": text,
                "delivered": False,
            },
        )
        self._say(f"Sent to {self.partner}.", seconds=2.0)

    def check_inbox(self, force_feedback: bool = True) -> None:
        rows = read_jsonl(self.inbox_path)
        if not rows:
            if force_feedback:
                self._say("Inbox empty.", seconds=2.0)
            return

        new_msgs = []
        changed = False
        for r in rows:
            if r.get("delivered") is True:
                continue
            sender = r.get("sender", "?")
            text = (r.get("text", "") or "").strip()
            r["delivered"] = True
            r["delivered_at"] = now_ts()
            changed = True
            if text:
                new_msgs.append(f"From {sender}:\n{text}")

        if changed:
            overwrite_jsonl(self.inbox_path, rows)

        if new_msgs:
            self._say("\n\n---\n\n".join(new_msgs), seconds=12.0)
        else:
            if force_feedback:
                self._say("No new messages.", seconds=2.0)

    # ---------- input ----------
    def mousePressEvent(self, e: QtGui.QMouseEvent) -> None:
        if e.button() == QtCore.Qt.LeftButton:
            self._dragging = True
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QtGui.QMouseEvent) -> None:
        if self._dragging:
            self.move(e.globalPosition().toPoint() - self._drag_offset)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: QtGui.QMouseEvent) -> None:
        if e.button() == QtCore.Qt.LeftButton and self._dragging:
            self._dragging = False
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e: QtGui.QMouseEvent) -> None:
        if e.button() == QtCore.Qt.LeftButton:
            # play swipe if available, else just check inbox
            if "swipe" in self.mapping.states:
                hold = float(self.mapping.defaults.get("swipe", {}).get("hold", 8))
                # hold in DeskPets looks like "frames"; we approximate time
                self._set_state("swipe", force_seconds=max(0.6, hold / 10.0))
            self.check_inbox(force_feedback=True)
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    # ---------- main tick ----------
    def _tick(self) -> None:
        if self._dragging:
            return

        # movement step
        screen = QtGui.QGuiApplication.screenAt(self.geometry().center()) or QtGui.QGuiApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()

        x, y = self.x(), self.y()

        # mild drift noise
        self.vx += 0.03 * ((time.time() * 1.13) % 1 - 0.5)
        self.vy += 0.03 * ((time.time() * 1.71) % 1 - 0.5)

        # clamp
        max_v = float(self.mapping.defaults.get("walk_fast", {}).get("movement_speed", 5)) * 0.6
        self.vx = max(min(self.vx, max_v), -max_v)
        self.vy = max(min(self.vy, max_v), -max_v)

        nx, ny = x + self.vx, y + self.vy

        margin = 10
        left = geo.x() + margin
        top = geo.y() + margin
        right = geo.x() + geo.width() - self.width() - margin
        bottom = geo.y() + geo.height() - self.height() - margin

        bounced = False
        if nx < left or nx > right:
            self.vx *= -1
            nx = max(min(nx, right), left)
            bounced = True
        if ny < top or ny > bottom:
            self.vy *= -1
            ny = max(min(ny, bottom), top)
            bounced = True

        self.move(int(nx), int(ny))

        # auto state switching (unless forced)
        t = now_ts()
        if t >= self._forced_until:
            speed = (abs(self.vx) + abs(self.vy))
            if speed < 0.3:
                self._set_state("idle")
            else:
                # prefer walk_fast if very fast, else walk
                if "walk_fast" in self.mapping.states and speed > 2.2:
                    self._set_state("walk_fast")
                elif "walk" in self.mapping.states:
                    self._set_state("walk")
                else:
                    self._set_state("idle")

        # bubble expiry
        if self._speech_text and t > self._speech_until:
            self._speech_text = None
            self.update()

    # ---------- bubble paint ----------
    def paintEvent(self, e: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

        if not self._speech_text:
            return

        bubble_x = self.sprite.geometry().right() + 12
        bubble_y = max(12, self.sprite.geometry().top() - (self.bubble_h - 30))
        bubble = QtCore.QRectF(bubble_x, bubble_y, self.bubble_w, self.bubble_h)

        painter.setBrush(QtGui.QColor(255, 255, 255, 240))
        painter.setPen(QtGui.QPen(QtGui.QColor(20, 20, 20, 120), 1.5))
        painter.drawRoundedRect(bubble, 12, 12)

        tail = QtGui.QPolygonF(
            [
                QtCore.QPointF(bubble_x, bubble_y + self.bubble_h * 0.70),
                QtCore.QPointF(bubble_x - 16, bubble_y + self.bubble_h * 0.78),
                QtCore.QPointF(bubble_x, bubble_y + self.bubble_h * 0.88),
            ]
        )
        painter.drawPolygon(tail)

        painter.setPen(QtGui.QPen(QtGui.QColor(20, 20, 20, 230), 1))
        font = QtGui.QFont()
        font.setPointSize(9)
        painter.setFont(font)

        text_rect = bubble.adjusted(10, 10, -10, -10)
        painter.drawText(
            text_rect,
            QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop | QtCore.Qt.TextWordWrap,
            self._speech_text,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--me", required=True)
    parser.add_argument("--partner", required=True)
    parser.add_argument("--shared", required=True, help="Shared (synced) folder path for inbox files")
    parser.add_argument("--desk_root", default=r".\\DeskPets_repo\\deskpets", help="Extracted DeskPets folder root (has pets_data.json, media/)")
    parser.add_argument("--species", default="dog")
    parser.add_argument("--variant", default="akita")
    args = parser.parse_args()

    shared_dir = Path(args.shared).expanduser().resolve()
    desk_root = Path(args.desk_root).expanduser().resolve()

    mapping = load_deskpets_mapping(desk_root, args.species, args.variant)

    app = QtWidgets.QApplication(sys.argv)
    w = DeskPetWidget(
        me=args.me,
        partner=args.partner,
        shared_dir=shared_dir,
        desk_root=desk_root,
        mapping=mapping,
    )
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
