#!/usr/bin/env python3
"""Replace reviewed composite questions with atomic questions in one transaction."""

import argparse
import json
from collections import Counter

from import_master import DEFAULT_DATABASE_URL, ROOT, classify, dedupe_key, run_psql


REVIEW_PATH = ROOT / "question_split_review.json"


def sql_text(value: str) -> str:
    return "'" + value.replace("\x00", "").replace("'", "''") + "'"


def sql_nullable(value) -> str:
    return "NULL" if value is None else sql_text(str(value))


def query_json(database_url: str, sql: str):
    return json.loads(run_psql(database_url, "-At", "-c", sql) or "null")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    review = json.loads(REVIEW_PATH.read_text(encoding="utf-8"))
    split = {int(key): value for key, value in review["split"].items()}
    remove = {int(key): value for key, value in review["remove"].items()}
    reuse_existing = {
        text: int(question_id)
        for text, question_id in review.get("reuse_existing", {}).items()
    }
    expected_ids = set(
        query_json(
            args.database_url,
            "SELECT COALESCE(json_agg(id ORDER BY id), '[]')::text FROM questions WHERE needs_review;",
        )
    )
    covered_ids = set(split) | set(remove)
    if covered_ids != expected_ids:
        raise SystemExit(
            json.dumps(
                {
                    "missing": sorted(expected_ids - covered_ids),
                    "extra": sorted(covered_ids - expected_ids),
                }
            )
        )

    for question_id, children in split.items():
        child_keys = [dedupe_key(child) for child in children]
        if not children or len(child_keys) != len(set(child_keys)):
            raise SystemExit(f"Q{question_id} has no children or duplicate child questions")
    child_texts = {child for children in split.values() for child in children}
    if not set(reuse_existing) <= child_texts:
        raise SystemExit("reuse_existing contains text that is not a split child")

    state = query_json(
        args.database_url,
        """
SELECT json_build_object(
    'counts', json_build_object(
        'questions', (SELECT COUNT(*) FROM questions),
        'variants', (SELECT COUNT(*) FROM question_variants),
        'occurrences', (SELECT COUNT(*) FROM question_occurrences)
    ),
    'questions', (SELECT json_agg(json_build_object(
        'id', id, 'key', normalized_key_en
    ) ORDER BY id) FROM questions),
    'tags', (SELECT json_agg(json_build_object('id', id, 'name_zh', name_zh)) FROM tags),
    'rows', (SELECT json_agg(row_to_json(item) ORDER BY item.interview_id, item.sequence_no DESC)
        FROM (
            SELECT q.id AS question_id, q.normalized_key_en AS old_key,
                qv.id AS variant_id, qv.original_text, qv.language,
                BTRIM(qv.text_sha256) AS text_sha256,
                qo.id AS occurrence_id, qo.interview_id,
                qo.source_question_key, qo.sequence_no
            FROM questions q
            JOIN question_variants qv ON qv.question_id = q.id
            JOIN question_occurrences qo ON qo.variant_id = qv.id
            WHERE q.needs_review
            ORDER BY qo.interview_id, qo.sequence_no DESC
        ) item)
)::text;
""",
    )
    rows = state["rows"] or []
    row_question_ids = {row["question_id"] for row in rows}
    if row_question_ids != expected_ids:
        raise SystemExit(
            f"Reviewed questions without occurrences: {sorted(expected_ids - row_question_ids)}"
        )

    questions_by_id = {item["id"]: item["key"] for item in state["questions"]}
    existing_keys = {item["key"] for item in state["questions"]}
    for child, target_id in reuse_existing.items():
        if target_id not in questions_by_id or target_id in expected_ids:
            raise SystemExit(f"Invalid reuse target for {child}: Q{target_id}")
        target_key = dedupe_key(child)
        old_key = questions_by_id[target_id]
        if target_key in existing_keys and target_key != old_key:
            raise SystemExit(f"Reuse target key already exists: {target_key}")
        existing_keys.remove(old_key)
        existing_keys.add(target_key)
        questions_by_id[target_id] = target_key
    old_keys = {
        item["key"] for item in state["questions"] if item["id"] in expected_ids
    }
    all_child_keys = {
        dedupe_key(child) for children in split.values() for child in children
    }
    if all_child_keys & old_keys:
        raise SystemExit(f"A child reuses a composite key: {sorted(all_child_keys & old_keys)}")

    new_keys = all_child_keys - existing_keys
    child_occurrences = sum(len(split.get(row["question_id"], [])) for row in rows)
    summary = {
        "composite_questions": len(split),
        "non_question_records_removed": len(remove),
        "atomic_question_entries": sum(map(len, split.values())),
        "existing_questions_cleaned_and_reused": len(reuse_existing),
        "existing_questions_reused": len(all_child_keys & existing_keys),
        "new_unique_questions": len(new_keys),
        "child_occurrences": child_occurrences,
        "resulting_questions": state["counts"]["questions"] - len(expected_ids) + len(new_keys),
        "resulting_occurrences": state["counts"]["occurrences"] - len(rows) + child_occurrences,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.dry_run:
        return

    tag_ids = {item["name_zh"]: item["id"] for item in state["tags"]}
    planned_keys = set(existing_keys)
    statements = ["BEGIN;"]
    for child, target_id in reuse_existing.items():
        child_sql = sql_text(child)
        normalized_sql = sql_text(dedupe_key(child))
        statements.extend(
            [
                f"UPDATE questions SET canonical_text_en = {child_sql}, normalized_key_en = {normalized_sql}, needs_review = FALSE WHERE id = {target_id};",
                f"UPDATE question_variants SET translated_text_en = {child_sql}, normalized_text_en = {normalized_sql}, match_method = 'manual', match_confidence = 1.0000 WHERE question_id = {target_id};",
            ]
        )
    # Process later questions first so sequence numbers keep their original order.
    for row in rows:
        children = split.get(row["question_id"], [])
        delta = len(children) - 1
        interview_id = row["interview_id"]
        sequence_no = row["sequence_no"]
        statements.extend(
            [
                f"DELETE FROM question_occurrences WHERE id = {row['occurrence_id']};",
                f"UPDATE question_occurrences SET sequence_no = sequence_no + 1000000 WHERE interview_id = {interview_id} AND sequence_no > {sequence_no};",
                f"UPDATE question_occurrences SET sequence_no = sequence_no - 1000000 + ({delta}) WHERE interview_id = {interview_id} AND sequence_no > {sequence_no + 1000000};",
            ]
        )
        for index, child in enumerate(children):
            normalized = dedupe_key(child)
            child_sql = sql_text(child)
            normalized_sql = sql_text(normalized)
            original_sql = sql_text(row["original_text"])
            language_sql = sql_text(row["language"])
            hash_sql = sql_text(row["text_sha256"])
            statements.extend(
                [
                    f"INSERT INTO questions (canonical_text_en, normalized_key_en, needs_review) VALUES ({child_sql}, {normalized_sql}, FALSE) ON CONFLICT (normalized_key_en) DO NOTHING;",
                    "INSERT INTO question_variants (question_id, original_text, language, translated_text_en, normalized_text_en, text_sha256, match_method, match_confidence) "
                    f"SELECT id, {original_sql}, {language_sql}, {child_sql}, {normalized_sql}, {hash_sql}, 'manual', 1.0000 FROM questions WHERE normalized_key_en = {normalized_sql} "
                    "ON CONFLICT (question_id, language, text_sha256) DO UPDATE SET translated_text_en = EXCLUDED.translated_text_en, normalized_text_en = EXCLUDED.normalized_text_en, match_method = 'manual', match_confidence = 1.0000;",
                    "INSERT INTO question_occurrences (interview_id, variant_id, source_question_key, sequence_no) "
                    f"SELECT {interview_id}, qv.id, {sql_nullable(row['source_question_key'])}, {sequence_no + index} "
                    "FROM questions q JOIN question_variants qv ON qv.question_id = q.id "
                    f"WHERE q.normalized_key_en = {normalized_sql} AND qv.language = {language_sql} AND BTRIM(qv.text_sha256) = {hash_sql};",
                ]
            )
            if normalized not in planned_keys:
                category = classify(normalized, Counter())
                statements.append(
                    "INSERT INTO question_tags (question_id, tag_id, is_primary, assigned_by, confidence) "
                    f"SELECT id, {tag_ids[category]}, TRUE, 'manual', 1.0000 FROM questions WHERE normalized_key_en = {normalized_sql} "
                    "ON CONFLICT (question_id, tag_id) DO UPDATE SET is_primary = TRUE, assigned_by = 'manual', confidence = 1.0000;"
                )
                planned_keys.add(normalized)

    for question_id in sorted(expected_ids):
        statements.extend(
            [
                f"DELETE FROM question_tags WHERE question_id = {question_id};",
                f"DELETE FROM question_variants WHERE question_id = {question_id};",
                f"DELETE FROM questions WHERE id = {question_id};",
            ]
        )
    statements.extend(
        [
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM questions WHERE needs_review) THEN RAISE EXCEPTION 'review queue is not empty'; END IF; END $$;",
            "COMMIT;",
        ]
    )
    run_psql(args.database_url, "-q", input_text="\n".join(statements))

    actual = query_json(
        args.database_url,
        """
SELECT json_build_object(
    'questions', (SELECT COUNT(*) FROM questions),
    'occurrences', (SELECT COUNT(*) FROM question_occurrences),
    'questions_needing_review', (SELECT COUNT(*) FROM questions WHERE needs_review),
    'orphan_occurrences', (
        SELECT COUNT(*) FROM question_occurrences qo
        LEFT JOIN interviews i ON i.id = qo.interview_id
        LEFT JOIN question_variants qv ON qv.id = qo.variant_id
        WHERE i.id IS NULL OR qv.id IS NULL
    )
)::text;
""",
    )
    assert actual["questions"] == summary["resulting_questions"], actual
    assert actual["occurrences"] == summary["resulting_occurrences"], actual
    assert actual["questions_needing_review"] == 0, actual
    assert actual["orphan_occurrences"] == 0, actual
    print(json.dumps(actual, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
