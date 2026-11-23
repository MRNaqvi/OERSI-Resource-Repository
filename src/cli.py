"""Command-line entry point for the Phase 1 OERSI extraction."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from src.extractor import Extractor
from src.oersi_client import OERSIClient

DEFAULT_API_URL = "https://oersi.org/api/search/oer_data/_search"
DEFAULT_USER_AGENT = "OERSI-Resource-Repository/0.1 (metadata-only extraction)"


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Extract OERSI metadata without downloading resource files.")
    parser.add_argument("--limit", type=int, default=int(os.getenv("OERSI_LIMIT", "100")),
                        help="Maximum number of unique records to retain (default: 100).")
    parser.add_argument("--sample-size", type=int, default=int(os.getenv("OERSI_SAMPLE_SIZE", "20")),
                        help="Broad initial sample size (default: 20).")
    parser.add_argument("--target-per-category", type=int,
                        default=int(os.getenv("OERSI_TARGET_PER_CATEGORY", "10")),
                        help="Try to collect this many records for each classified container (default: 10).")
    parser.add_argument("--timeout", type=float, default=float(os.getenv("OERSI_TIMEOUT", "20")),
                        help="Request timeout in seconds (default: 20).")
    parser.add_argument("--output-dir", type=Path, default=Path(os.getenv("OERSI_OUTPUT_DIR", "data")),
                        help="Output data directory (default: data).")
    args = parser.parse_args()

    client = OERSIClient(
        api_url=os.getenv("OERSI_API_URL", DEFAULT_API_URL),
        user_agent=os.getenv("OERSI_USER_AGENT", DEFAULT_USER_AGENT),
        timeout=args.timeout,
    )
    report = Extractor(
        client=client,
        output_dir=args.output_dir,
        limit=args.limit,
        sample_size=args.sample_size,
        target_per_category=args.target_per_category,
    ).run()
    print(json.dumps({key: report[key] for key in (
        "fetched_records",
        "unique_records",
        "successfully_classified_courses",
        "successfully_classified_learning_materials",
        "unclassified_records",
        "extraction_error_count",
        "extraction_errors",
    )}, ensure_ascii=False, indent=2))
    return 0 if report["extraction_error_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
