from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _required(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing {key}")
    return value.strip()


def _url(raw: dict[str, Any], key: str) -> str:
    value = _required(raw, key)
    if urlparse(value).scheme not in {"http", "https"}:
        raise ValueError(f"invalid {key}: expected http(s) URL")
    return value


def normalize(raw: dict[str, Any], source_file: Path) -> dict[str, Any]:
    platform = _required(raw, "platform")
    try:
        source_name = str(source_file.resolve().relative_to(Path(__file__).resolve().parent.parent))
    except ValueError:
        source_name = str(source_file)
    metadata: dict[str, Any] = {"source_file": source_name}
    segments: list[dict[str, Any]] = []

    if platform == "xiaohongshu":
        source_id = _required(raw, "note_id")
        content_type = "post"
        url = _url(raw, "url")
        title = _required(raw, "title")
        content = _required(raw, "body")
        author = {"id": raw.get("author_id"), "name": raw.get("author_name")}
        metadata.update({"hashtags": raw.get("hashtags", []), "media": raw.get("media")})
    elif platform == "x":
        source_id = _required(raw, "post_id")
        content_type = "post"
        url = _url(raw, "url")
        title = raw.get("title") or f"@{_required(raw, 'author_handle')} 的帖子"
        content = _required(raw, "content")
        author = {"id": raw.get("author_handle"), "name": raw.get("author_name")}
        metadata.update({"text_kind": raw.get("text_kind", "original"), "observed_via": raw.get("observed_via")})
    elif platform == "bilibili":
        source_id = _required(raw, "bvid")
        content_type = "video"
        url = _url(raw, "url")
        title = raw.get("title") or source_id
        content = raw.get("transcript") or ""
        author = {"id": raw.get("author_id"), "name": raw.get("author_name")}
        for item in raw.get("segments", []):
            segments.append({"start": item.get("from"), "end": item.get("to"), "text": item.get("content", "")})
        metadata.update({"cid": raw.get("cid"), "language": raw.get("language"), "source_type": raw.get("source_type")})
    elif platform == "github":
        source_id = _required(raw, "full_name")
        content_type = "repository"
        url = _url(raw, "html_url")
        title = source_id
        content = raw.get("description") or ""
        author = {"id": source_id.split("/", 1)[0], "name": None}
        metadata.update({"is_sample": bool(raw.get("is_sample", False)), "language": raw.get("language")})
    else:
        raise ValueError(f"unsupported platform: {platform}")

    status = raw.get("status", "complete")
    if status not in {"complete", "partial", "sample"}:
        raise ValueError(f"unsupported status: {status}")
    if not content and status == "complete":
        raise ValueError("complete record has no content")
    return {
        "platform": platform,
        "content_type": content_type,
        "source_id": source_id,
        "url": url,
        "title": title,
        "author": author,
        "published_at": raw.get("published_at"),
        "collected_at": raw.get("collected_at") or raw.get("captured_at"),
        "content": content,
        "content_segments": segments,
        "status": status,
        "metadata": metadata,
    }


def collect(paths: list[Path]) -> list[dict[str, Any]]:
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: expected JSON object")
        try:
            record = normalize(raw, path)
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
        key = (record["platform"], record["source_id"])
        if key in records:
            previous = {**records[key], "metadata": {k: v for k, v in records[key]["metadata"].items() if k != "source_file"}}
            current = {**record, "metadata": {k: v for k, v in record["metadata"].items() if k != "source_file"}}
            if previous != current:
                raise ValueError(f"conflicting duplicate: {key} ({path})")
            continue
        records[key] = record
    return sorted(records.values(), key=lambda item: (item["platform"], item["source_id"]))


def digest(records: list[dict[str, Any]]) -> str:
    lines = ["# SourceFlow 内容清单", "", f"共 {len(records)} 条。本清单摘录已有内容，不做事实核验或 LLM 总结。", ""]
    for item in records:
        label = {"complete": "完整样本", "partial": "不完整", "sample": "演示数据"}[item["status"]]
        if item["metadata"].get("text_kind") == "paraphrase":
            label += "、中文转述"
        date = item["published_at"] or "发布时间未核实"
        summary = " ".join(item["content"].split())[:120] or "正文未保存"
        lines.extend([
            f"## {item['title']}",
            "",
            f"来源：{item['platform']} · {label} · {date}",
            "",
            f"{summary}{'…' if len(' '.join(item['content'].split())) > 120 else ''}",
            "",
            f"[查看原文]({item['url']})",
            "",
        ])
    return "\n".join(lines)
