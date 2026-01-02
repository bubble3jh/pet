from PyQt6 import QtCore, QtGui, QtWidgets


class SpeechBubble(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self._text = ""
        self._hide_timer = QtCore.QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._w = 360
        self._h = 140
        self.resize(self._w, self._h)

    def show_text(self, text: str, seconds: float = 10.0):
        self._text = text or ""
        self.update()
        self.show()
        self.raise_()
        self._hide_timer.start(int(max(0.5, seconds) * 1000))

    def set_anchor(self, x: int, y: int):
        # x,y are screen coords
        self.move(x, y)

    def paintEvent(self, e: QtGui.QPaintEvent):
        if not self._text:
            return

        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

        bubble = QtCore.QRectF(0, 0, self.width(), self.height())
        p.setBrush(QtGui.QColor(255, 255, 255, 235))
        p.setPen(QtGui.QPen(QtGui.QColor(20, 20, 20, 110), 2))
        p.drawRoundedRect(bubble.adjusted(6, 6, -6, -16), 14, 14)

        # tail (bottom-left-ish)
        tail = QtGui.QPolygonF(
            [
                QtCore.QPointF(40, self.height() - 18),
                QtCore.QPointF(18, self.height() - 4),
                QtCore.QPointF(58, self.height() - 6),
            ]
        )
        p.drawPolygon(tail)

        p.setPen(QtGui.QPen(QtGui.QColor(10, 10, 10, 230), 1))
        font = QtGui.QFont()
        font.setPointSize(9)
        p.setFont(font)

        text_rect = bubble.adjusted(18, 18, -18, -28)
        p.drawText(
            text_rect,
            QtCore.Qt.AlignmentFlag.AlignLeft
            | QtCore.Qt.AlignmentFlag.AlignTop
            | QtCore.Qt.TextFlag.TextWordWrap,
            self._text,
        )
