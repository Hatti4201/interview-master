#!/usr/bin/env python3
"""Parse the two semi-structured TXT sources and append reviewable records."""

import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from import_master import (
    ROOT,
    DEFAULT_DATABASE_URL,
    canonical_display,
    classify,
    copy_all,
    dedupe_key,
    language_of,
    nullable,
    run_psql,
    sha256_bytes,
    split_questions,
)


DATE_PREFIX = re.compile(r"^\*?\s*\d{1,2}\s*[/.-]\s*\d{1,2}")
NUMBER_LINE = re.compile(r"^\s*(\d{1,4})\s*$")
CJK = re.compile(r"[\u3400-\u9fff]")
QUESTION_SIGNAL = re.compile(
    r"(?:\?|？)|^(?:q(?:uestion)?\s*)?\d{1,2}\s*[.):、,-]\s*|"
    r"^(?:what|why|how|when|where|who|which|can|could|do|does|did|have|has|is|are|"
    r"would|should|explain|describe|tell|write|implement|design|create|find|given|"
    r"introduce|compare|walk|coding|system design|self intro|project|difference|"
    r"first round|second round|client round|vendor round|final round)\b",
    re.I,
)
FORM_SIGNAL = re.compile(
    r"\b(?:zoom|webex|teams?|skype|google\s*meet|oa|hackerrank|glider|round|onsite|vo|"
    r"phone|call|minutes?|mins?|hours?|hrs?)\b",
    re.I,
)
BOILERPLATE = re.compile(
    r"^(?:no\.?|link/no\.?|编号|date|日期|vendor|client|shared? by|分享人|"
    r"interview form.*|x-?share.*|please update your notes.*)$|"
    r"面试完的同学记得更新|请大家面试完及时|update your notes/experience|"
    r"^(?:exact(?:ly)?\s+)?same (?:questions? )?as\s+(?:no\.?\s*)?\d+\.?$",
    re.I,
)
HEADING = re.compile(
    r"^(?:(?:first|second|third|final|client|vendor|technical|tech)\s+round.*|"
    r"round\s*\d+.*|coding\s*[：:;]?|bq\s*[：:;]?|八股\s*[：:;]?|"
    r"反问(?:面试官)?\s*[：:;]?|questions?\s*:?)$",
    re.I,
)
CHINESE_QUESTION_SIGNAL = re.compile(
    r"(?:什么|如何|怎么|为什么|区别|介绍|解释|是否|能否|请|实现|设计|写一个|算法题|情景题|问了|追问)"
)
ANSWER_PREFIX = re.compile(r"^(?:回答|答案|参考|我说|因为|然后|例如|比如|解法|output|example)\b", re.I)


@dataclass
class Block:
    source_id: int
    source_name: str
    source_number: str
    line_start: int
    line_end: int
    raw_date: str
    interview_date: str | None
    date_inferred: bool
    vendor: str | None
    client: str | None
    interviewer: str | None
    candidate: str | None
    interview_round: str | None
    position: str
    raw_content: str
    body: str
    questions: list[tuple[str, float]]
    parse_status: str
    parse_confidence: float


def nonempty(lines: list[str]) -> list[tuple[int, str]]:
    return [(line_no, value.strip()) for line_no, value in enumerate(lines, 1) if value.strip()]


def header_candidates(cells: list[tuple[int, str]]) -> list[tuple[int, int, int]]:
    candidates = []
    for index, (line_no, value) in enumerate(cells[:-1]):
        match = NUMBER_LINE.fullmatch(value)
        if match and DATE_PREFIX.match(cells[index + 1][1]):
            candidates.append((index, line_no, int(match.group(1))))
    return candidates


def detail_candidates(candidates: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    start = next(
        (
            index
            for index in range(1, len(candidates))
            if candidates[index][2] == 1 and candidates[index - 1][2] > 10
        ),
        0,
    )
    return candidates[start:]


def date_parts(raw: str) -> tuple[int, int, int | None] | None:
    if re.search(r"[A-Za-z]", raw):
        return None
    parts = [int(value) for value in re.findall(r"\d+", raw)]
    if len(parts) < 2:
        return None
    month, day = parts[:2]
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    year = parts[2] if len(parts) >= 3 else None
    if year is not None and year < 100:
        year += 2000
    if year is not None and not (2020 <= year <= 2030):
        return None
    return month, day, year


def infer_dates(raw_dates: list[str]) -> list[tuple[str | None, bool]]:
    parsed = [date_parts(raw) for raw in raw_dates]
    years = [value[2] if value else None for value in parsed]
    explicit = [index for index, year in enumerate(years) if year is not None]
    if not explicit:
        return [(None, False) for _ in raw_dates]

    first = explicit[0]
    current_year = years[first]
    next_month = parsed[first][0]
    for index in range(first - 1, -1, -1):
        value = parsed[index]
        if value is None:
            continue
        month = value[0]
        if month > next_month + 6:
            current_year -= 1
        years[index] = current_year
        next_month = month

    current_year = years[first]
    previous_month = parsed[first][0]
    for index in range(first + 1, len(parsed)):
        value = parsed[index]
        if value is None:
            continue
        month, _, explicit_year = value
        if explicit_year is not None:
            current_year = explicit_year
        elif month < previous_month - 6:
            current_year += 1
        years[index] = current_year
        previous_month = month

    results = []
    for value, inferred_year in zip(parsed, years):
        if value is None or inferred_year is None:
            results.append((None, False))
            continue
        try:
            normalized = date(inferred_year, value[0], value[1]).isoformat()
        except ValueError:
            results.append((None, False))
            continue
        results.append((normalized, value[2] is None))
    return results


def looks_like_body(value: str) -> bool:
    if len(value) >= 80 or QUESTION_SIGNAL.search(value):
        return True
    return bool(CJK.search(value) and len(value) >= 10)


def clean_label(value: str, label: str) -> str | None:
    result = re.sub(rf"^{label}\s*:\s*", "", value, flags=re.I).strip()
    return result or None


def parse_metadata(values: list[str]) -> tuple[dict, int]:
    scan = values[:10]
    body_start = next((index for index, value in enumerate(scan) if looks_like_body(value)), None)
    if body_start is None:
        body_start = min(4, len(values))
    metadata = values[:body_start]
    result = {"vendor": None, "client": None, "interviewer": None, "candidate": None, "round": None}

    labeled = any(re.match(r"^(?:client|vendor|interviewer)\s*:", value, re.I) for value in metadata)
    if labeled:
        unlabeled = []
        interviewer_values = []
        active_label = None
        for value in metadata:
            label_match = re.match(r"^(client|vendor|interviewer)\s*:\s*(.*)$", value, re.I)
            if label_match:
                active_label = label_match.group(1).lower()
                inline = label_match.group(2).strip()
                if inline:
                    if active_label == "interviewer":
                        interviewer_values.append(inline)
                    else:
                        result[active_label] = inline
                    active_label = None
                continue
            if active_label == "client":
                result["client"] = value
                active_label = None
            elif active_label == "vendor":
                result["vendor"] = value
                active_label = None
            elif active_label == "interviewer":
                interviewer_values.append(value)
            else:
                unlabeled.append(value)
        if interviewer_values:
            if len(interviewer_values) > 1:
                result["candidate"] = interviewer_values[-1]
                interviewer_values = interviewer_values[:-1]
            result["interviewer"] = " / ".join(interviewer_values) or None
        for value in unlabeled:
            if FORM_SIGNAL.search(value):
                result["round"] = value
            elif result["client"] is None:
                result["client"] = value
            elif result["candidate"] is None:
                result["candidate"] = value
        return result, body_start

    if len(metadata) >= 1:
        result["vendor"] = metadata[0]
    if len(metadata) >= 2:
        result["client"] = metadata[1]
    tail = metadata[2:]
    if tail:
        result["candidate"] = tail[-1]
        middle = tail[:-1]
        for value in middle:
            if FORM_SIGNAL.search(value):
                result["round"] = " / ".join(filter(None, [result["round"], value]))
            else:
                result["interviewer"] = " / ".join(filter(None, [result["interviewer"], value]))
    if result["client"] and FORM_SIGNAL.fullmatch(result["client"]):
        result["round"] = result["client"]
        result["client"] = None
    return result, body_start


def question_strength(text: str) -> float:
    if "?" in text or "？" in text:
        return 0.9
    if QUESTION_SIGNAL.search(text):
        return 0.8
    if CJK.search(text):
        return 0.65
    return 0.6


def extract_questions(body: str) -> list[tuple[str, float]]:
    virtual_lines = []
    for value in body.splitlines():
        value = re.sub(r"\s+", " ", value).strip()
        if not value or BOILERPLATE.search(value) or re.fullmatch(r"[_=*#-]{3,}", value):
            continue
        chunks = re.split(
            r"(?<!\w)(?=\d{1,2}\s*(?:[.)、]|,(?=\s+[A-Za-z\u3400-\u9fff]))\s*(?=[A-Za-z\u3400-\u9fff]))",
            value,
        )
        virtual_lines.extend(chunk.strip() for chunk in chunks if chunk.strip())

    grouped = []
    current = []
    current_strength = 0.0
    for value in virtual_lines:
        if BOILERPLATE.search(value) or HEADING.fullmatch(value):
            continue
        numbered = bool(re.match(r"^\d{1,2}\s*(?:[.)、]|,\s+)", value))
        explicit = bool(QUESTION_SIGNAL.search(value))
        chinese_prompt = bool(CHINESE_QUESTION_SIGNAL.search(value) and not ANSWER_PREFIX.match(value))
        short_topic = (
            len(value) <= 100
            and not value.startswith(("http://", "https://"))
            and not re.match(r"^(?:public|private|protected|class|import|return|if|for|while|//|/\*)\b", value)
        )
        incomplete_previous = bool(
            current
            and re.search(
                r"(?:between|versus|vs\.?|and|or|of|for|to|with|about|from|following|:)\s*$",
                current[-1],
                re.I,
            )
        )
        starts_new = numbered or explicit or chinese_prompt or (
            short_topic and bool(current) and not incomplete_previous
        )
        if starts_new and current:
            grouped.append((" ".join(current), current_strength))
            current = []
        if not current:
            current_strength = (
                0.9
                if "?" in value or "？" in value
                else 0.8
                if explicit
                else 0.75
                if numbered
                else 0.7
                if chinese_prompt
                else 0.55
            )
        current.append(value)
    if current:
        grouped.append((" ".join(current), current_strength))

    questions = []
    for text, strength in grouped:
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 3 or BOILERPLATE.search(text) or re.fullmatch(r"https?://\S+", text):
            continue
        questions.append((text, strength))
    return questions


def parse_source(path: Path, source_id: int, position: str) -> list[Block]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    cells = nonempty(lines)
    candidates = detail_candidates(header_candidates(cells))
    raw_dates = [cells[index + 1][1] for index, _, _ in candidates]
    normalized_dates = infer_dates(raw_dates)
    blocks = []
    for offset, ((index, line_start, source_number), normalized_date) in enumerate(
        zip(candidates, normalized_dates)
    ):
        next_index = candidates[offset + 1][0] if offset + 1 < len(candidates) else len(cells)
        line_end = candidates[offset + 1][1] - 1 if offset + 1 < len(candidates) else len(lines)
        block_cells = cells[index:next_index]
        raw_date = block_cells[1][1]
        values = [value for _, value in block_cells[2:]]
        metadata, body_start = parse_metadata(values)
        body_cells = block_cells[2 + body_start :]
        body = "\n".join(value for _, value in body_cells).strip()
        questions = extract_questions(body)
        valid_date, inferred = normalized_date
        confidence = 0.8
        if inferred:
            confidence -= 0.1
        if valid_date is None:
            confidence -= 0.25
        if not body:
            confidence -= 0.2
        if not metadata["vendor"] and not metadata["client"]:
            confidence -= 0.1
        confidence = max(0.2, round(confidence, 2))
        blocks.append(
            Block(
                source_id=source_id,
                source_name=path.name,
                source_number=str(source_number),
                line_start=line_start,
                line_end=line_end,
                raw_date=raw_date,
                interview_date=valid_date,
                date_inferred=inferred,
                vendor=nullable(metadata["vendor"]),
                client=nullable(metadata["client"]),
                interviewer=nullable(metadata["interviewer"]),
                candidate=nullable(metadata["candidate"]),
                interview_round=nullable(metadata["round"]),
                position=position,
                raw_content="\n".join(lines[line_start - 1 : line_end]),
                body=body,
                questions=questions,
                parse_status="parsed" if confidence >= 0.7 else "needs_review",
                parse_confidence=confidence,
            )
        )
    return blocks


def query_json(database_url: str, sql: str):
    value = run_psql(database_url, "-At", "-c", sql)
    return json.loads(value or "null")


def database_state(database_url: str):
    maxima = query_json(
        database_url,
        """
SELECT json_build_object(
    'raw_records', COALESCE((SELECT MAX(id) FROM raw_records), 0),
    'interviews', COALESCE((SELECT MAX(id) FROM interviews), 0),
    'questions', COALESCE((SELECT MAX(id) FROM questions), 0),
    'variants', COALESCE((SELECT MAX(id) FROM question_variants), 0),
    'occurrences', COALESCE((SELECT MAX(id) FROM question_occurrences), 0)
)::text;
""",
    )
    questions = query_json(
        database_url,
        "SELECT COALESCE(json_agg(json_build_object('id', id, 'key', normalized_key_en)), '[]')::text FROM questions;",
    )
    variants = query_json(
        database_url,
        "SELECT COALESCE(json_agg(json_build_object('id', id, 'question_id', question_id, 'language', language, 'hash', BTRIM(text_sha256))), '[]')::text FROM question_variants;",
    )
    tags = query_json(
        database_url,
        "SELECT COALESCE(json_agg(json_build_object('id', id, 'name_zh', name_zh)), '[]')::text FROM tags;",
    )
    return maxima, questions, variants, tags


def build_append_rows(database_url: str, blocks: list[Block]):
    maxima, existing_questions, existing_variants, existing_tags = database_state(database_url)
    question_ids = {item["key"]: item["id"] for item in existing_questions}
    variant_ids = {
        (item["question_id"], item["language"], item["hash"]): item["id"]
        for item in existing_variants
    }
    tag_ids = {item["name_zh"]: item["id"] for item in existing_tags}

    next_ids = {key: value + 1 for key, value in maxima.items()}
    raw_rows = []
    interview_rows = []
    question_rows = []
    variant_rows = []
    occurrence_rows = []
    question_tag_rows = []
    source_indexes = Counter()
    for block in blocks:
        source_indexes[block.source_id] += 1
        raw_id = next_ids["raw_records"]
        next_ids["raw_records"] += 1
        interview_id = next_ids["interviews"]
        next_ids["interviews"] += 1
        accepted_questions = [item for item in block.questions if item[1] >= 0.7]
        payload = json.dumps(
            {
                "raw_date": block.raw_date,
                "date_inferred": block.date_inferred,
                "question_candidates": len(block.questions),
                "questions_imported": len(accepted_questions),
                "weak_candidates_kept_in_raw": len(block.questions) - len(accepted_questions),
                "parser": "txt-v1",
            },
            ensure_ascii=False,
        )
        raw_rows.append(
            (
                raw_id,
                block.source_id,
                source_indexes[block.source_id] + 1,
                block.source_number,
                block.line_start,
                block.line_end,
                block.raw_content,
                payload,
                block.parse_status,
                block.parse_confidence,
                None,
            )
        )
        interview_rows.append(
            (
                interview_id,
                raw_id,
                block.source_number,
                block.interview_date,
                block.client,
                block.vendor,
                block.interviewer,
                block.candidate,
                block.position,
                block.interview_round,
                None,
                block.body,
                None,
                None,
                None,
            )
        )
        for sequence_no, (original_text, strength) in enumerate(accepted_questions, 1):
            language = language_of(original_text)
            raw_key = dedupe_key(original_text)
            if not raw_key:
                continue
            if not CJK.search(raw_key):
                canonical_text, normalized_key, language_review = canonical_display(
                    raw_key, Counter({original_text: 1})
                )
            else:
                token = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:12]
                canonical_text = f"Translation pending [{token}]"
                normalized_key = f"translation-pending-{token}"
                language_review = True
            question_id = question_ids.get(normalized_key)
            new_question = question_id is None
            if new_question:
                question_id = next_ids["questions"]
                next_ids["questions"] += 1
                question_ids[normalized_key] = question_id
                question_rows.append(
                    (
                        question_id,
                        canonical_text,
                        normalized_key,
                        language_review or strength < 0.75,
                    )
                )
                position_counter = Counter({block.position: 1})
                category = classify(raw_key, position_counter)
                question_tag_rows.append((question_id, tag_ids[category], True, "rule", strength))

            text_hash = sha256_bytes(original_text.encode("utf-8"))
            variant_key = (question_id, language, text_hash)
            variant_id = variant_ids.get(variant_key)
            if variant_id is None:
                variant_id = next_ids["variants"]
                next_ids["variants"] += 1
                variant_ids[variant_key] = variant_id
                variant_rows.append(
                    (
                        variant_id,
                        question_id,
                        original_text,
                        language,
                        "Tell me about yourself"
                        if language != "en" and normalized_key == "tell me about yourself"
                        else None,
                        raw_key if language == "en" else None,
                        text_hash,
                        "new" if new_question else "normalized",
                        strength,
                    )
                )
            occurrence_id = next_ids["occurrences"]
            next_ids["occurrences"] += 1
            occurrence_rows.append(
                (
                    occurrence_id,
                    interview_id,
                    variant_id,
                    f"txt:{block.source_number}",
                    sequence_no,
                )
            )

    return [
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
            raw_rows,
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
            interview_rows,
        ),
        (
            "questions",
            ("id", "canonical_text_en", "normalized_key_en", "needs_review"),
            question_rows,
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
            variant_rows,
        ),
        (
            "question_occurrences",
            ("id", "interview_id", "variant_id", "source_question_key", "sequence_no"),
            occurrence_rows,
        ),
        (
            "question_tags",
            ("question_id", "tag_id", "is_primary", "assigned_by", "confidence"),
            question_tag_rows,
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        assert date_parts("05/20/2026") == (5, 20, 2026)
        assert date_parts("6/16") == (6, 16, None)
        assert date_parts("* 1/2re6") is None
        assert detail_candidates([(0, 1, 1), (1, 2, 20), (2, 3, 1)])[0][1] == 3
        print("self-test passed")
        return

    source_specs = [
        (ROOT / "raw" / "Java Backend Interview Questions.txt", 2, "JAVA"),
        (ROOT / "raw" / "第十三期offer收割机- Interview Questions Notes No.13.txt", 3, "REACT"),
    ]
    blocks = [
        block
        for path, source_id, position in source_specs
        for block in parse_source(path, source_id, position)
    ]
    summary = {
        "blocks": len(blocks),
        "parsed": sum(block.parse_status == "parsed" for block in blocks),
        "needs_review": sum(block.parse_status == "needs_review" for block in blocks),
        "question_candidates": sum(len(block.questions) for block in blocks),
        "questions_to_import": sum(
            strength >= 0.7 for block in blocks for _, strength in block.questions
        ),
        "weak_candidates_kept_in_raw": sum(
            strength < 0.7 for block in blocks for _, strength in block.questions
        ),
        "dates_inferred": sum(block.date_inferred for block in blocks),
        "dates_missing": sum(block.interview_date is None for block in blocks),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for block in blocks[:3] + blocks[-3:]:
        print(
            json.dumps(
                {
                    "source": block.source_name,
                    "line": block.line_start,
                    "number": block.source_number,
                    "date": block.interview_date,
                    "vendor": block.vendor,
                    "client": block.client,
                    "questions": [text for text, _ in block.questions[:3]],
                },
                ensure_ascii=False,
            )
        )
    if args.dry_run:
        return

    already_imported = int(
        run_psql(
            args.database_url,
            "-At",
            "-c",
            "SELECT COUNT(*) FROM raw_records WHERE source_id IN (2, 3) AND record_index > 1",
        )
    )
    if already_imported:
        raise SystemExit("TXT sources already contain parsed records; refusing to append duplicates.")

    tables = build_append_rows(args.database_url, blocks)
    post_sql = """
UPDATE raw_records
SET parse_status = 'needs_review',
    parse_confidence = 0.7000,
    parsed_payload = jsonb_build_object('parser', 'txt-v1', 'detail_records_created', (
        SELECT COUNT(*) FROM raw_records child
        WHERE child.source_id = raw_records.source_id AND child.record_index > 1
    ))
WHERE source_id IN (2, 3) AND record_index = 1;
"""
    copy_all(args.database_url, tables, post_sql=post_sql)
    print(json.dumps({table: len(rows) for table, _, rows in tables}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
