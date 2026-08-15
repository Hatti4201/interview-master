#!/usr/bin/env python3
"""Remove TXT headings, merge safe self-introduction aliases, and clean canonical numbering."""

import json
import re

from import_master import DEFAULT_DATABASE_URL, dedupe_key, run_psql
from import_txt_sources import BOILERPLATE, HEADING


def query_json(sql: str):
    return json.loads(run_psql(DEFAULT_DATABASE_URL, "-At", "-c", sql) or "[]")


rows = query_json(
    """
SELECT COALESCE(json_agg(json_build_object(
    'question_id', q.id,
    'question_key', q.normalized_key_en,
    'variant_id', qv.id,
    'original_text', qv.original_text
) ORDER BY q.id, qv.id), '[]')::text
FROM questions q
JOIN question_variants qv ON qv.question_id = q.id;
"""
)

question_keys = {row["question_key"]: row["question_id"] for row in rows}
variants_by_question = {}
heading_variant_ids = []
for row in rows:
    variants_by_question.setdefault(row["question_id"], []).append(row)
    cleaned = re.sub(
        r"^(?:q(?:uestion)?\s*)?\d+\s*[.):、,-]\s*",
        "",
        row["original_text"],
        flags=re.I,
    ).strip()
    if HEADING.fullmatch(cleaned) or BOILERPLATE.search(cleaned):
        heading_variant_ids.append(row["variant_id"])

merges = []
for source_id, variants in variants_by_question.items():
    new_keys = {dedupe_key(row["original_text"]) for row in variants}
    if len(new_keys) != 1:
        continue
    new_key = new_keys.pop()
    target_id = question_keys.get(new_key)
    if target_id is not None and target_id != source_id:
        merges.append((source_id, target_id))

heading_ids_sql = ",".join(map(str, heading_variant_ids)) or "NULL"
merge_sql = []
for source_id, target_id in merges:
    merge_sql.extend(
        [
            f"UPDATE question_variants SET question_id = {target_id} WHERE question_id = {source_id};",
            f"DELETE FROM question_tags WHERE question_id = {source_id};",
            f"DELETE FROM questions WHERE id = {source_id};",
        ]
    )

sql = f"""
BEGIN;
DELETE FROM question_occurrences
WHERE variant_id IN ({heading_ids_sql});

DELETE FROM question_variants qv
WHERE qv.id IN ({heading_ids_sql})
  AND NOT EXISTS (SELECT 1 FROM question_occurrences qo WHERE qo.variant_id = qv.id);

DELETE FROM question_tags qt
WHERE NOT EXISTS (SELECT 1 FROM question_variants qv WHERE qv.question_id = qt.question_id);

DELETE FROM questions q
WHERE NOT EXISTS (SELECT 1 FROM question_variants qv WHERE qv.question_id = q.id);

{"".join(merge_sql)}

UPDATE questions
SET canonical_text_en = REGEXP_REPLACE(
    canonical_text_en,
    '^(?:q(?:uestion)?[[:space:]]*)?[0-9]+[[:space:]]*[.):、,-][[:space:]]*',
    '',
    'i'
)
WHERE canonical_text_en ~* '^(?:q(?:uestion)?[[:space:]]*)?[0-9]+[[:space:]]*[.):、,-]';
COMMIT;
"""
run_psql(DEFAULT_DATABASE_URL, "-q", "-c", sql)
print(json.dumps({"heading_variants_removed": len(heading_variant_ids), "questions_merged": len(merges)}))
