from src.classification import classify_resource

COURSE = "https://w3id.org/kim/hcrt/course"
VIDEO = "https://w3id.org/kim/hcrt/video"


def test_exact_course_type_is_classified_as_course():
    assert classify_resource({"learningResourceType": [{"id": COURSE}]})[0] == "courses"


def test_concrete_material_type_is_classified_as_learning_material():
    assert classify_resource({"learningResourceType": [{"id": VIDEO}]})[0] == "learning_materials"


def test_ambiguous_types_are_unclassified():
    for resource_type in ("other", "portal", "web_page", "unknown"):
        container, _ = classify_resource(
            {"learningResourceType": [{"id": f"https://w3id.org/kim/hcrt/{resource_type}"}]}
        )
        assert container == "unclassified"


def test_missing_or_malformed_type_is_unclassified():
    assert classify_resource({})[0] == "unclassified"
    assert classify_resource({"learningResourceType": "course"})[0] == "unclassified"


def test_course_takes_precedence_for_mixed_types():
    metadata = {"learningResourceType": [{"id": COURSE}, {"id": VIDEO}]}
    assert classify_resource(metadata)[0] == "courses"
