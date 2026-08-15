#!/usr/bin/env python3
"""Build the local PostgreSQL master from the preserved raw files."""

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
sys.path.insert(0, str(WORKSPACE))

from extract_interview_questions import CATEGORY_RULES, classify, dedupe_key, split_questions  # noqa: E402


DEFAULT_DATABASE_URL = (
    "postgresql://interview_master:local_interview_only@127.0.0.1:54329/interview_master"
)

CATEGORY_EN = {
    "自我介绍与行为面试": ("behavioral", "Behavioral and self-introduction"),
    "安全、认证与权限": ("security-auth", "Security, authentication, and authorization"),
    "测试与质量保障": ("testing-qa", "Testing and quality assurance"),
    "云、DevOps 与容器": ("cloud-devops", "Cloud, DevOps, and containers"),
    "数据库、SQL 与缓存": ("database-sql-cache", "Databases, SQL, and caching"),
    "React、Angular 与前端": ("frontend", "React, Angular, and frontend"),
    "JavaScript、TypeScript 与 Node.js": (
        "javascript-typescript-node",
        "JavaScript, TypeScript, and Node.js",
    ),
    "Java、Spring 与 JVM": ("java-spring-jvm", "Java, Spring, and JVM"),
    "Python": ("python", "Python"),
    "AI、机器学习与数据工程": ("ai-ml-data", "AI, machine learning, and data engineering"),
    "系统设计、架构与分布式系统": (
        "system-design",
        "System design, architecture, and distributed systems",
    ),
    "后端、API 与微服务": ("backend-api", "Backend, APIs, and microservices"),
    "编程题、算法与数据结构": (
        "algorithms-data-structures",
        "Coding, algorithms, and data structures",
    ),
    "计算机基础、OOP 与并发": (
        "computer-science-oop-concurrency",
        "Computer science, OOP, and concurrency",
    ),
    "项目经历与工程实践": ("project-engineering", "Projects and engineering practices"),
    "综合与其他问题": ("general-other", "General and other questions"),
}


def run_psql(database_url: str, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["psql", database_url, "-v", "ON_ERROR_STOP=1", *args],
        input=input_text,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def nullable(value):
    return None if value is None or value == "" else value


def language_of(text: str) -> str:
    has_zh = bool(re.search(r"[\u3400-\u9fff]", text))
    has_en = bool(re.search(r"[A-Za-z]", text))
    if has_zh and has_en:
        return "mixed"
    if has_zh:
        return "zh"
    if has_en:
        return "en"
    return "unknown"


def canonical_display(key: str, variants: Counter) -> tuple[str, str, bool]:
    display = max(variants, key=lambda text: (variants[text], -len(text), text))
    if key == "tell me about yourself":
        return "Tell me about yourself", key, False
    if language_of(display) in {"zh", "mixed"}:
        token = sha256_bytes(key.encode("utf-8"))[:12]
        return f"Translation pending [{token}]", f"translation-pending-{token}", True
    display = re.sub(r"^(?:q(?:uestion)?\s*)?\d+\s*[.):、,-]\s*", "", display, flags=re.I)
    return display, key, False


def write_copy(process, table: str, columns: tuple[str, ...], rows: list[tuple]) -> None:
    process.stdin.write(
        f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT CSV, NULL '\\N');\n"
    )
    writer = csv.writer(process.stdin, lineterminator="\n")
    for row in rows:
        writer.writerow([r"\N" if value is None else value for value in row])
    process.stdin.write("\\.\n")


def copy_all(
    database_url: str,
    tables: list[tuple[str, tuple[str, ...], list[tuple]]],
    post_sql: str = "",
) -> None:
    process = subprocess.Popen(
        ["psql", database_url, "-q", "-v", "ON_ERROR_STOP=1"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    process.stdin.write("BEGIN;\n")
    for table, columns, rows in tables:
        write_copy(process, table, columns, rows)
    process.stdin.write(
        """
SELECT setval(pg_get_serial_sequence('sources', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM sources;
SELECT setval(pg_get_serial_sequence('raw_records', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM raw_records;
SELECT setval(pg_get_serial_sequence('interviews', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM interviews;
SELECT setval(pg_get_serial_sequence('questions', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM questions;
SELECT setval(pg_get_serial_sequence('question_variants', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM question_variants;
SELECT setval(pg_get_serial_sequence('question_occurrences', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM question_occurrences;
SELECT setval(pg_get_serial_sequence('tags', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM tags;
"""
        + post_sql
        + """
COMMIT;
"""
    )
    process.stdin.close()
    stdout = process.stdout.read() if process.stdout else ""
    stderr = process.stderr.read() if process.stderr else ""
    return_code = process.wait()
    if return_code:
        raise RuntimeError(stderr.strip() or stdout.strip())


def build_rows(pilot_path: Path, text_paths: list[Path]):
    pilot_bytes = pilot_path.read_bytes()
    pilot_records = json.loads(pilot_bytes.decode("utf-8"))

    source_files = [(pilot_path, "json"), *((path, "txt") for path in text_paths)]
    sources = []
    raw_records = []
    for source_id, (path, source_kind) in enumerate(source_files, 1):
        data = path.read_bytes()
        sources.append(
            (
                source_id,
                path.name,
                source_kind,
                f"raw/{path.name}",
                sha256_bytes(data),
                len(data),
            )
        )

    raw_id = 0
    pilot_raw_ids = []
    for record_index, record in enumerate(pilot_records, 1):
        raw_id += 1
        pilot_raw_ids.append(raw_id)
        raw_records.append(
            (
                raw_id,
                1,
                record_index,
                str(record.get("id")) if record.get("id") is not None else None,
                None,
                None,
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                None,
                "parsed",
                1,
                None,
            )
        )

    for source_id, path in enumerate(text_paths, 2):
        text = path.read_text(encoding="utf-8-sig")
        raw_id += 1
        raw_records.append(
            (
                raw_id,
                source_id,
                1,
                None,
                1,
                len(text.splitlines()),
                text,
                None,
                "pending",
                None,
                None,
            )
        )

    interviews = []
    grouped = {}
    occurrence_drafts = []
    for interview_id, (raw_record_id, record) in enumerate(zip(pilot_raw_ids, pilot_records), 1):
        interviews.append(
            (
                interview_id,
                raw_record_id,
                nullable(str(record.get("id"))) if record.get("id") is not None else None,
                nullable(record.get("date")),
                nullable(record.get("client")),
                nullable(record.get("vendor")),
                nullable(record.get("interviewer")),
                nullable(record.get("candidate")),
                nullable(record.get("position")),
                nullable(record.get("round")),
                nullable(record.get("type")),
                nullable(record.get("notes")),
                nullable(record.get("feedback")),
                nullable(record.get("createdBy")),
                nullable(record.get("createdAt")),
            )
        )
        sequence_no = 0
        position = (record.get("position") or "").strip().upper()
        for source_question in record.get("questions", []):
            for original_text in split_questions(source_question.get("text", "")):
                key = dedupe_key(original_text)
                if not key:
                    continue
                sequence_no += 1
                item = grouped.setdefault(
                    key,
                    {"variants": Counter(), "positions": Counter(), "variant_order": []},
                )
                if original_text not in item["variants"]:
                    item["variant_order"].append(original_text)
                item["variants"][original_text] += 1
                item["positions"][position] += 1
                occurrence_drafts.append(
                    (
                        interview_id,
                        key,
                        original_text,
                        nullable(str(source_question.get("id")))
                        if source_question.get("id") is not None
                        else None,
                        sequence_no,
                    )
                )

    questions = []
    variants = []
    variant_ids = {}
    categories = []
    question_categories = []
    variant_id = 0
    for question_id, (key, item) in enumerate(grouped.items(), 1):
        canonical_text, normalized_key, needs_review = canonical_display(key, item["variants"])
        questions.append((question_id, canonical_text, normalized_key, needs_review))
        canonical_variant = max(
            item["variants"],
            key=lambda text: (item["variants"][text], -len(text), text),
        )
        for original_text in item["variant_order"]:
            variant_id += 1
            language = language_of(original_text)
            variants.append(
                (
                    variant_id,
                    question_id,
                    original_text,
                    language,
                    None,
                    dedupe_key(original_text) if language == "en" else None,
                    sha256_bytes(original_text.encode("utf-8")),
                    "new" if original_text == canonical_variant else "normalized",
                    1,
                )
            )
            variant_ids[(key, original_text)] = variant_id
        category = classify(key, item["positions"])
        if category not in categories:
            categories.append(category)
        question_categories.append((question_id, category))

    tags = []
    tag_ids = {}
    for tag_id, category in enumerate(categories, 1):
        slug, name_en = CATEGORY_EN[category]
        tags.append((tag_id, slug, name_en, category))
        tag_ids[category] = tag_id

    occurrences = []
    for occurrence_id, draft in enumerate(occurrence_drafts, 1):
        interview_id, key, original_text, source_question_key, sequence_no = draft
        occurrences.append(
            (
                occurrence_id,
                interview_id,
                variant_ids[(key, original_text)],
                source_question_key,
                sequence_no,
            )
        )

    question_tags = [
        (question_id, tag_ids[category], True, "rule", 0.8)
        for question_id, category in question_categories
    ]

    return [
        (
            "sources",
            ("id", "original_filename", "source_kind", "storage_path", "sha256", "byte_size"),
            sources,
        ),
        (
            "raw_records",
            (
                "id",
                "source_id",
                "record_index",
                "source_record_key",
                "line_start",
                "line_end",
                "raw_content",
                "parsed_payload",
                "parse_status",
                "parse_confidence",
                "parse_error",
            ),
            raw_records,
        ),
        (
            "interviews",
            (
                "id",
                "raw_record_id",
                "external_id",
                "interview_date",
                "client",
                "vendor",
                "interviewer",
                "candidate",
                "position",
                "interview_round",
                "employment_type",
                "notes",
                "feedback",
                "source_created_by",
                "source_created_at",
            ),
            interviews,
        ),
        (
            "questions",
            ("id", "canonical_text_en", "normalized_key_en", "needs_review"),
            questions,
        ),
        (
            "question_variants",
            (
                "id",
                "question_id",
                "original_text",
                "language",
                "translated_text_en",
                "normalized_text_en",
                "text_sha256",
                "match_method",
                "match_confidence",
            ),
            variants,
        ),
        (
            "question_occurrences",
            ("id", "interview_id", "variant_id", "source_question_key", "sequence_no"),
            occurrences,
        ),
        ("tags", ("id", "slug", "name_en", "name_zh"), tags),
        (
            "question_tags",
            ("question_id", "tag_id", "is_primary", "assigned_by", "confidence"),
            question_tags,
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("INTERVIEW_MASTER_DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        assert language_of("介绍 yourself") == "mixed"
        assert language_of("Tell me about yourself") == "en"
        assert dedupe_key("Introduce yourself") == "tell me about yourself"
        print("self-test passed")
        return

    pilot_path = ROOT / "raw" / "pilot_interviews.json"
    text_paths = [
        ROOT / "raw" / "Java Backend Interview Questions.txt",
        ROOT / "raw" / "第十三期offer收割机- Interview Questions Notes No.13.txt",
    ]
    missing = [str(path) for path in [pilot_path, *text_paths] if not path.exists()]
    if missing:
        raise SystemExit("Missing raw file(s): " + ", ".join(missing))

    schema_exists = run_psql(
        args.database_url,
        "-At",
        "-c",
        "SELECT to_regclass('public.sources') IS NOT NULL",
    )
    if schema_exists != "t":
        run_psql(args.database_url, "-f", str(ROOT / "database" / "schema.sql"))
    elif int(run_psql(args.database_url, "-At", "-c", "SELECT COUNT(*) FROM sources")):
        raise SystemExit("Database already contains data; refusing to overwrite the master.")

    tables = build_rows(pilot_path, text_paths)
    copy_all(args.database_url, tables)
    counts = {table: len(rows) for table, _, rows in tables}
    print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
