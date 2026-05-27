"""Cold tier — append-only Parquet+zstd shards partitioned by month and channel.

Layout (Hive-style, DuckDB-friendly):

    SLACK_BRIDGE_COLD_ARCHIVE_DIR/
        year_month=2025-10/
            channel_id=C0123456789/
                shard-00000001.parquet
                shard-00000002.parquet
            channel_id=D09JZSTLC9J/
                shard-00000001.parquet
        year_month=2025-11/...

Why append-only shards (instead of one file per partition that gets rewritten):
- Nightly compaction never touches old partitions. New rows = new shard file.
- A separate `merge_shards` job consolidates a partition's shards into one
  canonical file when there are too many or it's "old enough". That job is
  cheap to run weekly because it only runs on partitions that actually
  changed in the last cycle.

Schema (v2):
    msg_id INT64                  — stable id from hot SQLite, joins to vec DB
    channel_id TEXT
    ts TEXT                       — Slack ts, e.g. '1714382100.123456'
    edit_seq INT64                — 0=original, 1+=edit version
    user TEXT
    user_label TEXT
    text TEXT
    thread_ts TEXT (nullable)
    subtype TEXT (nullable)
    raw_json TEXT
    recorded_at INT64
    deleted_at INT64 (nullable)   — soft-delete tombstone

Compression: zstd level 19 (max). On JSON-heavy text, ~10-15× ratio.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from pathlib import Path
from typing import Any

from ..config import settings

COLD_ROOT = settings().cold_archive_dir

# v2 cold schema. Order matters — kept stable across writes.
_FIELDS = [
    ("msg_id", "int64"),
    ("channel_id", "string"),
    ("ts", "string"),
    ("edit_seq", "int64"),
    ("user", "string"),
    ("user_label", "string"),
    ("text", "string"),
    ("thread_ts", "string"),
    ("subtype", "string"),
    ("raw_json", "string"),
    ("recorded_at", "int64"),
    ("deleted_at", "int64"),
]

_SHARD_RE = re.compile(r"shard-(\d{8})\.parquet$")

log = logging.getLogger("slack-archive-cold")

_duckdb_conn: Any | None = None


def _ensure_root() -> None:
    COLD_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)


def _partition_dir(year_month: str, channel_id: str) -> Path:
    return COLD_ROOT / f"year_month={year_month}" / f"channel_id={channel_id}"


def _next_shard_path(year_month: str, channel_id: str) -> Path:
    """Find the next shard number for this partition."""
    pdir = _partition_dir(year_month, channel_id)
    pdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    nums = []
    for f in pdir.glob("shard-*.parquet"):
        m = _SHARD_RE.search(f.name)
        if m:
            nums.append(int(m.group(1)))
    next_n = (max(nums) + 1) if nums else 1
    return pdir / f"shard-{next_n:08d}.parquet"


def _build_table(rows: list[dict[str, Any]]) -> Any:
    import pyarrow as pa

    columns: dict[str, list[Any]] = {name: [] for name, _ in _FIELDS}
    for r in rows:
        for name, _ in _FIELDS:
            columns[name].append(r.get(name))
    schema = pa.schema(
        [(name, pa.string() if t == "string" else pa.int64()) for name, t in _FIELDS]
    )
    return pa.table(columns, schema=schema)


def append_shard(
    year_month: str,
    channel_id: str,
    rows: list[dict[str, Any]],
) -> tuple[Path, int]:
    """Write rows as a brand-new shard file under the (year_month, channel_id)
    partition. Returns (path, rows_written). Atomic via .tmp + rename."""
    import pyarrow.parquet as pq

    if not rows:
        return _partition_dir(year_month, channel_id), 0

    _ensure_root()
    path = _next_shard_path(year_month, channel_id)
    tbl = _build_table(rows)

    tmp = path.with_suffix(".tmp")
    pq.write_table(
        tbl,
        str(tmp),
        compression="zstd",
        compression_level=19,
        version="2.6",
        use_dictionary=True,
    )
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return path, len(rows)


def merge_shards(year_month: str, channel_id: str) -> tuple[Path | None, int, int]:
    """Consolidate all shards in a partition into a single canonical
    `shard-00000001.parquet`. Idempotent — running on a 1-shard partition
    is a no-op. Returns (final_path, shards_merged, rows). Dedups on
    (msg_id) keeping the highest edit_seq."""
    import pyarrow.parquet as pq

    pdir = _partition_dir(year_month, channel_id)
    if not pdir.exists():
        return None, 0, 0
    shards = sorted(pdir.glob("shard-*.parquet"))
    if len(shards) <= 1:
        return (shards[0] if shards else None), 0, 0

    tables = [pq.read_table(str(s)) for s in shards]
    import pyarrow as pa

    merged = pa.concat_tables(tables, promote_options="default")

    # Dedup on msg_id, keeping latest edit_seq, then sort by ts
    df = merged.to_pandas()
    df = df.sort_values(["msg_id", "edit_seq"]).drop_duplicates(subset=["msg_id"], keep="last")
    df = df.sort_values("ts")
    schema = pa.schema(
        [(name, pa.string() if t == "string" else pa.int64()) for name, t in _FIELDS]
    )
    final_table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)

    final = pdir / "shard-00000001.parquet"
    tmp = final.with_suffix(".tmp")
    pq.write_table(
        final_table,
        str(tmp),
        compression="zstd",
        compression_level=19,
        version="2.6",
        use_dictionary=True,
    )
    # Remove old shards (excluding final_path), then rename tmp into place
    for s in shards:
        if s != final:
            s.unlink()
    os.replace(tmp, final)
    with contextlib.suppress(OSError):
        os.chmod(final, 0o600)
    return final, len(shards), final_table.num_rows


def _globs() -> list[str]:
    """Return the list of parquet globs that actually have matching files.
    DuckDB errors out if a glob in the list has zero matches, so we resolve
    them in Python first.
    Legacy:    year_month=YYYY-MM/channel_id=CXXX.parquet     (one file per partition)
    Sharded:   year_month=YYYY-MM/channel_id=CXXX/shard-N.parquet (append-only shards)"""
    candidates = [
        COLD_ROOT / "year_month=*" / "channel_id=*.parquet",
        COLD_ROOT / "year_month=*" / "channel_id=*" / "*.parquet",
    ]
    return [str(g) for g in candidates if list(COLD_ROOT.glob(str(g.relative_to(COLD_ROOT))))]


def get_duckdb() -> Any:
    """Lazy DuckDB connection used for cold reads. Reads zstd Parquet via the
    partition globs with hive_partitioning=true."""
    global _duckdb_conn
    if _duckdb_conn is None:
        import duckdb

        _duckdb_conn = duckdb.connect(":memory:")
    return _duckdb_conn


def query_via_duckdb(
    where: str = "1=1",
    params: tuple = (),
    columns: str = "*",
    order_by: str = "ts ASC",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Run a SQL filter against the entire cold tier."""
    if not COLD_ROOT.exists():
        return []
    globs = _globs()
    if not globs:
        return []
    conn = get_duckdb()
    glob_list = ", ".join(f"'{g}'" for g in globs)
    sql = (
        f"SELECT {columns} FROM read_parquet([{glob_list}], "
        f"hive_partitioning=true, union_by_name=true) WHERE {where} ORDER BY {order_by}"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rs = conn.execute(sql, list(params))
    cols = [d[0] for d in rs.description]
    return [dict(zip(cols, row, strict=True)) for row in rs.fetchall()]


def horizon_oldest_ts() -> str | None:
    rows = query_via_duckdb(columns="MIN(ts) AS m", order_by="1", limit=1)
    if not rows or rows[0].get("m") is None:
        return None
    return str(rows[0]["m"])


def stats() -> dict[str, Any]:
    """Per-month and total cold-tier sizes + shard counts."""
    if not COLD_ROOT.exists():
        return {"months": [], "total_files": 0, "total_size_bytes": 0}
    months: list[dict[str, Any]] = []
    total_files = 0
    total_size = 0
    for ym_dir in sorted(COLD_ROOT.glob("year_month=*")):
        files = list(ym_dir.glob("channel_id=*/*.parquet")) + list(
            ym_dir.glob("channel_id=*.parquet")  # legacy single-file layout
        )
        size = sum(f.stat().st_size for f in files)
        months.append(
            {
                "year_month": ym_dir.name.split("=", 1)[1],
                "files": len(files),
                "size_bytes": size,
            }
        )
        total_files += len(files)
        total_size += size
    return {
        "root": str(COLD_ROOT),
        "months": months,
        "total_files": total_files,
        "total_size_bytes": total_size,
    }
