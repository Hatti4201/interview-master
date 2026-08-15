#!/usr/bin/env python3
"""Apply the four audited translation batches in one PostgreSQL transaction."""

import argparse
import json
from collections import Counter

from import_master import DEFAULT_DATABASE_URL, ROOT, classify, dedupe_key, run_psql


def sql_text(value: str) -> str:
    return "'" + value.replace("\x00", "").replace("'", "''") + "'"


def query_json(database_url: str, sql: str):
    return json.loads(run_psql(database_url, "-At", "-c", sql) or "null")


def load_review():
    approved = {}
    split_review = {}
    remove = set()
    for path in sorted(ROOT.glob("translation_review_batch*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        approved.update({int(key): value for key, value in data["approved"].items()})
        split_review.update({int(key): value for key, value in data["split_review"].items()})
        remove.update(data["remove"])
    groups = [set(approved), set(split_review), remove]
    assert not (groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2])
    return approved, split_review, remove


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    approved, split_review, remove = load_review()
    expected_ids = set(
        query_json(
            args.database_url,
            "SELECT COALESCE(json_agg(id ORDER BY id), '[]')::text FROM questions WHERE needs_review;",
        )
    )
    covered_ids = set(approved) | set(split_review) | remove
    if covered_ids != expected_ids:
        raise SystemExit(
            json.dumps(
                {
                    "missing": sorted(expected_ids - covered_ids),
                    "extra": sorted(covered_ids - expected_ids),
                }
            )
        )

    state = query_json(
        args.database_url,
        """
SELECT json_build_object(
    'questions', (SELECT json_agg(json_build_object(
        'id', id, 'key', normalized_key_en, 'needs_review', needs_review
    ) ORDER BY id) FROM questions),
    'variants', (SELECT json_agg(json_build_object(
        'id', id, 'question_id', question_id, 'language', language,
        'hash', BTRIM(text_sha256)
    ) ORDER BY id) FROM question_variants),
    'tags', (SELECT json_agg(json_build_object('id', id, 'name_zh', name_zh)) FROM tags)
)::text;
""",
    )
    question_keys = {item["key"]: item["id"] for item in state["questions"]}
    old_keys = {item["id"]: item["key"] for item in state["questions"]}
    variant_by_question = {}
    variant_index = {}
    for item in state["variants"]:
        variant_by_question.setdefault(item["question_id"], []).append(item)
        variant_index[(item["question_id"], item["language"], item["hash"])] = item["id"]
    tag_ids = {item["name_zh"]: item["id"] for item in state["tags"]}

    statements = ["BEGIN;"]
    for question_id in sorted(remove):
        statements.extend(
            [
                f"DELETE FROM question_occurrences WHERE variant_id IN (SELECT id FROM question_variants WHERE question_id = {question_id});",
                f"DELETE FROM question_tags WHERE question_id = {question_id};",
                f"DELETE FROM question_variants WHERE question_id = {question_id};",
                f"DELETE FROM questions WHERE id = {question_id};",
            ]
        )
        old_key = old_keys.get(question_id)
        if old_key and question_keys.get(old_key) == question_id:
            question_keys.pop(old_key, None)

    merged = 0
    updated = 0
    for action, records in (("approved", approved), ("split_review", split_review)):
        for question_id, translation in records.items():
            normalized = dedupe_key(translation)
            target_id = question_keys.get(normalized)
            confidence = "1.0000" if action == "approved" else "0.7500"
            translation_sql = sql_text(translation)
            normalized_sql = sql_text(normalized)
            if target_id is not None and target_id != question_id:
                for variant in variant_by_question.get(question_id, []):
                    duplicate_id = variant_index.get(
                        (target_id, variant["language"], variant["hash"])
                    )
                    if duplicate_id is not None:
                        statements.extend(
                            [
                                f"UPDATE question_occurrences SET variant_id = {duplicate_id} WHERE variant_id = {variant['id']};",
                                f"UPDATE question_variants SET translated_text_en = {translation_sql}, normalized_text_en = {normalized_sql}, match_method = 'translated', match_confidence = {confidence} WHERE id = {duplicate_id};",
                                f"DELETE FROM question_variants WHERE id = {variant['id']};",
                            ]
                        )
                    else:
                        statements.append(
                            f"UPDATE question_variants SET question_id = {target_id}, translated_text_en = {translation_sql}, normalized_text_en = {normalized_sql}, match_method = 'translated', match_confidence = {confidence} WHERE id = {variant['id']};"
                        )
                        variant_index[(target_id, variant["language"], variant["hash"])] = variant[
                            "id"
                        ]
                statements.extend(
                    [
                        f"DELETE FROM question_tags WHERE question_id = {question_id};",
                        f"DELETE FROM questions WHERE id = {question_id};",
                    ]
                )
                merged += 1
                continue

            statements.extend(
                [
                    f"UPDATE questions SET canonical_text_en = {translation_sql}, normalized_key_en = {normalized_sql}, needs_review = {'FALSE' if action == 'approved' else 'TRUE'} WHERE id = {question_id};",
                    f"UPDATE question_variants SET translated_text_en = {translation_sql}, normalized_text_en = {normalized_sql}, match_method = 'translated', match_confidence = {confidence} WHERE question_id = {question_id};",
                ]
            )
            category = classify(normalized, Counter())
            statements.extend(
                [
                    f"DELETE FROM question_tags WHERE question_id = {question_id} AND is_primary;",
                    f"INSERT INTO question_tags (question_id, tag_id, is_primary, assigned_by, confidence) VALUES ({question_id}, {tag_ids[category]}, TRUE, 'manual', {confidence}) ON CONFLICT (question_id, tag_id) DO UPDATE SET is_primary = TRUE, assigned_by = 'manual', confidence = EXCLUDED.confidence;",
                ]
            )
            old_key = old_keys.get(question_id)
            if old_key and question_keys.get(old_key) == question_id:
                question_keys.pop(old_key, None)
            question_keys[normalized] = question_id
            updated += 1

    statements.append("COMMIT;")
    summary = {
        "approved": len(approved),
        "translated_but_needs_split": len(split_review),
        "removed_derived_questions": len(remove),
        "merged_into_existing_questions": merged,
        "updated_questions": updated,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not args.dry_run:
        run_psql(args.database_url, "-q", "-c", "\n".join(statements))


if __name__ == "__main__":
    main()
