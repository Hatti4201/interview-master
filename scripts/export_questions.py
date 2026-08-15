#!/usr/bin/env python3
"""Export the current canonical question library to Markdown and plain text."""

import json
import os
import subprocess
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.environ.get(
    "INTERVIEW_MASTER_DATABASE_URL",
    "postgresql://interview_master:local_interview_only@127.0.0.1:54329/interview_master",
)


def query(sql: str):
    result = subprocess.run(
        ["psql", DATABASE_URL, "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout.strip())


stats = query(
    """
SELECT json_build_object(
    'interviews', (SELECT COUNT(*) FROM interviews),
    'questions', (SELECT COUNT(*) FROM questions),
    'occurrences', (SELECT COUNT(*) FROM question_occurrences),
    'needs_review', (SELECT COUNT(*) FROM questions WHERE needs_review)
)::text;
"""
)

questions = query(
    """
SELECT COALESCE(json_agg(row_to_json(result)), '[]')::text
FROM (
    SELECT
        q.id,
        q.canonical_text_en AS question,
        q.needs_review,
        qf.interview_frequency,
        qf.total_mentions,
        COALESCE(t.name_zh, '未分类') AS category
    FROM questions q
    JOIN question_frequency qf ON qf.question_id = q.id
    LEFT JOIN question_tags qt ON qt.question_id = q.id AND qt.is_primary
    LEFT JOIN tags t ON t.id = qt.tag_id
    ORDER BY COALESCE(t.name_zh, '未分类'), qf.interview_frequency DESC, q.canonical_text_en
) result;
"""
)

remaining_review = query(
    """
SELECT COALESCE(json_agg(row_to_json(result)), '[]')::text
FROM (
    SELECT
        q.id,
        q.canonical_text_en,
        qf.interview_frequency,
        string_agg(DISTINCT qv.original_text, E'\n---\n') AS original_text
    FROM questions q
    JOIN question_frequency qf ON qf.question_id = q.id
    JOIN question_variants qv ON qv.question_id = q.id
    WHERE q.needs_review
    GROUP BY q.id, q.canonical_text_en, qf.interview_frequency
    ORDER BY qf.interview_frequency DESC, q.id
) result;
"""
)

grouped = defaultdict(list)
for question in questions:
    grouped[question["category"]].append(question)

markdown = [
    "# 面试问题母版：去重、频次与分类",
    "",
    f"- 面试记录：{stats['interviews']} 场",
    f"- 英文标准问题：{stats['questions']} 道",
    f"- 问题出现关系：{stats['occurrences']} 条",
    f"- 待翻译或人工审核：{stats['needs_review']} 道",
    "- 频次口径：COUNT(DISTINCT interview_id)",
    "",
]
plain = [
    "面试问题母版：去重、频次与分类",
    f"面试记录：{stats['interviews']} 场",
    f"英文标准问题：{stats['questions']} 道",
    f"问题出现关系：{stats['occurrences']} 条",
    f"待翻译或人工审核：{stats['needs_review']} 道",
    "",
]

for category, items in grouped.items():
    markdown.extend([f"## {category}", ""])
    plain.extend([f"【{category}】", ""])
    for index, item in enumerate(items, 1):
        question = " ".join(item["question"].split())
        review = "；待审核" if item["needs_review"] else ""
        markdown.append(
            f"{index}. {question} — 频次：{item['interview_frequency']}{review}"
        )
        plain.append(
            f"{index}. {question} [频次: {item['interview_frequency']}{review}]"
        )
    markdown.append("")
    plain.append("")

(ROOT / "exports" / "questions.md").write_text("\n".join(markdown), encoding="utf-8")
(ROOT / "exports" / "questions.txt").write_text("\n".join(plain), encoding="utf-8")
(ROOT / "exports" / "translation_review_remaining.md").write_text(
    "\n".join(
        [
            "# 已翻译、仍需拆分的问题",
            "",
            f"共 {len(remaining_review)} 道。这些记录已经有英文标准文本，但原始记录包含多个独立问题。",
            "",
            *[
                f"## Q{item['id']} — {item['canonical_text_en']}\n\n"
                f"- 出现场次：{item['interview_frequency']}\n"
                f"- 原文：{item['original_text'].replace(chr(10), ' ')}\n"
                for item in remaining_review
            ],
        ]
    ),
    encoding="utf-8",
)
print(json.dumps(stats, ensure_ascii=False))
