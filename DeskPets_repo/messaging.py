# messaging.py
import json
import os
import time
import uuid
from pathlib import Path
from typing import List, Dict, Any


def now_ts() -> float:
    return time.time()


def atomic_append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def overwrite_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def inbox_path(shared_dir: Path, user_id: str) -> Path:
    return shared_dir / f"inbox_{user_id}.jsonl"


def send_message(shared_dir: Path, sender: str, receiver: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    path = inbox_path(shared_dir, receiver)
    atomic_append_jsonl(
        path,
        {
            "msg_id": str(uuid.uuid4()),
            "sender": sender,
            "receiver": receiver,
            "created_at": now_ts(),
            "text": text,
            "delivered": False,
        },
    )


def fetch_undelivered(shared_dir: Path, user_id: str) -> List[Dict[str, Any]]:
    """
    Read inbox_<user>.jsonl, mark undelivered as delivered, and return them.
    """
    path = inbox_path(shared_dir, user_id)
    rows = read_jsonl(path)
    if not rows:
        return []

    out = []
    changed = False
    t = now_ts()

    for r in rows:
        if r.get("delivered") is True:
            continue
        r["delivered"] = True
        r["delivered_at"] = t
        changed = True

        text = (r.get("text", "") or "").strip()
        if text:
            out.append(r)

    if changed:
        overwrite_jsonl(path, rows)

    return out
