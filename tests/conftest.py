import json

import pytest

from src.context_snapshot import ContextDocument, load_context_snapshot, write_context_snapshot
from src.jsonld_validation import OFFICIAL_AMB_CONTEXT_URL
from tests.jsonld_fixtures import AMB_CONTEXT_DOCUMENT


@pytest.fixture
def approved_contexts(tmp_path):
    path = tmp_path / "amb-snapshot"
    doc = ContextDocument(
        OFFICIAL_AMB_CONTEXT_URL, OFFICIAL_AMB_CONTEXT_URL, json.dumps(AMB_CONTEXT_DOCUMENT).encode(),
    )
    manifest = write_context_snapshot(path, [doc])
    return load_context_snapshot(path, manifest["manifest_sha256"])
