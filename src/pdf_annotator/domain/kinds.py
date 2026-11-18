from enum import Enum


class AnnotationKind(str, Enum):
    INK = "ink"
    HIGHLIGHT = "highlight"
    TEXT_BOX = "text_box"
    CALLOUT = "callout"
    DELETE_TEXT = "delete_text"
    INSERT_TEXT = "insert_text"
    REPLACE_TEXT = "replace_text"
    STICKY_NOTE = "sticky_note"


class CommandKind(str, Enum):
    CREATE_INK = "create_ink"
    CREATE_HIGHLIGHT = "create_highlight"
    CREATE_FREE_TEXT = "create_free_text"
    CREATE_CALLOUT = "create_callout"
    CREATE_DELETE_TEXT = "create_delete_text"
    CREATE_INSERT_TEXT = "create_insert_text"
    CREATE_REPLACE_TEXT = "create_replace_text"
    CREATE_STICKY_NOTE = "create_sticky_note"
    MOVE_INK = "move_ink"
    UPDATE_FREE_TEXT = "update_free_text"
    MOVE_FREE_TEXT = "move_free_text"
    RESIZE_FREE_TEXT = "resize_free_text"
    TRANSFER_FREE_TEXT = "transfer_free_text"
    UPDATE_CALLOUT = "update_callout"
    MOVE_CALLOUT = "move_callout"
    RESIZE_CALLOUT = "resize_callout"
    MOVE_CALLOUT_ANCHOR = "move_callout_anchor"
    TRANSFER_CALLOUT = "transfer_callout"
    DELETE_ANNOTATION = "delete_annotation"
    DELETE_EXTERNAL_ANNOTATION = "delete_external_annotation"
    EDIT_EXTERNAL_ANNOTATION = "edit_external_annotation"
    UNDO = "undo"
    REDO = "redo"
