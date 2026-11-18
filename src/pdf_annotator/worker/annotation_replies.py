"""Read-only /Text reply resolution; no PDF mutations or capability decisions."""

from dataclasses import replace
from typing import Any

from ..domain.annotation_inventory import AnnotationReply, AnnotationSummary


def associate_replies(document: Any, records: list[AnnotationSummary],
                      physical: dict[int, int], group_roots: dict[int, int]) -> list[AnnotationSummary]:
    by_xref = {int(record.logical_id.rsplit(":", 1)[1]): record for record in records}
    edges: dict[int, int] = {}
    relations: dict[int, AnnotationReply] = {}
    resolved: dict[int, tuple[int | None, str]] = {}
    for xref, record in by_xref.items():
        if record.subtype != "Text":
            continue
        irt_kind, irt_value = document.xref_get_key(xref, "IRT")
        rt_kind, rt_value = document.xref_get_key(xref, "RT")
        if irt_kind == rt_kind == "null":
            continue
        parent = int(irt_value.split()[0]) if irt_kind == "xref" else None
        relations[xref] = AnnotationReply(parent, irt_value if irt_kind != "null" else None,
                                         rt_value if rt_kind != "null" else None, None, "unresolved")
        if irt_kind != "xref":
            resolved[xref] = None, "invalid_irt"
        elif rt_kind != "null" and (rt_kind != "name" or rt_value != "/R"):
            resolved[xref] = None, "unsupported_reply_type"
        elif parent not in physical:
            resolved[xref] = None, "missing_parent"
        elif physical[parent] != record.page_index:
            resolved[xref] = None, "cross_page"
        else:
            edges[xref] = parent

    # Iterative and memoized: malformed cycles and long reply chains cannot
    # overflow Python recursion or turn a normal scan into a quadratic walk.
    for start in relations:
        path: list[int] = []
        visited: set[int] = set()
        current = start
        while current not in resolved:
            if current in visited:
                resolved[current] = None, "cycle"
                break
            visited.add(current)
            path.append(current)
            if current in group_roots:
                current = group_roots[current]
            elif current in edges:
                current = edges[current]
            elif current in by_xref:
                kind, _value = document.xref_get_key(current, "IRT")
                resolved[current] = (current, "linked") if kind == "null" else (None, "unresolved_parent")
            else:
                resolved[current] = None, "unreadable_parent"
        result = resolved[current]
        for xref in path:
            resolved[xref] = result

    children: dict[str, list[str]] = {}
    updated = {}
    for xref, relation in relations.items():
        root, status = resolved[xref]
        parent_id = by_xref[root].logical_id if root is not None else None
        record = by_xref[xref]
        updated[xref] = replace(record, reply=replace(relation, parent_logical_id=parent_id, status=status))
        if parent_id is not None:
            children.setdefault(parent_id, []).append(record.logical_id)
    # Preserve original /Annots order, independent of author, dates or text.
    order = {xref: index for index, xref in enumerate(physical)}
    return [replace(updated.get(xref, record), reply_ids=tuple(sorted(
                children.get(record.logical_id, ()), key=lambda value: order[int(value.rsplit(":", 1)[1])])))
            for xref, record in by_xref.items()]
