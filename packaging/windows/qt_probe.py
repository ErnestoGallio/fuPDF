"""Stock-hook frozen Qt import probe; deliberately independent of fuPDF."""
import PySide6
from PySide6 import QtCore, QtGui, QtWidgets

print("PySide6:", PySide6.__version__)
print("Qt:", QtCore.qVersion())
print("QtCore OK; QtGui OK; QtWidgets OK", flush=True)
