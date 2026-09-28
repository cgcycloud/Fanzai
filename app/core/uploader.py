"""事件云端上传（可选）—— MINDFUL_UPLOAD_URL 配置后可把本地事件同步到云端。"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

import requests

from .db import HealthStore

logger = logging.getLogger(__name__)


def upload_pending(store: HealthStore, upload_url: str | None = None, limit: int = 20) -> Dict[str, Any]:
    url = upload_url or os.environ.get("MINDFUL_UPLOAD_URL")
    if not url:
        return {"ok": False, "reason": "未配置 MINDFUL_UPLOAD_URL，跳过上传"}
    uploaded, failed = 0, 0
    for row in store.pending_events(limit=limit):
        payload = {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "type": row["type"],
            "data": json.loads(row["data_json"]),
        }
        try:
            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            store.mark_uploaded(row["id"])
            uploaded += 1
        except Exception as exc:
            store.mark_failed(row["id"], str(exc))
            failed += 1
            logger.warning("事件上传失败 id=%s: %s", row["id"], exc)
    return {"ok": True, "uploaded": uploaded, "failed": failed}
