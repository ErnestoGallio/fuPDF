"""Qt- and PyMuPDF-independent annotation command domain."""

from .annotation_inventory import (AnnotationCapabilities, AnnotationDetail,
                                   AnnotationGeometry, AnnotationSummary,
                                   external_command_annotation_id)

__all__ = ("AnnotationCapabilities", "AnnotationDetail", "AnnotationGeometry",
           "AnnotationSummary", "external_command_annotation_id")
