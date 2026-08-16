#!/usr/bin/env python3
"""Interview-library JSON API with PostgreSQL sessions and role checks."""

import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from datetime import date
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


from runtime_support import (
    DEFAULT_DATABASE_URL,
    classify,
    dedupe_key,
    language_of,
    run_psql,
)


BACKEND_ROOT = Path(__file__).resolve().parent
MASTER_ROOT = BACKEND_ROOT.parent
DATABASE_URL = os.environ.get("INTERVIEW_MASTER_DATABASE_URL", DEFAULT_DATABASE_URL)
FRONTEND_ORIGIN = os.environ.get("INTERVIEW_FRONTEND_ORIGIN", "http://127.0.0.1:8010")
COOKIE_SECURE = os.environ.get("INTERVIEW_COOKIE_SECURE", "false").lower() == "true"
COOKIE_SAMESITE = os.environ.get("INTERVIEW_COOKIE_SAMESITE", "Lax").title()
if COOKIE_SAMESITE not in {"Lax", "Strict", "None"}:
    raise RuntimeError("INTERVIEW_COOKIE_SAMESITE must be Lax, Strict, or None")
if COOKIE_SAMESITE == "None" and not COOKIE_SECURE:
    raise RuntimeError("SameSite=None requires INTERVIEW_COOKIE_SECURE=true")
SESSION_COOKIE = "interview_session"
SESSION_SECONDS = 7 * 24 * 60 * 60
# ponytail: process-local login limiter; move to Redis when running multiple API replicas.
LOGIN_ATTEMPTS = {}


def sql_text(value: str) -> str:
    return "'" + value.replace("\x00", "").replace("'", "''") + "'"


def sql_nullable(value) -> str:
    return "NULL" if value is None or str(value).strip() == "" else sql_text(str(value).strip())


def query_json(sql: str):
    # ponytail: one psql process per request is enough for a local single-user app;
    # switch to a psycopg connection pool only when concurrent traffic requires it.
    output = run_psql(DATABASE_URL, "-qAt", "-c", sql)
    return json.loads(output or "null")


def execute_sql(sql: str) -> str:
    return run_psql(DATABASE_URL, "-qAt", input_text=sql)


def apply_auth_schema() -> None:
    run_psql(
        DATABASE_URL,
        "-qAt",
        "-f",
        str(MASTER_ROOT / "database" / "auth.sql"),
    )


def normalize_email(email: str) -> str:
    email = email.strip().casefold()
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("invalid email")
    return email


def validate_password(password: str) -> None:
    if len(password) < 10 or len(password) > 256:
        raise ValueError("password must be between 10 and 256 characters")


def password_digest(password: str, salt_hex: str) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=bytes.fromhex(salt_hex),
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    ).hex()


def password_record(password: str) -> tuple[str, str]:
    validate_password(password)
    salt = secrets.token_hex(16)
    return salt, password_digest(password, salt)


def create_user(email: str, password: str, role: str, display_name="", replace=False):
    email = normalize_email(email)
    if role not in {"admin", "user"}:
        raise ValueError("role must be admin or user")
    if len(display_name) > 120:
        raise ValueError("display_name is too long")
    existing = query_json(
        f"SELECT COALESCE((SELECT json_build_object('id', id) FROM app_users WHERE LOWER(BTRIM(email)) = {sql_text(email)}), 'null'::json)::text;"
    )
    if existing and not replace:
        raise ValueError("email already exists")
    salt, digest = password_record(password)
    if existing:
        return query_json(
            f"""
UPDATE app_users SET email = {sql_text(email)}, display_name = {sql_text(display_name)},
    role = {sql_text(role)}, password_salt = {sql_text(salt)},
    password_hash = {sql_text(digest)}, is_active = TRUE, updated_at = CURRENT_TIMESTAMP
WHERE id = {existing['id']}
RETURNING json_build_object('id', id, 'email', email, 'display_name', display_name, 'role', role, 'is_active', is_active)::text;
"""
        )
    return query_json(
        f"""
INSERT INTO app_users (email, display_name, role, password_salt, password_hash)
VALUES ({sql_text(email)}, {sql_text(display_name)}, {sql_text(role)}, {sql_text(salt)}, {sql_text(digest)})
RETURNING json_build_object('id', id, 'email', email, 'display_name', display_name, 'role', role, 'is_active', is_active)::text;
"""
    )


def session_from_token(token: str):
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()
    return query_json(
        f"""
SELECT COALESCE((
    SELECT json_build_object(
        'id', u.id, 'email', u.email, 'display_name', u.display_name,
        'role', u.role, 'csrf_token', s.csrf_token, 'token_hash', BTRIM(s.token_hash)
    )
    FROM app_sessions s
    JOIN app_users u ON u.id = s.user_id
    WHERE BTRIM(s.token_hash) = {sql_text(token_hash)}
      AND s.expires_at > CURRENT_TIMESTAMP AND u.is_active
), 'null'::json)::text;
"""
    )


def issue_session(user_id: int):
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    csrf = secrets.token_hex(32)
    query_json(
        f"""
WITH cleanup AS (DELETE FROM app_sessions WHERE expires_at <= CURRENT_TIMESTAMP)
INSERT INTO app_sessions (token_hash, user_id, csrf_token, expires_at)
VALUES ({sql_text(token_hash)}, {user_id}, {sql_text(csrf)}, CURRENT_TIMESTAMP + INTERVAL '7 days')
RETURNING json_build_object('created', TRUE)::text;
"""
    )
    return token, csrf


def login_allowed(key: str) -> bool:
    now = time.monotonic()
    attempts = [attempt for attempt in LOGIN_ATTEMPTS.get(key, []) if now - attempt < 300]
    LOGIN_ATTEMPTS[key] = attempts
    return len(attempts) < 5


def record_login_failure(key: str) -> None:
    LOGIN_ATTEMPTS.setdefault(key, []).append(time.monotonic())


def int_param(params, name: str, default: int, minimum: int, maximum: int) -> int:
    value = params.get(name, [str(default)])[0]
    if not value.isdigit():
        raise ValueError(f"{name} must be an integer")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def facet_param(params, name: str) -> str:
    value = params.get(name, [""])[0].strip().casefold()
    if len(value) > 120 or any(ord(char) < 32 for char in value):
        raise ValueError(f"invalid {name}")
    return value


def text_param(params, name: str, maximum: int = 160) -> str:
    value = params.get(name, [""])[0].strip()
    if len(value) > maximum or any(ord(char) < 32 for char in value):
        raise ValueError(f"invalid {name}")
    return value


def scope_question_filter(question_alias: str, company: str, vendor: str) -> str:
    if not company and not vendor:
        return "TRUE"
    conditions = []
    if company:
        conditions.append(f"LOWER(BTRIM(scope_interview.client)) = {sql_text(company)}")
    if vendor:
        conditions.append(f"LOWER(BTRIM(scope_interview.vendor)) = {sql_text(vendor)}")
    return f"""EXISTS (
        SELECT 1
        FROM question_variants scope_variant
        JOIN question_occurrences scope_occurrence ON scope_occurrence.variant_id = scope_variant.id
        JOIN interviews scope_interview ON scope_interview.id = scope_occurrence.interview_id
        WHERE scope_variant.question_id = {question_alias}.id
          AND {' AND '.join(conditions)}
    )"""


def scope_frequency(question_alias: str, company: str, vendor: str) -> str:
    if not company and not vendor:
        return "qf.interview_frequency"
    conditions = []
    if company:
        conditions.append(f"LOWER(BTRIM(scope_interview.client)) = {sql_text(company)}")
    if vendor:
        conditions.append(f"LOWER(BTRIM(scope_interview.vendor)) = {sql_text(vendor)}")
    return f"""(
        SELECT COUNT(DISTINCT scope_interview.id)
        FROM question_variants scope_variant
        JOIN question_occurrences scope_occurrence ON scope_occurrence.variant_id = scope_variant.id
        JOIN interviews scope_interview ON scope_interview.id = scope_occurrence.interview_id
        WHERE scope_variant.question_id = {question_alias}.id
          AND {' AND '.join(conditions)}
    )"""


def get_stats():
    return query_json(
        """
SELECT json_build_object(
    'interviews', (SELECT COUNT(*) FROM interviews),
    'questions', (SELECT COUNT(*) FROM questions WHERE NOT needs_review),
    'occurrences', (SELECT COUNT(*) FROM question_occurrences),
    'categories', (SELECT COUNT(*) FROM tags),
    'top_frequency', (SELECT COALESCE(MAX(interview_frequency), 0) FROM question_frequency)
)::text;
"""
    )


def get_companies():
    return query_json(
        """
SELECT COALESCE(json_agg(row_to_json(company) ORDER BY company.interviews DESC, company.company_name), '[]')::text
FROM (
    SELECT LOWER(BTRIM(i.client)) AS company_key,
        INITCAP(LOWER(BTRIM(i.client))) AS company_name,
        COUNT(DISTINCT i.id) AS interviews,
        COUNT(DISTINCT qv.question_id) AS questions
    FROM interviews i
    LEFT JOIN question_occurrences qo ON qo.interview_id = i.id
    LEFT JOIN question_variants qv ON qv.id = qo.variant_id
    WHERE NULLIF(BTRIM(i.client), '') IS NOT NULL
      AND LOWER(BTRIM(i.client)) NOT IN ('-', 'n/a', 'na', 'client', 'client interview')
    GROUP BY LOWER(BTRIM(i.client))
) company;
"""
    )


def get_vendors():
    return query_json(
        """
SELECT COALESCE(json_agg(row_to_json(vendor) ORDER BY vendor.interviews DESC, vendor.vendor_name), '[]')::text
FROM (
    SELECT LOWER(BTRIM(i.vendor)) AS vendor_key,
        INITCAP(LOWER(BTRIM(i.vendor))) AS vendor_name,
        COUNT(DISTINCT i.id) AS interviews,
        COUNT(DISTINCT qv.question_id) AS questions
    FROM interviews i
    LEFT JOIN question_occurrences qo ON qo.interview_id = i.id
    LEFT JOIN question_variants qv ON qv.id = qo.variant_id
    WHERE NULLIF(BTRIM(i.vendor), '') IS NOT NULL
      AND LOWER(BTRIM(i.vendor)) NOT IN ('-', 'n/a', 'na', 'vendor')
    GROUP BY LOWER(BTRIM(i.vendor))
) vendor;
"""
    )


def get_categories(params):
    company = facet_param(params, "company")
    vendor = facet_param(params, "vendor")
    frequency = scope_frequency("q", company, vendor)
    scope_filter = scope_question_filter("q", company, vendor)
    return query_json(
        f"""
WITH eligible AS (
    SELECT q.id, qt.tag_id, {frequency} AS frequency
    FROM questions q
    JOIN question_tags qt ON qt.question_id = q.id AND qt.is_primary
    JOIN question_frequency qf ON qf.question_id = q.id
    WHERE NOT q.needs_review AND {scope_filter}
)
SELECT COALESCE(json_agg(row_to_json(category) ORDER BY category.question_count DESC, category.name_zh), '[]')::text
FROM (
    SELECT t.slug, t.name_zh, t.name_en,
        COUNT(DISTINCT eligible.id) AS question_count,
        COALESCE(SUM(eligible.frequency), 0) AS total_frequency
    FROM tags t
    LEFT JOIN eligible ON eligible.tag_id = t.id
    GROUP BY t.id, t.slug, t.name_zh, t.name_en
) category;
"""
    )


def get_questions(params):
    page = int_param(params, "page", 1, 1, 100000)
    page_size = int_param(params, "page_size", 24, 1, 100)
    search = params.get("q", [""])[0].strip()
    category = params.get("category", [""])[0].strip()
    company = facet_param(params, "company")
    vendor = facet_param(params, "vendor")
    sort = params.get("sort", ["frequency"])[0]
    if len(search) > 200:
        raise ValueError("q must be 200 characters or fewer")
    if category and not re.fullmatch(r"[a-z0-9-]+", category):
        raise ValueError("invalid category")
    orders = {
        "frequency": "frequency DESC, question ASC",
        "az": "question ASC",
        "newest": "id DESC",
    }
    if sort not in orders:
        raise ValueError("sort must be frequency, az, or newest")

    search_filter = "TRUE"
    if search:
        pattern = sql_text(f"%{search}%")
        search_filter = f"""(
            q.canonical_text_en ILIKE {pattern}
            OR EXISTS (
                SELECT 1 FROM question_variants search_variant
                WHERE search_variant.question_id = q.id
                  AND search_variant.original_text ILIKE {pattern}
            )
        )"""
    category_filter = "TRUE" if not category else f"t.slug = {sql_text(category)}"
    scope_filter = scope_question_filter("q", company, vendor)
    frequency = scope_frequency("q", company, vendor)
    offset = (page - 1) * page_size
    return query_json(
        f"""
WITH filtered AS (
    SELECT q.id, q.canonical_text_en AS question,
        {frequency} AS frequency,
        COALESCE(t.slug, 'uncategorized') AS category_slug,
        COALESCE(t.name_zh, '未分类') AS category_zh,
        COALESCE(t.name_en, 'Uncategorized') AS category_en
    FROM questions q
    JOIN question_frequency qf ON qf.question_id = q.id
    LEFT JOIN question_tags qt ON qt.question_id = q.id AND qt.is_primary
    LEFT JOIN tags t ON t.id = qt.tag_id
    WHERE NOT q.needs_review AND {search_filter} AND {category_filter} AND {scope_filter}
)
SELECT json_build_object(
    'items', COALESCE((
        SELECT json_agg(row_to_json(page_rows))
        FROM (
            SELECT * FROM filtered
            ORDER BY {orders[sort]}
            LIMIT {page_size} OFFSET {offset}
        ) page_rows
    ), '[]'::json),
    'total', (SELECT COUNT(*) FROM filtered),
    'page', {page},
    'page_size', {page_size}
)::text;
"""
    )


def get_question(question_id: int, params):
    company = facet_param(params, "company")
    vendor = facet_param(params, "vendor")
    frequency = scope_frequency("q", company, vendor)
    occurrence_conditions = []
    if company:
        occurrence_conditions.append(f"LOWER(BTRIM(i.client)) = {sql_text(company)}")
    if vendor:
        occurrence_conditions.append(f"LOWER(BTRIM(i.vendor)) = {sql_text(vendor)}")
    occurrence_scope_filter = " AND ".join(occurrence_conditions) or "TRUE"
    return query_json(
        f"""
SELECT COALESCE((
    SELECT json_build_object(
        'id', q.id,
        'question', q.canonical_text_en,
        'frequency', {frequency},
        'total_mentions', qf.total_mentions,
        'category_slug', COALESCE(t.slug, 'uncategorized'),
        'category_zh', COALESCE(t.name_zh, '未分类'),
        'category_en', COALESCE(t.name_en, 'Uncategorized'),
        'occurrences', COALESCE((
            SELECT json_agg(json_build_object(
                'occurrence_id', qo.id,
                'interview_id', i.id,
                'external_id', i.external_id,
                'date', i.interview_date,
                'client', i.client,
                'vendor', i.vendor,
                'position', i.position,
                'round', i.interview_round,
                'interviewer', i.interviewer,
                'candidate', i.candidate,
                'original_text', qv.original_text,
                'language', qv.language
            ) ORDER BY i.interview_date DESC NULLS LAST, qo.id DESC)
            FROM question_variants qv
            JOIN question_occurrences qo ON qo.variant_id = qv.id
            JOIN interviews i ON i.id = qo.interview_id
            WHERE qv.question_id = q.id AND {occurrence_scope_filter}
        ), '[]'::json)
    )
    FROM questions q
    JOIN question_frequency qf ON qf.question_id = q.id
    LEFT JOIN question_tags qt ON qt.question_id = q.id AND qt.is_primary
    LEFT JOIN tags t ON t.id = qt.tag_id
    WHERE q.id = {question_id}
), 'null'::json)::text;
"""
    )


def get_interviews(params):
    page = int_param(params, "page", 1, 1, 100000)
    page_size = int_param(params, "page_size", 40, 1, 100)
    columns = {
        "date": "COALESCE(i.interview_date::text, '')",
        "company": "COALESCE(i.client, '')",
        "vendor": "COALESCE(i.vendor, '')",
        "round": "COALESCE(i.interview_round, '')",
        "type": "COALESCE(i.employment_type, '')",
        "position": "COALESCE(i.position, '')",
        "interviewer": "COALESCE(i.interviewer, '')",
        "candidate": "COALESCE(i.candidate, '')",
    }
    filters = []
    for name, expression in columns.items():
        value = text_param(params, name)
        if value:
            filters.append(f"{expression} ILIKE {sql_text(f'%{value}%')}")
    where = " AND ".join(filters) or "TRUE"
    offset = (page - 1) * page_size
    return query_json(
        f"""
WITH filtered AS (
    SELECT i.id, i.interview_date AS date, i.client AS company, i.vendor,
        i.interview_round AS round, i.employment_type AS type, i.position,
        i.interviewer, i.candidate,
        (SELECT COUNT(*) FROM question_occurrences qo WHERE qo.interview_id = i.id) AS question_count
    FROM interviews i
    WHERE {where}
)
SELECT json_build_object(
    'items', COALESCE((
        SELECT json_agg(row_to_json(page_rows))
        FROM (
            SELECT * FROM filtered
            ORDER BY date DESC NULLS LAST, id DESC
            LIMIT {page_size} OFFSET {offset}
        ) page_rows
    ), '[]'::json),
    'total', (SELECT COUNT(*) FROM filtered),
    'page', {page},
    'page_size', {page_size}
)::text;
"""
    )


def get_interview(interview_id: int):
    return query_json(
        f"""
SELECT COALESCE((
    SELECT json_build_object(
        'id', i.id,
        'external_id', i.external_id,
        'date', i.interview_date,
        'client', i.client,
        'vendor', i.vendor,
        'interviewer', i.interviewer,
        'candidate', i.candidate,
        'position', i.position,
        'round', i.interview_round,
        'employment_type', i.employment_type,
        'notes', i.notes,
        'feedback', i.feedback,
        'source_filename', s.original_filename,
        'raw_content', rr.raw_content,
        'questions', COALESCE((
            SELECT json_agg(json_build_object(
                'id', q.id,
                'sequence', qo.sequence_no,
                'question', CASE WHEN q.needs_review THEN qv.original_text ELSE q.canonical_text_en END,
                'frequency', qf.interview_frequency,
                'original_text', qv.original_text,
                'language', qv.language
            ) ORDER BY qo.sequence_no)
            FROM question_occurrences qo
            JOIN question_variants qv ON qv.id = qo.variant_id
            JOIN questions q ON q.id = qv.question_id
            JOIN question_frequency qf ON qf.question_id = q.id
            WHERE qo.interview_id = i.id
        ), '[]'::json)
    )
    FROM interviews i
    LEFT JOIN raw_records rr ON rr.id = i.raw_record_id
    LEFT JOIN sources s ON s.id = rr.source_id
    WHERE i.id = {interview_id}
), 'null'::json)::text;
"""
    )


def get_users():
    return query_json(
        """
SELECT COALESCE(json_agg(json_build_object(
    'id', id, 'email', email, 'display_name', display_name,
    'role', role, 'is_active', is_active, 'created_at', created_at
) ORDER BY created_at, id), '[]')::text
FROM app_users;
"""
    )


def update_user(user_id: int, payload, acting_user_id: int):
    updates = []
    if "role" in payload:
        role = payload["role"]
        if role not in {"admin", "user"}:
            raise ValueError("role must be admin or user")
        if user_id == acting_user_id and role != "admin":
            raise ValueError("you cannot remove your own admin role")
        updates.append(f"role = {sql_text(role)}")
    if "is_active" in payload:
        active = payload["is_active"]
        if not isinstance(active, bool):
            raise ValueError("is_active must be boolean")
        if user_id == acting_user_id and not active:
            raise ValueError("you cannot deactivate your own account")
        updates.append(f"is_active = {'TRUE' if active else 'FALSE'}")
    if payload.get("password"):
        salt, digest = password_record(payload["password"])
        updates.extend(
            [f"password_salt = {sql_text(salt)}", f"password_hash = {sql_text(digest)}"]
        )
    if "display_name" in payload:
        display_name = str(payload["display_name"]).strip()
        if len(display_name) > 120:
            raise ValueError("display_name is too long")
        updates.append(f"display_name = {sql_text(display_name)}")
    if not updates:
        raise ValueError("no supported fields to update")
    updates.append("updated_at = CURRENT_TIMESTAMP")
    return query_json(
        f"""
UPDATE app_users SET {', '.join(updates)} WHERE id = {user_id}
RETURNING json_build_object('id', id, 'email', email, 'display_name', display_name, 'role', role, 'is_active', is_active)::text;
"""
    )


def add_interview(payload, user):
    interview_date = str(payload.get("date") or "").strip()
    if interview_date:
        try:
            date.fromisoformat(interview_date)
        except ValueError as error:
            raise ValueError("date must use YYYY-MM-DD") from error
    fields = {}
    for key in (
        "client",
        "vendor",
        "interviewer",
        "candidate",
        "position",
        "round",
        "type",
        "notes",
        "feedback",
    ):
        value = str(payload.get(key) or "").strip()
        if len(value) > 5000:
            raise ValueError(f"{key} is too long")
        fields[key] = value
    if not fields["client"] or not fields["position"]:
        raise ValueError("client and position are required")
    questions = payload.get("questions", [])
    if isinstance(questions, str):
        questions = questions.splitlines()
    if not isinstance(questions, list):
        raise ValueError("questions must be a list or newline-separated text")
    questions = [str(question).strip() for question in questions if str(question).strip()]
    if not 1 <= len(questions) <= 200:
        raise ValueError("provide between 1 and 200 questions")
    if any(len(question) > 10000 for question in questions):
        raise ValueError("a question is too long")
    if any(not dedupe_key(question) for question in questions):
        raise ValueError("questions must contain letters or numbers")

    raw_payload = {
        "date": interview_date or None,
        **fields,
        "questions": questions,
        "created_by": user["email"],
    }
    raw_json = json.dumps(raw_payload, ensure_ascii=False, separators=(",", ":"))
    source_hash = hashlib.sha256(b"interview-admin-web-v1").hexdigest()
    record_key = f"manual:{secrets.token_hex(12)}"
    statements = [
        "BEGIN;",
        "SELECT pg_advisory_xact_lock(88008111);",
        "INSERT INTO sources (original_filename, source_kind, storage_path, sha256, byte_size) "
        f"VALUES ('Admin web entries', 'manual', 'database', {sql_text(source_hash)}, 0) ON CONFLICT (sha256) DO NOTHING;",
        f"SELECT id AS manual_source_id FROM sources WHERE BTRIM(sha256) = {sql_text(source_hash)} \\gset",
        "SELECT COALESCE(MAX(record_index), 0) + 1 AS next_record_index FROM raw_records WHERE source_id = :manual_source_id \\gset",
        "INSERT INTO raw_records (source_id, record_index, source_record_key, raw_content, parsed_payload, parse_status, parse_confidence) "
        f"VALUES (:manual_source_id, :next_record_index, {sql_text(record_key)}, {sql_text(raw_json)}, {sql_text(raw_json)}::jsonb, 'parsed', 1.0000) RETURNING id AS new_raw_id \\gset",
        "INSERT INTO interviews (raw_record_id, interview_date, client, vendor, interviewer, candidate, position, interview_round, employment_type, notes, feedback, source_created_by, created_by_user_id) "
        f"VALUES (:new_raw_id, {sql_nullable(interview_date)}::date, {sql_nullable(fields['client'])}, {sql_nullable(fields['vendor'])}, {sql_nullable(fields['interviewer'])}, {sql_nullable(fields['candidate'])}, {sql_nullable(fields['position'])}, {sql_nullable(fields['round'])}, {sql_nullable(fields['type'])}, {sql_nullable(fields['notes'])}, {sql_nullable(fields['feedback'])}, {sql_text(user['email'])}, {user['id']}) RETURNING id AS new_interview_id \\gset",
    ]
    for sequence, question in enumerate(questions, 1):
        language = language_of(question)
        question_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()
        canonical = question if language == "en" else f"[Translation review required: {question_hash[:12]}]"
        normalized = dedupe_key(canonical)
        translated = sql_text(question) if language == "en" else "NULL"
        needs_review = "FALSE" if language == "en" else "TRUE"
        category = classify(dedupe_key(question))
        statements.extend(
            [
                "INSERT INTO questions (canonical_text_en, normalized_key_en, needs_review) "
                f"VALUES ({sql_text(canonical)}, {sql_text(normalized)}, {needs_review}) ON CONFLICT (normalized_key_en) DO NOTHING;",
                "INSERT INTO question_variants (question_id, original_text, language, translated_text_en, normalized_text_en, text_sha256, match_method, match_confidence) "
                f"SELECT id, {sql_text(question)}, {sql_text(language)}, {translated}, {sql_nullable(dedupe_key(question))}, {sql_text(question_hash)}, 'manual', 1.0000 FROM questions WHERE normalized_key_en = {sql_text(normalized)} "
                "ON CONFLICT (question_id, language, text_sha256) DO UPDATE SET match_method = 'manual', match_confidence = 1.0000;",
                "INSERT INTO question_tags (question_id, tag_id, is_primary, assigned_by, confidence) "
                f"SELECT q.id, t.id, TRUE, 'manual', 1.0000 FROM questions q JOIN tags t ON t.name_zh = {sql_text(category)} WHERE q.normalized_key_en = {sql_text(normalized)} AND NOT EXISTS (SELECT 1 FROM question_tags current_tag WHERE current_tag.question_id = q.id AND current_tag.is_primary) ON CONFLICT DO NOTHING;",
                "INSERT INTO question_occurrences (interview_id, variant_id, source_question_key, sequence_no) "
                f"SELECT :new_interview_id, qv.id, {sql_text(f'manual:{sequence}')}, {sequence} FROM questions q JOIN question_variants qv ON qv.question_id = q.id WHERE q.normalized_key_en = {sql_text(normalized)} AND qv.language = {sql_text(language)} AND BTRIM(qv.text_sha256) = {sql_text(question_hash)};",
            ]
        )
    statements.extend(
        [
            "SELECT json_build_object('id', :new_interview_id, 'question_count', "
            f"{len(questions)})::text;",
            "COMMIT;",
        ]
    )
    output = execute_sql("\n".join(statements))
    return json.loads(output.splitlines()[-1])


class Handler(BaseHTTPRequestHandler):
    server_version = "InterviewLibrary/2.0"

    def send_json(self, payload, status=200, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        origin = self.headers.get("Origin")
        if origin == FRONTEND_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        origin = self.headers.get("Origin")
        if origin == FRONTEND_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CSRF-Token")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
        self.end_headers()

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid content length") from error
        if length < 2 or length > 1_000_000:
            raise ValueError("request body must be JSON under 1 MB")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("content type must be application/json")
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        return cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else ""

    def current_user(self):
        return session_from_token(self.session_token())

    def require_user(self, admin=False, csrf=False):
        user = self.current_user()
        if not user:
            self.send_json({"error": "authentication required"}, 401)
            return None
        if admin and user["role"] != "admin":
            self.send_json({"error": "admin permission required"}, 403)
            return None
        if csrf and not hmac.compare_digest(
            self.headers.get("X-CSRF-Token", ""), user["csrf_token"]
        ):
            self.send_json({"error": "invalid CSRF token"}, 403)
            return None
        return user

    @staticmethod
    def public_user(user):
        return {
            "id": user["id"],
            "email": user["email"],
            "display_name": user.get("display_name") or "",
            "role": user["role"],
            "csrf_token": user["csrf_token"],
        }

    def login(self):
        payload = self.read_json()
        email = normalize_email(str(payload.get("email") or ""))
        password = payload.get("password")
        key = f"{self.client_address[0]}:{email}"
        if not login_allowed(key):
            self.send_json({"error": "too many login attempts; try again in 5 minutes"}, 429)
            return
        if not isinstance(password, str) or not 1 <= len(password) <= 256:
            record_login_failure(key)
            self.send_json({"error": "invalid email or password"}, 401)
            return
        user = query_json(
            f"""
SELECT COALESCE((
    SELECT json_build_object(
        'id', id, 'email', email, 'display_name', display_name, 'role', role,
        'password_salt', password_salt, 'password_hash', BTRIM(password_hash)
    ) FROM app_users
    WHERE LOWER(BTRIM(email)) = {sql_text(email)} AND is_active
), 'null'::json)::text;
"""
        )
        salt = user["password_salt"] if user else "00" * 16
        valid = hmac.compare_digest(
            password_digest(password, salt), user["password_hash"] if user else "0" * 64
        )
        if not user or not valid:
            record_login_failure(key)
            self.send_json({"error": "invalid email or password"}, 401)
            return
        LOGIN_ATTEMPTS.pop(key, None)
        token, csrf = issue_session(user["id"])
        user["csrf_token"] = csrf
        secure = "; Secure" if COOKIE_SECURE else ""
        cookie = (
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite={COOKIE_SAMESITE}; "
            f"Max-Age={SESSION_SECONDS}{secure}"
        )
        self.send_json({"user": self.public_user(user)}, headers={"Set-Cookie": cookie})

    def logout(self, user):
        execute_sql(
            f"DELETE FROM app_sessions WHERE BTRIM(token_hash) = {sql_text(user['token_hash'])};"
        )
        secure = "; Secure" if COOKIE_SECURE else ""
        self.send_json(
            {"ok": True},
            headers={
                "Set-Cookie": f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite={COOKIE_SAMESITE}; Max-Age=0{secure}"
            },
        )

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/api"}:
                self.send_json(
                    {
                        "name": "Interview Library API",
                        "endpoints": [
                            "/api/health",
                            "/api/auth/me",
                            "/api/stats",
                            "/api/categories",
                            "/api/companies",
                            "/api/vendors",
                            "/api/questions",
                            "/api/questions/{id}",
                            "/api/interviews",
                            "/api/interviews/{id}",
                        ]
                    }
                )
                return
            if parsed.path == "/api/health":
                self.send_json({"status": "ok"})
                return
            user = self.require_user(admin=parsed.path == "/api/admin/users")
            if not user:
                return
            if parsed.path == "/api/auth/me":
                self.send_json({"user": self.public_user(user)})
                return
            if parsed.path == "/api/admin/users":
                self.send_json(get_users())
                return
            if parsed.path == "/api/stats":
                self.send_json(get_stats())
                return
            if parsed.path == "/api/categories":
                self.send_json(get_categories(parse_qs(parsed.query)))
                return
            if parsed.path == "/api/companies":
                self.send_json(get_companies())
                return
            if parsed.path == "/api/vendors":
                self.send_json(get_vendors())
                return
            if parsed.path == "/api/questions":
                self.send_json(get_questions(parse_qs(parsed.query)))
                return
            if parsed.path == "/api/interviews":
                self.send_json(get_interviews(parse_qs(parsed.query)))
                return
            match = re.fullmatch(r"/api/questions/(\d+)", parsed.path)
            if match:
                payload = get_question(int(match.group(1)), parse_qs(parsed.query))
                self.send_json(payload if payload else {"error": "question not found"}, 200 if payload else 404)
                return
            match = re.fullmatch(r"/api/interviews/(\d+)", parsed.path)
            if match:
                payload = get_interview(int(match.group(1)))
                self.send_json(payload if payload else {"error": "interview not found"}, 200 if payload else 404)
                return
            if parsed.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            self.send_json({"error": "not found"}, 404)
        except ValueError as error:
            self.send_json({"error": str(error)}, 400)
        except Exception as error:  # API boundary: never leak database details.
            print(f"request failed: {error}", file=sys.stderr)
            self.send_json({"error": "internal server error"}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/auth/login":
                self.login()
                return
            user = self.require_user(
                admin=parsed.path in {"/api/admin/users", "/api/interviews"}, csrf=True
            )
            if not user:
                return
            if parsed.path == "/api/auth/logout":
                self.logout(user)
                return
            if parsed.path == "/api/admin/users":
                payload = self.read_json()
                created = create_user(
                    str(payload.get("email") or ""),
                    payload.get("password") if isinstance(payload.get("password"), str) else "",
                    str(payload.get("role") or "user"),
                    str(payload.get("display_name") or "").strip(),
                )
                self.send_json(created, 201)
                return
            if parsed.path == "/api/interviews":
                self.send_json(add_interview(self.read_json(), user), 201)
                return
            self.send_json({"error": "not found"}, 404)
        except ValueError as error:
            self.send_json({"error": str(error)}, 400)
        except Exception as error:
            print(f"request failed: {error}", file=sys.stderr)
            self.send_json({"error": "internal server error"}, 500)

    def do_PATCH(self):
        parsed = urlparse(self.path)
        try:
            match = re.fullmatch(r"/api/admin/users/(\d+)", parsed.path)
            user = self.require_user(admin=True, csrf=True)
            if not user:
                return
            if not match:
                self.send_json({"error": "not found"}, 404)
                return
            updated = update_user(int(match.group(1)), self.read_json(), user["id"])
            self.send_json(updated if updated else {"error": "user not found"}, 200 if updated else 404)
        except ValueError as error:
            self.send_json({"error": str(error)}, 400)
        except Exception as error:
            print(f"request failed: {error}", file=sys.stderr)
            self.send_json({"error": "internal server error"}, 500)

    def log_message(self, message, *args):
        print(f"{self.address_string()} - {message % args}")


def self_test():
    assert sql_text("O'Brien") == "'O''Brien'"
    assert int_param({"page": ["3"]}, "page", 1, 1, 10) == 3
    assert facet_param({"company": [" Walmart "]}, "company") == "walmart"
    assert facet_param({"vendor": [" RandStad "]}, "vendor") == "randstad"
    try:
        int_param({"page": ["0"]}, "page", 1, 1, 10)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid page was accepted")
    print("self-test passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8011")))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--create-admin", metavar="EMAIL")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    apply_auth_schema()
    if args.create_admin:
        password = getpass.getpass("Admin password: ")
        created = create_user(args.create_admin, password, "admin", "Admin", replace=True)
        print(json.dumps(created, ensure_ascii=False))
        return
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Interview Library API: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
