#!/usr/bin/env python3
"""Verify the local master database and print its current Top 5 questions."""

import json
import os
import subprocess
from pathlib import Path


DATABASE_URL = os.environ.get(
    "INTERVIEW_MASTER_DATABASE_URL",
    "postgresql://interview_master:local_interview_only@127.0.0.1:54329/interview_master",
)
ROOT = Path(__file__).resolve().parents[1]


def query(sql: str) -> str:
    result = subprocess.run(
        ["psql", DATABASE_URL, "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


summary = json.loads(
    query(
        """
SELECT json_build_object(
    'sources', (SELECT COUNT(*) FROM sources),
    'raw_records', (SELECT COUNT(*) FROM raw_records),
    'pending_raw_records', (SELECT COUNT(*) FROM raw_records WHERE parse_status = 'pending'),
    'needs_review_raw_records', (SELECT COUNT(*) FROM raw_records WHERE parse_status = 'needs_review'),
    'interviews', (SELECT COUNT(*) FROM interviews),
    'questions', (SELECT COUNT(*) FROM questions),
    'variants', (SELECT COUNT(*) FROM question_variants),
    'occurrences', (SELECT COUNT(*) FROM question_occurrences),
    'questions_needing_review', (SELECT COUNT(*) FROM questions WHERE needs_review),
    'orphan_occurrences', (
        SELECT COUNT(*)
        FROM question_occurrences qo
        LEFT JOIN interviews i ON i.id = qo.interview_id
        LEFT JOIN question_variants qv ON qv.id = qo.variant_id
        WHERE i.id IS NULL OR qv.id IS NULL
    )
)::text;
"""
    )
)

manifest_counts = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["counts"]
for key in (
    "sources",
    "raw_records",
    "pending_raw_records",
    "needs_review_raw_records",
    "interviews",
    "questions",
    "variants",
    "occurrences",
    "questions_needing_review",
):
    assert summary[key] == manifest_counts[key], (key, summary[key], manifest_counts[key])
assert summary["orphan_occurrences"] == 0, summary

top_five = query(
    """
SELECT interview_frequency || E'\t' || canonical_text_en
FROM question_frequency
ORDER BY interview_frequency DESC, total_mentions DESC, canonical_text_en
LIMIT 5;
"""
)

print(json.dumps(summary, ensure_ascii=False, indent=2))
print("\nTop 5 questions by distinct interviews:")
print(top_five)
