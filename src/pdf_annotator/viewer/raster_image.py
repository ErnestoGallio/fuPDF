"""Shared Qt RGB adoption; the consumer accounts for its owned resident image."""

from PySide6.QtGui import QImage

from ..worker.client import Delivery


def adopt_raster(delivery: Delivery) -> QImage:
    response = delivery.response
    if response is None or not response.success or not response.pixels:
        raise ValueError("Delivery contains no raster")
    p = response.payload
    try:
        formats = {"RGB888": QImage.Format.Format_RGB888,
                   "RGBA8888": QImage.Format.Format_RGBA8888_Premultiplied}
        image_format = formats.get(p["format"])
        if image_format is None:
            raise ValueError("Unsupported raster format")
        borrowed = QImage(response.pixels, p["width"], p["height"], p["stride"], image_format)
        owned = borrowed.copy()
        if owned.isNull():
            raise MemoryError("Qt raster allocation failed")
        return owned
    finally:
        # copy() detaches the Qt storage before dropping the transport reservation.
        delivery.release()


adopt_rgb = adopt_raster
