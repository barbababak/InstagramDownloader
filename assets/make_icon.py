"""Draws assets/icon.ico. Only needed if the icon is to be changed."""
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPen, QPolygonF

SIZE = 256


def draw() -> QImage:
    image = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    gradient = QLinearGradient(0, SIZE, SIZE, 0)
    gradient.setColorAt(0, QColor("#f9a825"))
    gradient.setColorAt(0.5, QColor("#e1306c"))
    gradient.setColorAt(1, QColor("#7b2ff7"))
    painter.setBrush(QBrush(gradient))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(QRectF(8, 8, 240, 240), 56, 56)

    white = QColor("white")
    painter.setBrush(white)
    painter.drawRect(QRectF(108, 46, 40, 76))
    painter.drawPolygon(QPolygonF([QPointF(68, 116), QPointF(188, 116), QPointF(128, 176)]))
    painter.setPen(QPen(white, 22, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(QPointF(68, 204), QPointF(188, 204))
    painter.end()
    return image


if __name__ == "__main__":
    app = QGuiApplication([])
    target = Path(__file__).with_name("icon.ico")
    if not draw().save(str(target), "ICO"):
        raise SystemExit(f"could not write {target}")
    print(target, target.stat().st_size)
