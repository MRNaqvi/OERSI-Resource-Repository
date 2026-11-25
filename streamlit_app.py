"""Streamlit entry point for the local OERSI repository workbench."""

from __future__ import annotations

import hashlib
import html
import json
import math
from collections import defaultdict
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

from src.competency_evaluation import EVALUATION
from src.rdf_export import dataset_summary
from src.semantic_validation import ROOT
from src.workbench import (
    REPORT_LIMIT, Workbench, as_csv, general_validation, graph_choices, inspect_dataset, mapping_preview,
    neighborhood, parse_upload, query_table, repository_validation, run_query,
)

PAGES = (
    "Dashboard", "Resources", "Upload RDF", "Semantic Validation", "SPARQL Workbench",
    "Knowledge Graph", "Migrations", "Pending Reviews", "Settings",
)
CATEGORIES = {"courses": "Courses", "learning_materials": "Learning Materials", "unclassified": "Unclassified"}
UPLOAD = "Uploaded RDF"
REPOSITORY = "SQLite repository"
STYLE = """
<style>
[data-testid="stSidebar"] {background:#14253b;color:#e7eef5}
[data-testid="stSidebar"] h1,[data-testid="stSidebar"] p,
[data-testid="stSidebar"] label,[data-testid="stSidebar"] small {color:#e7eef5}
[data-testid="stSidebar"] [role="radiogroup"] {gap:.64rem}
[data-testid="stSidebar"] [role="radiogroup"] label {padding:.19rem 0}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {color:#abbccf}
[data-testid="stMainBlockContainer"] {padding-top:3rem;max-width:1280px}
[data-testid="stMetric"] {padding:1.1rem;background:#fff;border:1px solid #dce5ee;border-radius:.85rem}
[data-testid="stMetricValue"] {font-size:1.9rem;color:#005f59}
[data-testid="stMetricLabel"] p {font-size:.88rem;color:#3c526d}
[data-testid="stToolbar"] {right:1.5rem}
[data-testid="stAppDeployButton"] {display:none}
h1,h2,h3 {letter-spacing:-.035em}
</style>
"""


def metrics(pairs):
    for column, (label, value) in zip(st.columns(len(pairs)), pairs, strict=True):
        column.metric(label, value)


def table(rows):
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    else:
        st.caption("No rows to display.")


def download_reports(report, turtle):
    columns = st.columns(2)
    columns[0].download_button("Download JSON", json.dumps(report, ensure_ascii=False, indent=2),
                               "validation.json", mime="application/json")
    if turtle is not None:
        columns[1].download_button("Download SHACL RDF", turtle, "shacl-report.ttl", mime="text/turtle")


def graph_picker(dataset, key):
    choices = graph_choices(dataset)
    selected = st.selectbox("Graph", choices, index=list(choices).index("Default graph"), key=key)
    graph_id = choices[selected]
    return None if selected == "Default graph" else graph_id


def dataset_picker(settings, page):
    choices = (UPLOAD, REPOSITORY) if st.session_state.get("upload") else (REPOSITORY, UPLOAD)
    source = st.radio("Data source", choices, horizontal=True, key=f"{page}-source")
    if source == UPLOAD:
        uploaded = st.session_state.get("upload")
        if uploaded is None:
            st.info("Add a dataset on the Upload RDF page.")
            return None, source, None
        st.caption(uploaded["name"])
        return uploaded["dataset"], source, None
    identifier = st.text_input("Resource ID or IRI (optional)", key=f"{page}-resource").strip() or None
    cached = st.session_state.get(f"{page}-dataset")
    if st.button("Load repository RDF", key=f"{page}-load", type="secondary"):
        with st.spinner("Exporting the read-only repository snapshot…"):
            rdf, snapshot = settings.dataset(identifier, with_snapshot=True)
        cached = identifier, rdf, snapshot
        st.session_state[f"{page}-dataset"] = cached
        st.session_state["rdf_counts"] = dataset_summary(rdf)
    if cached is None or cached[0] != identifier:
        st.info("Load an RDF snapshot using the approved context configured in Settings.")
        return None, source, None
    return cached[1], source, cached[2]


def invalidate_repo_views():
    for page in ("validation", "sparql", "graph"):
        st.session_state.pop(f"{page}-dataset", None)
    st.session_state.pop("rdf_counts", None)


def dashboard(settings):
    st.title("Repository dashboard")
    st.caption("SQLite inventory, RDF statistics and recent validation activity")
    try:
        repository = settings.repository()
        with repository._connection() as connection:
            schema = repository._ensure_initialized(connection)
        stats = repository.stats()
    except (OSError, RuntimeError, ValueError) as exc:
        st.warning(str(exc))
    else:
        metrics([("Resources", stats["resources"]), ("Metadata versions", stats["resource_versions"]),
                 ("Pending reviews", stats["pending_updates"]), ("Schema version", schema)])
        st.subheader("Current classification")
        metrics([("Courses", stats["courses"]), ("Learning Materials", stats["learning_materials"]),
                 ("Unclassified", stats["unclassified_resources"])])
    left, right = st.columns(2)
    with left:
        st.subheader("RDF dataset")
        if st.button("Calculate RDF statistics"):
            st.session_state["rdf_counts"] = dataset_summary(settings.dataset())
        if counts := st.session_state.get("rdf_counts"):
            table([{"Statistic": name.replace("_", " ").capitalize(), "Count": value} for name, value in counts.items()])
        else:
            st.caption("Load repository RDF with an approved context to calculate counts.")
    with right:
        st.subheader("Recent validation")
        table(st.session_state.get("validation_history", []))


def resources(settings):
    st.title("Resource explorer")
    st.caption("Search the catalogue and inspect immutable metadata across its history.")
    repository = settings.repository()
    search, filter_column = st.columns([2, 1])
    term = search.text_input("Search by title, identifier or IRI").casefold().strip()
    category = filter_column.selectbox("Classification", ["All", *CATEGORIES], format_func=lambda key: CATEGORIES.get(key, key))
    rows = [
        row for row in settings.resources()
        if (category == "All" or category == row["classification"])
        and (not term or any(term in str(row.get(key, "")).casefold()
                             for key in ("resource_id", "resource_iri", "title", "source_iri")))
    ]
    st.caption(f"{len(rows):,} matching resources")
    table([{"Title": row.get("title"), "Classification": CATEGORIES[row["classification"]], "ID": row["resource_id"]}
           for row in rows[:250]])
    if not rows:
        return
    options = {row["resource_id"]: row.get("title") or row["resource_id"] for row in rows}
    selected = st.selectbox("Inspect resource", options, format_func=lambda item: f"{options[item]} · {item}")
    resource = repository.get_resource(selected)
    st.subheader(resource.get("title") or selected)
    st.code(resource["resource_iri"], language=None, wrap_lines=True)
    source, versions, compare, history = st.tabs(["Resource", "Versions", "Compare", "At a time"])
    numbered = [row["version_number"] for row in repository.list_versions(selected)]
    with source:
        st.json({key: resource.get(key) for key in (
            "resource_id", "resource_iri", "source_iri", "container_id", "current_version_iri", "last_observed_at",
        )}, expanded=True)
    with versions:
        number = st.selectbox("Version", numbered, index=len(numbered) - 1, format_func=lambda n: f"Version {n}")
        show_version(repository.get_version(selected, number))
    with compare:
        first, second = st.columns(2)
        before = first.selectbox("From version", numbered, key="compare-before")
        after = second.selectbox("To version", numbered, index=len(numbered) - 1, key="compare-after")
        st.json(repository.compare_versions(selected, before, after), expanded=2)
    with history:
        cutoff = st.text_input("UTC or offset timestamp", placeholder="2026-01-15T12:00:00Z")
        basis = st.radio("Time basis", ["observation", "repository"], horizontal=True, format_func=str.title)
        if st.button("Retrieve version") and cutoff:
            found = repository.get_version_at_time(selected, cutoff, basis=basis)
            if found:
                st.success(f"Version {found['version_number']} at the selected cutoff")
                show_version(found)
            else:
                st.info("No version at that timestamp.")


def show_version(version):
    details = {key: version.get(key) for key in (
        "version_iri", "graph_iri", "previous_version_iri", "created_at", "observed_at",
        "source_modified_at", "metadata_hash",
    )}
    st.json(details, expanded=True)
    with st.expander("Original JSON-LD"):
        st.json(version["original_jsonld"], expanded=2)
    with st.expander("Normalized metadata"):
        st.json(version["normalized_metadata"], expanded=2)


def upload_rdf():
    st.title("RDF upload laboratory")
    st.caption("Inspect a temporary TriG or N-Quads dataset in this browser session.")
    generation = st.session_state.get("upload-generation", 0)
    file = st.file_uploader("Drop a TriG or N-Quads file", type=["trig", "nq", "nquads"], max_upload_size=4,
                            key=f"rdf-file-{generation}")
    previous = st.session_state.get("upload")
    if previous and st.button("Clear uploaded dataset"):
        for key in ("upload", "validation-result", "sparql-result"):
            st.session_state.pop(key, None)
        st.session_state["upload-generation"] = generation + 1
        st.rerun()
    if file is not None:
        data = file.getvalue()
        fingerprint = hashlib.sha256(data).hexdigest()
        if not previous or previous["sha256"] != fingerprint or previous["name"] != file.name:
            st.session_state.pop("upload", None)
            with st.spinner("Parsing isolated RDF…"):
                format, dataset = parse_upload(file.name, data)
            for key in ("validation-result", "sparql-result"):
                st.session_state.pop(key, None)
            previous = {"name": Path(file.name).name, "dataset": dataset, "format": format,
                        "size": file.size, "sha256": fingerprint}
            st.session_state["upload"] = previous
    if previous:
        dataset = previous["dataset"]
        st.success("Syntax parsing passed")
        st.caption(f"{previous['name']} · {previous['format']} · {previous['size']:,} bytes")
        counts = dataset_summary(dataset)
        metrics([("Quads", counts["total_quads"]), ("Named graphs", counts["named_graphs"]),
                 ("Default graph triples", counts["default_graph_triples"])])
        graph = graph_picker(dataset, f"upload-graph-{previous['sha256']}")
        report = inspect_dataset(dataset, graph)
        named, sample, terms, available = st.tabs(["Graphs", "Statements", "Namespaces & terms", "Resources"])
        with named:
            table(report["graphs"])
        with sample:
            table(report["statements"])
        with terms:
            st.markdown("**Namespaces**")
            table(report["namespaces"])
            left, right = st.columns(2)
            with left:
                st.markdown("**Classes**")
                table(report["classes"])
            with right:
                st.markdown("**Properties**")
                table(report["properties"])
        with available:
            table([{"Subject IRI": value} for value in report["resources"]])


def semantic_validation(settings):
    st.title("Semantic validation")
    st.caption("Inspect general RDF or validate an OERSI repository snapshot against its vocabulary.")
    dataset, source, snapshot = dataset_picker(settings, "validation")
    mode = st.radio("Validation mode", ["General RDF", "OERSI repository"], horizontal=True,
                    index=0 if source == UPLOAD else 1, key="validation-mode")
    if dataset is None:
        return
    compare = False
    identifier = None
    shapes = None
    if mode == "General RDF":
        identifier = graph_picker(dataset, "validation-shape-graph")
        shapes = st.file_uploader("Optional SHACL Core shapes (Turtle)", type=["ttl"], max_upload_size=1)
    else:
        compare = st.checkbox("Check completeness and fidelity against configured SQLite", value=source == REPOSITORY)
        if compare and source == UPLOAD:
            identifier = st.text_input("SQLite resource ID or IRI (optional)").strip() or None
    shape_data = shapes.getvalue() if shapes else None
    result_key = (mode, source, id(dataset), compare, identifier,
                  hashlib.sha256(shape_data).hexdigest() if shape_data else None)
    if st.button("Run validation", type="primary"):
        st.session_state.pop("validation-result", None)
        with st.spinner("Checking constraints…"):
            if mode == "General RDF":
                report, turtle = general_validation(dataset, identifier, shapes=shape_data)
            else:
                report, turtle = repository_validation(
                    dataset, settings, against_sqlite=compare, identifier=identifier, supplied_snapshot=snapshot,
                )
        st.session_state["validation-result"] = (result_key, report, turtle)
        status = ("Passed" if report["conforms"] else "Failed") if "conforms" in report else report["shacl"]["status"]
        if mode == "OERSI repository" and status == "Passed" and report["fidelity"]["status"] == "Not evaluated":
            status = "Structural checks passed"
        rows = st.session_state.get("validation_history", [])
        st.session_state["validation_history"] = [
            {"Source": source, "Mode": mode, "SHACL": report["shacl"]["status"], "Result": status}, *rows,
        ][:REPORT_LIMIT]
    result = st.session_state.get("validation-result")
    if result and result[0] == result_key:
        _, report, turtle = result
        if diagnostic := report.get("diagnostic"):
            st.error(diagnostic)
        for key, label in (("shacl", "SHACL"), ("integrity", "Repository integrity"),
                           ("sqlite_completeness", "SQLite completeness"), ("fidelity", "Source fidelity")):
            if key in report:
                st.write(f"**{label}** · {report[key]['status']}")
        st.json(report, expanded=2)
        download_reports(report, turtle)


def _load_example(item):
    definition = next(item_ for item_ in json.loads((EVALUATION / "expected-answers.json").read_text())
                      if item_["id"] == item)
    st.session_state["query-editor"] = (ROOT / "queries" / definition["query"]).read_text()
    for kind in ("iri", "integer", "time"):
        st.session_state[f"query-bind-{kind}"] = "\n".join(
            f"{name}={details['value']}" for name, details in definition.get("bind", {}).items() if details["type"] == kind
        )


def sparql(settings):
    st.title("SPARQL workbench")
    st.caption("Run local SELECT and ASK queries. Use GRAPH patterns to query named graphs.")
    dataset, source, _ = dataset_picker(settings, "sparql")
    if dataset is None:
        return
    graph_id = graph_picker(dataset, "sparql-graph-inspect")
    with st.expander("Inspect selected graph"):
        table(inspect_dataset(dataset, graph_id)["statements"])
    questions = {row["id"]: row["query"] for row in json.loads((EVALUATION / "expected-answers.json").read_text())}
    example = st.selectbox("Example competency question", questions, format_func=lambda key: f"{key} · {questions[key]}")
    st.button("Load example", on_click=_load_example, args=(example,))
    query = st.text_area("SPARQL", value="SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 25", height=210, key="query-editor")
    with st.expander("Variable bindings"):
        columns = st.columns(3)
        bindings = {kind: column.text_area(f"{kind.title()} bindings", key=f"query-bind-{kind}",
                                          placeholder="variable=value")
                    for column, kind in zip(columns, ("iri", "integer", "time"), strict=True)}
    result_key = (source, id(dataset), query, tuple(bindings.values()))
    if st.button("Run query", type="primary"):
        st.session_state.pop("sparql-result", None)
        with st.spinner("Running the local query…"):
            result = run_query(dataset, query, settings.namespaces()[1], **bindings)
        st.session_state["sparql-result"] = (result_key, result)
    saved = st.session_state.get("sparql-result")
    if saved and saved[0] == result_key:
        result = saved[1]
        cols, rows = query_table(result)
        st.subheader("Query result")
        if result["kind"] == "ASK":
            st.metric("ASK", rows[0]["answer"])
        else:
            st.caption(f"{len(rows):,} rows shown")
            if result["truncated"]:
                st.warning("Result limited to the first 1,000 rows.")
            table(rows)
        left, right = st.columns(2)
        left.download_button("Download JSON", json.dumps(result, ensure_ascii=False, indent=2), "query.json")
        right.download_button("Download CSV", as_csv(cols, rows), "query.csv", mime="text/csv")


def _short(term):
    parts = str(term).rstrip("/").split("/")
    label = parts[-1].rsplit("#", 1)[-1]
    if len(parts) >= 2 and parts[-2] == "version":
        label = f"Graph V{label}" if "/graph/resource/" in str(term) else f"Version {label}"
    elif len(parts) >= 2 and parts[-2] in {"classification", "container"}:
        label = f"{parts[-2].capitalize()}: {CATEGORIES.get(label, label)}"
    return (label[:28] + "…") if len(label) > 29 else label


def _graph_figure(levels, edges):
    circles = defaultdict(list)
    for node, level in levels.items():
        circles[level].append(node)
    xy = {}
    for level, ring in circles.items():
        for index, node in enumerate(sorted(ring, key=str)):
            angle = math.tau * index / len(ring)
            xy[node] = level * math.cos(angle), level * math.sin(angle)
    line_x, line_y = [], []
    for start, _, end in edges:
        line_x.extend((xy[start][0], xy[end][0], None))
        line_y.extend((xy[start][1], xy[end][1], None))
    nodes = list(levels)
    fig = go.Figure([
        go.Scatter(x=line_x, y=line_y, mode="lines", line={"width": 1, "color": "#a8bccd"}, hoverinfo="skip"),
        go.Scatter(x=[xy[node][0] for node in nodes], y=[xy[node][1] for node in nodes], mode="markers+text",
                   customdata=list(range(len(nodes))), text=[html.escape(_short(node)) for node in nodes],
                   textposition="top center", hovertext=[html.escape(str(node)) for node in nodes],
                   hovertemplate="%{hovertext}<extra></extra>",
                   marker={"size": [18 if not levels[node] else 11 for node in nodes],
                           "color": ["#007a73" if not levels[node] else "#3c6ba6" for node in nodes]}),
    ])
    fig.update_layout(height=480, showlegend=False, clickmode="event+select",
                      margin={"l": 10, "r": 10, "t": 24, "b": 16},
                      xaxis={"visible": False}, yaxis={"visible": False, "scaleanchor": "x"},
                      plot_bgcolor="#ffffff", paper_bgcolor="#ffffff")
    return fig


def knowledge_graph(settings):
    st.title("Knowledge graph")
    st.caption("Explore a bounded neighborhood and inspect the statements on any selected node.")
    dataset, source, snapshot = dataset_picker(settings, "graph")
    if dataset is None:
        return
    scope = graph_picker(dataset, "knowledge-graph-scope")
    graph = dataset.default_graph if scope is None else dataset.graph(scope)
    candidates = set(graph.subjects())
    if not candidates:
        st.info("This graph contains no subjects.")
        return
    preferred = {row["resource_iri"] for row in snapshot["resources"]} if snapshot else set()
    nodes = sorted(candidates, key=lambda node: (str(node) not in preferred, str(node)))[:250]
    center = st.selectbox("Neighborhood center", nodes, format_func=str)
    levels, edges = neighborhood(graph, center)
    st.caption(f"{len(levels)} displayed nodes · {len(edges)} relationships · depth 2")
    plot = st.plotly_chart(_graph_figure(levels, edges), on_select="rerun", selection_mode="points",
                           key=f"graph-plot-{hash((id(dataset), scope, center))}")
    points = plot.selection.points
    targets = list(levels)
    clicked = points[0].get("customdata") if points else None
    index = clicked if isinstance(clicked, int) and 0 <= clicked < len(targets) else 0
    node = st.selectbox("Inspect node", targets, index=index, format_func=str)
    triples = {*graph.triples((node, None, None)), *graph.triples((None, None, node))}
    table([{"Subject": subject.n3(), "Relationship": predicate.n3(), "Object": obj.n3()}
           for subject, predicate, obj in sorted(triples, key=lambda row: tuple(map(str, row)))[:100]])


def migrations(settings):
    st.title("Migration manager")
    st.caption("Preview a supported SQLite migration or inspect potential RDF field mappings.")
    sqlite_tab, rdf_tab = st.tabs(["SQLite migration", "RDF mapping preview"])
    with rdf_tab:
        uploaded = st.session_state.get("upload")
        if uploaded is None:
            st.info("Add a dataset on the Upload RDF page.")
        else:
            st.caption("Read-only preview. Resource identity and classification require explicit mapping and review.")
            table(mapping_preview(uploaded["dataset"]))
    with sqlite_tab:
        try:
            preview = settings.migration_preview()
        except (OSError, ValueError, RuntimeError) as exc:
            st.warning(str(exc))
            return
        metrics([("Detected schema", preview["schema_version"] or "Unknown"), ("Supported target", preview["target_version"])])
        st.write(preview["status"])
        if preview["supported"]:
            for change in preview["changes"]:
                st.markdown(f"- {change}")
            st.caption(f"Backup directory · {settings.backup_dir or 'Configure in Settings before migrating'}")
            name = st.text_input("New backup filename", placeholder="repository-before-migration.sqlite3")
            if name:
                try:
                    st.code(str(settings.backup_path(name)), language=None)
                except (OSError, ValueError) as exc:
                    st.warning(str(exc))
            with st.form("migration-form"):
                confirmed = st.checkbox("I confirm the backup path and want to migrate the configured database.")
                if st.form_submit_button("Apply migration", type="primary"):
                    result = settings.migrate(name, confirmed=confirmed)
                    invalidate_repo_views()
                    st.success(f"Migration verified. Backup created at {result['backup']}")
                    st.json(result)


def pending_reviews(settings):
    st.title("Pending reviews")
    st.caption("Inspect proposed changes and record decisions through the repository transaction.")
    repo = settings.repository()
    if message := st.session_state.pop("review-message", None):
        st.success(message)
    include = st.checkbox("Show decided updates")
    updates = repo.list_pending_updates(include_decided=include)
    table([{key: row.get(key) for key in ("update_id", "resource_id", "status", "observed_at", "decided_at")}
           for row in updates])
    if not updates:
        return
    choices = {row["update_id"]: f"#{row['update_id']} · {row['resource_id']} · {row['status']}" for row in updates}
    selected = st.selectbox("Inspect update", choices, format_func=choices.__getitem__)
    review = repo.review_update(selected)
    candidate = review["candidate"]
    change, current, proposed = st.tabs(["Changes", "Current snapshot", "Proposed metadata"])
    change.json(review["changes"], expanded=2)
    with current:
        show_version(review["current_version"])
    proposed.json(candidate, expanded=2)
    if candidate["status"] == "pending":
        with st.form(f"decision-{selected}"):
            decision = st.radio("Decision", ["Accept", "Reject"], horizontal=True)
            confirmed = st.checkbox("I confirm this decision for the displayed candidate.")
            if st.form_submit_button("Record decision", type="primary"):
                result = settings.decide(selected, decision, confirmed=confirmed)
                invalidate_repo_views()
                st.session_state["review-message"] = f"{decision} recorded. Version history is available in Resources."
                if accepted := result.get("accepted_version_iri"):
                    st.session_state["review-message"] += f" Accepted version: {accepted}"
                st.rerun()


def settings_page(settings):
    st.title("Settings")
    st.caption("Set paths and approved context fingerprints in the environment before launching the workbench.")
    base, vocabulary = settings.namespaces()
    table([
        {"Setting": label, "Configured value": str(value) if value else "Not configured"}
        for label, value in (
            ("SQLite database", settings.database), ("Repository namespace", base), ("Vocabulary namespace", vocabulary),
            ("Context snapshot", settings.context_snapshot), ("Approved context fingerprint", settings.context_sha256),
            ("Migration backup directory", settings.backup_dir),
        )
    ])
    if st.button("Verify approved context"):
        pin = settings.contexts()
        st.success(f"Verified {len(pin.documents)} local context document(s)")
    st.markdown("**RDF controls**")
    st.write("TriG and N-Quads datasets up to 4 MiB. Turtle SHACL Core shapes up to 256 KiB. "
             "Local SELECT/ASK, up to 1,000 result rows.")


def main():
    load_dotenv()
    st.set_page_config(page_title="OERSI Repository Workbench", page_icon="◈", layout="wide")
    st.html(STYLE)
    st.sidebar.markdown("## ◈ OERSI")
    st.sidebar.caption("REPOSITORY WORKBENCH")
    page = st.sidebar.radio("Workspace", PAGES, label_visibility="collapsed")
    st.sidebar.divider()
    st.sidebar.caption("Local research workspace")
    settings = Workbench.configured()
    pages = {
        "Dashboard": lambda: dashboard(settings),
        "Resources": lambda: resources(settings),
        "Upload RDF": upload_rdf,
        "Semantic Validation": lambda: semantic_validation(settings),
        "SPARQL Workbench": lambda: sparql(settings),
        "Knowledge Graph": lambda: knowledge_graph(settings),
        "Migrations": lambda: migrations(settings),
        "Pending Reviews": lambda: pending_reviews(settings),
        "Settings": lambda: settings_page(settings),
    }
    try:
        pages[page]()
    except Exception as exc:
        st.error(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
