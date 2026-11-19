"""Bounded wire protocol: JSON metadata followed by optional RGB bytes.

No pickle/unpickling and no engine objects on the application transport.
M0 probe operations remain separate from the M1 document lifecycle operations.
"""

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import math
import struct
from typing import Any

# Text geometry travels as independently bounded chunks. The control ceiling is
# a hard frame guard; ordinary chunks target 256 KiB and only one text request
# is pending per view.
MAX_CONTROL_BYTES = 2 * 1024 * 1024
MIN_RASTER_BYTES = 16  # one pixel plus conservative bounds/padded-row rounding
MAX_SINGLE_RASTER_BYTES = 4 * 1024 * 1024
MAX_FRAME_BYTES = 4 + MAX_CONTROL_BYTES + MAX_SINGLE_RASTER_BYTES
MAX_IN_FLIGHT_RASTER_BYTES = 8 * 1024 * 1024
MAX_IN_FLIGHT_RASTERS = 2
MAX_PENDING_REQUESTS = 4
MAX_GEOMETRY_PAGES = 64
# Product transport ceiling; M0 retains its smaller default stress profile.
VIEWER_SINGLE_RASTER_BYTES = 16 * 1024 * 1024


def frame_limit(raster_limit: int = MAX_SINGLE_RASTER_BYTES) -> int:
    if type(raster_limit) is not int or not MIN_RASTER_BYTES <= raster_limit <= VIEWER_SINGLE_RASTER_BYTES:
        raise ValueError("Invalid transport raster limit")
    return 4 + MAX_CONTROL_BYTES + raster_limit


class Operation(str, Enum):
    OPEN_DOCUMENT = "open_document"
    PAGE_GEOMETRY = "page_geometry"
    INFO = "info"
    RENDER = "render"
    ANNOTATION_APPEARANCE = "annotation_appearance"
    ANNOTATION_COMMAND = "annotation_command"
    ANNOTATION_SUMMARIES = "annotation_summaries"
    ANNOTATION_DETAIL = "annotation_detail"
    TEXT_PAGE_INDEX = "text_page_index"
    TEXT_CHUNK = "text_chunk"
    TEXT_SELECTION = "text_selection"
    TEXT_INSERTION = "text_insertion"
    TEXT_TRIM = "text_trim"
    STRUCTURED_TEXT_PAGE = "structured_text_page"
    SEARCH_SLICE = "search_slice"
    REVIEW_SNAPSHOT = "review_snapshot"
    REVIEW_EXTRACT = "review_extract"
    REVIEW_RELEASE_EXTRACT = "review_release_extract"
    REVIEW_RELEASE = "review_release"
    REVIEW_LOCALIZE = "review_localize"
    REVIEW_CONFIRM = "review_confirm"
    REVIEW_LOOKUP = "review_lookup"
    ANNOTATION_SPIKE = "annotation_spike"
    SAVE_SPIKE = "save_spike"
    SAVE = "save"
    HOLD = "hold"
    CRASH = "crash"
    CLOSE = "close"


REVIEW_OPERATIONS = (Operation.REVIEW_SNAPSHOT, Operation.REVIEW_EXTRACT,
                     Operation.REVIEW_RELEASE_EXTRACT, Operation.REVIEW_RELEASE,
                     Operation.REVIEW_LOCALIZE, Operation.REVIEW_CONFIRM, Operation.REVIEW_LOOKUP,
                     Operation.STRUCTURED_TEXT_PAGE)


@dataclass(frozen=True)
class Request:
    document_id: str
    document_generation: int
    request_id: int
    operation: Operation
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str


@dataclass(frozen=True)
class Response:
    document_id: str
    document_generation: int
    request_id: int
    operation: Operation
    success: bool
    payload: dict[str, Any] = field(default_factory=dict)
    error: ErrorInfo | None = None
    pixels: bytes = b""


def _validate_identity(message: Request | Response) -> None:
    if not isinstance(message.document_id, str) or not 0 < len(message.document_id) <= 128:
        raise ValueError("Invalid document identity")
    if type(message.document_generation) is not int or message.document_generation < 0:
        raise ValueError("Invalid generation")
    if type(message.request_id) is not int or message.request_id < 1:
        raise ValueError("Invalid request identity")
    if not isinstance(message.operation, Operation) or not isinstance(message.payload, dict):
        raise ValueError("Invalid operation/payload")


def encode(message: Request | Response, raster_limit: int = MAX_SINGLE_RASTER_BYTES) -> bytes:
    frame_limit(raster_limit)
    _validate_identity(message)
    meta = asdict(message)
    pixels = meta.pop("pixels", b"")
    meta["message_type"] = "response" if isinstance(message, Response) else "request"
    # JSON rejects non-DTO objects; NaN/Inf never enter geometry through the wire.
    header = json.dumps(meta, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    if len(header) > MAX_CONTROL_BYTES or len(pixels) > raster_limit:
        raise ValueError("IPC frame exceeds its explicit limit")
    return struct.pack("!I", len(header)) + header + pixels


def decode(frame: bytes, raster_limit: int = MAX_SINGLE_RASTER_BYTES) -> Request | Response:
    if not 4 <= len(frame) <= frame_limit(raster_limit):
        raise ValueError("Invalid IPC frame length")
    size = struct.unpack("!I", frame[:4])[0]
    if size > MAX_CONTROL_BYTES or size > len(frame) - 4:
        raise ValueError("Invalid IPC header length")
    data = json.loads(frame[4:4 + size])
    kind = data.pop("message_type")
    data["operation"] = Operation(data["operation"])
    pixels = frame[4 + size:]
    if kind == "request":
        if pixels:
            raise ValueError("Requests cannot contain raster data")
        message = Request(**data)
    elif kind == "response":
        error = data.get("error")
        data["error"] = ErrorInfo(**error) if error is not None else None
        if type(data.get("success")) is not bool or data["success"] == (error is not None):
            raise ValueError("Inconsistent response outcome")
        message = Response(**data, pixels=pixels)
        if pixels:
            p = message.payload
            w, h, stride = p["width"], p["height"], p["stride"]
            if any(type(v) is not int or v <= 0 for v in (w, h, stride)):
                raise ValueError("Invalid raster dimensions")
            formats = {"RGB888": 3, "RGBA8888": 4}
            channels = formats.get(p["format"])
            if channels is None or stride != w * channels or len(pixels) != stride * h:
                raise ValueError("Inconsistent raster payload")
            if len(pixels) > raster_limit:
                raise ValueError("Oversized raster")
    else:
        raise ValueError("Unknown message type")
    _validate_identity(message)
    return message


def positive_number(payload: dict[str, Any], key: str, default: float) -> float:
    value = payload.get(key, default)
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"Invalid {key}")
    return float(value)
