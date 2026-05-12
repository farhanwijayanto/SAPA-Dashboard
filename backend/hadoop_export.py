import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from bson import ObjectId

from . import mongo_db


def _utc_day_bounds(day: datetime) -> tuple[datetime, datetime]:
    if day.tzinfo is None:
        day = day.replace(tzinfo=timezone.utc)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end


def _json_default(obj: Any):
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
        return obj.isoformat().replace("+00:00", "Z")
    if isinstance(obj, ObjectId):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _to_jsonl(docs: Iterable[dict]) -> bytes:
    lines: list[str] = []
    for doc in docs:
        doc = dict(doc)
        doc.pop("_id", None)
        lines.append(json.dumps(doc, ensure_ascii=False, default=_json_default))
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def _webhdfs_create_file(webhdfs_base_url: str, hdfs_path: str, data: bytes, hadoop_user: str) -> None:
    base = webhdfs_base_url.rstrip("/")
    if not base.endswith("/webhdfs/v1"):
        base = base + "/webhdfs/v1"

    hdfs_path = "/" + hdfs_path.lstrip("/")
    create_url = (
        f"{base}{urllib.parse.quote(hdfs_path)}"
        f"?op=CREATE&overwrite=true&user.name={urllib.parse.quote(hadoop_user)}"
    )

    req = urllib.request.Request(create_url, method="PUT")
    try:
        urllib.request.urlopen(req, timeout=15)
        return
    except urllib.error.HTTPError as e:
        if e.code not in (307, 308):
            raise
        location = e.headers.get("Location")
        if not location:
            raise RuntimeError("WebHDFS redirect missing Location header")
        put_req = urllib.request.Request(location, data=data, method="PUT")
        put_req.add_header("Content-Type", "application/octet-stream")
        urllib.request.urlopen(put_req, timeout=30)


def export_collection_day(
    *,
    collection_name: str,
    hdfs_subdir: str,
    day: datetime,
    webhdfs_base_url: str,
    hadoop_user: str,
    hdfs_base_path: str,
    dry_run: bool,
    limit: int,
) -> dict[str, Any]:
    start, end = _utc_day_bounds(day)
    year, month, day_num = start.year, start.month, start.day

    if collection_name == "attendance":
        coll = mongo_db.get_logs_collection()
    elif collection_name == "audit":
        coll = mongo_db.get_audit_collection()
    else:
        raise ValueError("collection_name must be 'attendance' or 'audit'")

    docs = list(
        coll.find({"timestamp": {"$gte": start, "$lt": end}}).sort("timestamp", 1).limit(limit)
    )
    payload = _to_jsonl(docs)

    hdfs_target = (
        f"{hdfs_base_path.rstrip('/')}/{hdfs_subdir}"
        f"/year={year:04d}/month={month:02d}/day={day_num:02d}/{collection_name}.jsonl"
    )

    if dry_run:
        preview = payload[:500].decode("utf-8", errors="replace")
        return {
            "collection": collection_name,
            "count": len(docs),
            "hdfs_path": hdfs_target,
            "bytes": len(payload),
            "preview": preview,
        }

    _webhdfs_create_file(webhdfs_base_url, hdfs_target, payload, hadoop_user)
    return {"collection": collection_name, "count": len(docs), "hdfs_path": hdfs_target, "bytes": len(payload)}


def _parse_date(date_str: str) -> datetime:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.replace(tzinfo=timezone.utc)


def main():
    parser = argparse.ArgumentParser(description="Export SAPA logs from MongoDB to HDFS via WebHDFS (JSONL).")
    parser.add_argument("--date", required=True, help="UTC date to export (YYYY-MM-DD)")
    parser.add_argument("--audit", action="store_true", help="Export audit logs too")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to HDFS; print summary/preview")
    parser.add_argument("--limit", type=int, default=200000, help="Max documents exported per collection")

    args = parser.parse_args()

    webhdfs_base_url = os.getenv("WEBHDFS_URL")
    hadoop_user = os.getenv("HADOOP_USER", "hdfs")
    hdfs_base_path = os.getenv("HDFS_BASE_PATH", "/data/sapa")

    if not webhdfs_base_url and not args.dry_run:
        raise SystemExit("WEBHDFS_URL belum di-set. Contoh: http://namenode:9870/webhdfs/v1")

    day = _parse_date(args.date)

    results: list[dict[str, Any]] = []
    results.append(
        export_collection_day(
            collection_name="attendance",
            hdfs_subdir="attendance",
            day=day,
            webhdfs_base_url=webhdfs_base_url or "",
            hadoop_user=hadoop_user,
            hdfs_base_path=hdfs_base_path,
            dry_run=args.dry_run,
            limit=args.limit,
        )
    )
    if args.audit:
        results.append(
            export_collection_day(
                collection_name="audit",
                hdfs_subdir="audit",
                day=day,
                webhdfs_base_url=webhdfs_base_url or "",
                hadoop_user=hadoop_user,
                hdfs_base_path=hdfs_base_path,
                dry_run=args.dry_run,
                limit=args.limit,
            )
        )

    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
