"""Conservative classification based on OERSI learning-resource-type IDs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

COURSE_TYPE_ID = "https://w3id.org/kim/hcrt/course"

# Resource type IDs observed in the OERSI index and representing a concrete
# learning object/material. Broad or container-like types (for example
# ``other``, ``portal`` and ``web_page``) are deliberately not inferred as
# learning materials: their meaning is not specific enough for this container.
LEARNING_MATERIAL_TYPE_IDS = frozenset(
    {
        "https://w3id.org/kim/hcrt/video",
        "https://w3id.org/kim/hcrt/textbook",
        "https://w3id.org/kim/hcrt/text",
        "https://w3id.org/kim/hcrt/drill_and_practice",
        "https://w3id.org/kim/hcrt/audio",
        "https://w3id.org/kim/hcrt/worksheet",
        "https://w3id.org/kim/hcrt/slide",
        "https://w3id.org/kim/hcrt/image",
        "https://w3id.org/kim/hcrt/assessment",
        "https://w3id.org/kim/hcrt/lesson_plan",
        "https://w3id.org/kim/hcrt/script",
        "https://w3id.org/kim/hcrt/diagram",
        "https://w3id.org/kim/hcrt/application",
        "https://w3id.org/kim/hcrt/educational_game",
        "https://w3id.org/kim/hcrt/simulation",
        "https://w3id.org/kim/hcrt/case_study",
        "https://w3id.org/kim/hcrt/experiment",
        "https://w3id.org/kim/hcrt/data",
        "https://w3id.org/kim/hcrt/questionnaire",
        "https://w3id.org/kim/hcrt/sheet_music",
        "https://w3id.org/kim/hcrt/map",
    }
)


def _type_ids(metadata: Mapping[str, Any]) -> set[str]:
    values = metadata.get("learningResourceType")
    if isinstance(values, Mapping):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return set()

    ids: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            identifier = value.get("id")
            if isinstance(identifier, str) and identifier.strip():
                ids.add(identifier.strip())
    return ids


def classify_resource(metadata: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(container, reason)``; uncertain metadata is unclassified."""
    type_ids = _type_ids(metadata)
    has_course_type = COURSE_TYPE_ID in type_ids
    matched_material_types = sorted(type_ids & LEARNING_MATERIAL_TYPE_IDS)
    if has_course_type and matched_material_types:
        return (
            "unclassified",
            "Ambiguous OERSI resource types: both course and learning material types are present",
        )
    if has_course_type:
        return "courses", f"Exact OERSI type match: {COURSE_TYPE_ID}"

    if matched_material_types:
        return "learning_materials", "Recognized OERSI material type: " + ", ".join(
            matched_material_types
        )

    if not type_ids:
        return "unclassified", "No usable learningResourceType identifier"
    return "unclassified", "No recognized course or concrete-material type"
