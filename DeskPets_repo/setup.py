import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple, Dict

from PySide6 import QtCore, QtGui, QtWidgets


# -----------------------------
# Message I/O (shared jsonl inbox)
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
# Asset discovery (DeskPets img/)
# - Prefer GIF
# - Fallback to PNG sequence grouped by prefix
# -----------------------------
IMAGE_EXTS = {".gif", ".png", ".webp", ".jpg", ".jpeg"}


def _is_image(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in IMAGE_EXTS


def discover_assets(assets_root: Path) -> List[Path]:
    if not assets_root.exists():
        return []
    return [p for p in assets_root.rglob("*") if _is_image(p)]


def score_match(path: Path, pet_keyword: str) -> int:
    """
    Higher is better. This is a fuzzy-ish scorer.
    """
    key = pet_keyword.lower().strip()
    s = str(path).lower()
    score = 0
    if key and key in s:
        score += 50
    # prefer gif
    if path.suffix.lower() == ".gif":
        score += 30
    # prefer idle-ish naming
    if "idle" in s:
        score += 10
    if "walk" in s or "run" in s:
        score += 6
    # prefer shorter paths (often more "direct")
    score += max(0, 10 - len(path.parts) // 3)
    return score


def pick_best_gif(images: List[Path], pet_keyword: str) -> Optional[Path]:
    gifs = [p for p in images if p.suffix.lower() == ".gif"]
    if not gifs:
        return None
    gifs.sort(key=lambda p: score_match(p, pet_keyword), reverse=True)
    return gifs[0]


def group_png_sequences(images: List[Path], pet_keyword: str) -> Dict[str, List[Path]]:
    """
    Group PNGs by a simple key: filename stem with trailing digits removed.
    Example: dog_walk_001.png, dog_walk_002.png -> key: dog_walk_
    """
    import re

    seqs: Dict[str, List[Path]] = {}
    for p in images:
        if p.suffix.lower() != ".png":
            continue
        s = str(p).lower()
        if pet_keyword and pet_keyword.lower() not in s:
            continue
        stem = p.stem
        key = re.sub(r"\d+$", "", stem)
        seqs.setdefault(key, []).append(p)

    # sort each sequence by filename
    for k in list(seqs.keys()):
        seqs[k].sort(key=lambda x: x.name)
        # drop tiny groups
        if len(seqs[k]) < 2:
            seqs.pop(k, None)
    return seqs


@dataclass
class SpriteSource:
    gif_path: Optional[Path] = None
    png_frames: Optional[List[Path]] = None


def choose_sprite_source(assets_root: Path, pet_keyword: str) -> SpriteSource:
    images = discover_assets(assets_root)
    if not images:
        return SpriteSource()

    best_gif = pick_best_gif(images, pet_keyword)
    if best_gif:
        return SpriteSource(gif_path=best_gif)

    # fallback: pick best PNG sequence (prefer idle > walk > others)
    seqs = group_png_sequences(images, pet_keyword)
    if not seqs:
        return SpriteSource()

    def seq_score(key: str, frames: List[Path]) -> int:
        s = key.lower()
        score = 0
        if "idle" in s:
            score += 25
        if "walk" in s or "run" in s:
            score += 12
        score += min(len(frames), 20)  # more frames is usually better
        # if keyword appears in key, bump
        if pet_keyword and pet_keyword.lower() in s:
            score += 10
        return score

    best_key = max(seqs.keys(), key=lambda k: seq_score(k, seqs[k]))
    return SpriteSource(png_frames=seqs[best_key])


# -----------------------------
# Pet Widget
# -----------------------------
class PetWidget(QtWidgets.QWidget):
    def __init__(self, me: str, partner: str, shared_dir: Path, assets_root: Path, pet_keyword: str):
        super().__init__()
        self.me = me
        self.partner = partner
        self.shared_dir = shared_dir
        self.inbox_path = self.shared_dir / f"inbox_{self.me}.jsonl"

        # overlay window flags
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.Tool
            | QtCore.Qt.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)

        # Layout: sprite label + bubble (custom painted)
        self.sprite_label = QtWidgets.QLabel(self)
        self.sprite_label.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.sprite_label.setStyleSheet("background: transparent;")
        self.sprite_label.setScaledContents(True)

        self._movie: Optional[QtGui.QMovie] = None
        self._png_frames: Optional[List[QtGui.QPixmap]] = None
        self._png_index = 0

        self._speech_text: Optional[str] = None
        self._speech_until: float = 0.0

        # Load sprite from DeskPets assets
        src = choose_sprite_source(assets_root, pet_keyword)
        if src.gif_path:
            self._load_gif(src.gif_path)
        elif src.png_frames:
            self._load_png_sequence(src.png_frames)
        else:
            # hard fallback: show a placeholder so user notices asset path mismatch
            self.sprite_label.setText("No assets found")
            self.sprite_label.setStyleSheet("color: white; background: rgba(0,0,0,120); padding: 6px;")

        # reasonable default geometry
        self.pet_w = 96
        self.pet_h = 96
        self.bubble_w = 300
        self.bubble_h = 120

        # Window size accounts for bubble on the right
        self.resize(self.pet_w + self.bubble_w + 40, max(self.pet_h, self.bubble_h) + 40)
        self._place_initial()

        # position sprite label near bottom-left of window
        self._update_sprite_geometry()

        # movement
        self.vx = 1.2
        self.vy = 0.8
        self._dragging = False
        self._drag_offset = QtCore.QPoint(0, 0)

        self.walk_timer = QtCore.QTimer(self)
        self.walk_timer.setInterval(20)
        self.walk_timer.timeout.connect(self._walk_step)
        self.walk_timer.start()

        # png animation timer (only if using png frames)
        self.png_timer = QtCore.QTimer(self)
        self.png_timer.setInterval(90)  # ~11 fps
        self.png_timer.timeout.connect(self._tick_png)
        if self._png_frames:
            self.png_timer.start()

        # context menu
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._open_context_menu)

        # check inbox once on launch
        QtCore.QTimer.singleShot(300, lambda: self.check_inbox(force_feedback=False))

    def _place_initial(self) -> None:
        screen = QtGui.QGuiApplication.primaryScreen()
        if not screen:
            self.move(400, 400)
            return
        geo = screen.availableGeometry()
        self.move(geo.x() + int(geo.width() * 0.78), geo.y() + int(geo.height() * 0.65))

    def _update_sprite_geometry(self) -> None:
        # sprite in bottom-left
        x = 15
        y = self.height() - self.pet_h - 15
        self.sprite_label.setGeometry(x, y, self.pet_w, self.pet_h)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_sprite_geometry()

    # ---------- Sprite loaders ----------
    def _load_gif(self, gif_path: Path) -> None:
        movie = QtGui.QMovie(str(gif_path))
        if movie.isValid():
            self._movie = movie
            self.sprite_label.setMovie(movie)
            movie.start()
        else:
            self._say(f"Invalid GIF: {gif_path.name}", seconds=3.0)

    def _load_png_sequence(self, frames: List[Path]) -> None:
        pix = []
        for p in frames:
            pm = QtGui.QPixmap(str(p))
            if not pm.isNull():
                pix.append(pm)
        if pix:
            self._png_frames = pix
            self.sprite_label.setPixmap(pix[0])
        else:
            self._say("PNG frames failed to load.", seconds=3.0)

    def _tick_png(self) -> None:
        if not self._png_frames:
            return
        self._png_index = (self._png_index + 1) % len(self._png_frames)
        self.sprite_label.setPixmap(self._png_frames[self._png_index])

    # ---------- Menu / message ----------
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

    def _say(self, text: str, seconds: float = 8.0) -> None:
        self._speech_text = text
        self._speech_until = now_ts() + seconds
        self.update()

    # ---------- Drag / double-click ----------
    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            self._dragging = True
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._dragging:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton and self._dragging:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            self.check_inbox(force_feedback=True)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    # ---------- Walk loop ----------
    def _walk_step(self) -> None:
        if self._dragging:
            return

        screen = QtGui.QGuiApplication.screenAt(self.geometry().center())
        if screen is None:
            screen = QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return

        geo = screen.availableGeometry()
        x, y = self.x(), self.y()

        # mild drift
        self.vx += 0.02 * ((time.time() * 1.17) % 1 - 0.5)
        self.vy += 0.02 * ((time.time() * 1.53) % 1 - 0.5)
        self.vx = max(min(self.vx, 2.0), -2.0)
        self.vy = max(min(self.vy, 2.0), -2.0)

        nx = x + self.vx
        ny = y + self.vy

        margin = 10
        left = geo.x() + margin
        top = geo.y() + margin
        right = geo.x() + geo.width() - self.width() - margin
        bottom = geo.y() + geo.height() - self.height() - margin

        if nx < left or nx > right:
            self.vx *= -1
            nx = max(min(nx, right), left)
        if ny < top or ny > bottom:
            self.vy *= -1
            ny = max(min(ny, bottom), top)

        self.move(int(nx), int(ny))

        # expire bubble
        if self._speech_text and now_ts() > self._speech_until:
            self._speech_text = None
            self.update()

    # ---------- Paint bubble ----------
    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

        if not self._speech_text:
            return

        # bubble to the right of sprite
        bubble_x = self.sprite_label.geometry().right() + 12
        bubble_y = max(12, self.sprite_label.geometry().top() - (self.bubble_h - 30))
        bubble = QtCore.QRectF(bubble_x, bubble_y, self.bubble_w, self.bubble_h)

        painter.setBrush(QtGui.QColor(255, 255, 255, 240))
        painter.setPen(QtGui.QPen(QtGui.QColor(20, 20, 20, 120), 1.5))
        painter.drawRoundedRect(bubble, 12, 12)

        # tail toward sprite
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
    parser.add_argument("--shared", required=True, help="Shared (synced) folder path")
    parser.add_argument("--assets_root", required=True, help="DeskPets img/ directory path")
    parser.add_argument("--pet", default="dog", help="Keyword to pick pet assets (e.g., dog, cat)")
    args = parser.parse_args()

    shared_dir = Path(args.shared).expanduser().resolve()
    assets_root = Path(args.assets_root).expanduser().resolve()

    app = QtWidgets.QApplication(sys.argv)
    w = PetWidget(
        me=args.me,
        partner=args.partner,
        shared_dir=shared_dir,
        assets_root=assets_root,
        pet_keyword=args.pet,
    )
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
