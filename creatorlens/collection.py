"""Mixed-media collection; provider injection permits network-free regression tests."""
import math
from pathlib import Path

from .storage import StopCapture, digest, ensure, file_hash
from .tasks import ID, now


def timestamp(value):
    ensure(type(value) in (int, float) and math.isfinite(value) and value > 0, "publication_time_unknown")
    return int(value)


def page_data(value, list_key):
    ensure(isinstance(value, dict) and value.get("success") is True, "api_response_unsuccessful")
    data = value.get("data")
    ensure(isinstance(data, dict), "api_data_missing")
    items = data.get(list_key)
    if items is None and data.get("has_more") is False:
        items = []
    ensure(isinstance(items, list), "api_items_missing")
    return data, items


def exact_count(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def normalize_comments(task, note_id, page, extras, displayed_total):
    data, roots = page_data(page, "comments")
    rows, seen, incomplete = [], set(), []
    by_root = {}
    for root_id, extra in extras:
        by_root.setdefault(root_id, []).append(extra)

    def append(comment, root_id=None):
        cid = comment.get("id")
        ensure(isinstance(cid, str) and cid, "comment_id_missing")
        ensure(comment.get("note_id", note_id) == note_id, "comment_work_mismatch")
        if cid in seen:
            return
        seen.add(cid)
        user = comment.get("user_info") or {}
        parent = (comment.get("target_comment") or {}).get("id")
        content = comment.get("content")
        rows.append({"key": f"xiaohongshu:{task.config['batch_id']}:{note_id}:{cid}",
                     "id": cid, "author_id": user.get("user_id"), "author_name": user.get("nickname"),
                     "is_work_author": user.get("user_id") == task.config["account_id"],
                     "text": content if isinstance(content, str) and content else None,
                     "text_missing": not bool(content), "image_count": len(comment.get("pictures") or []),
                     "created_ms": comment.get("create_time"), "likes_raw": comment.get("like_count"),
                     "root_id": root_id, "parent_id": parent,
                     "relationship": "top_level" if root_id is None else ("explicit_parent" if parent else "thread_known_parent_unknown")})

    for root in roots:
        append(root)
        rid = root["id"]
        for child in root.get("sub_comments") or []:
            append(child, rid)
        more = root.get("sub_comment_has_more")
        for extra in by_root.get(rid, []):
            subdata, children = page_data(extra, "comments")
            for child in children:
                append(child, rid)
            more = subdata.get("has_more")
        if more is not False:
            incomplete.append({"root_id": rid, "has_more": more, "reason": "reply_coverage_limited_or_unknown"})
    total = exact_count(displayed_total)
    return {"schema_version": 1, "work_id": note_id, "rows": rows,
            "coverage": {"kind": "limited_sample", "saved_count": len(rows), "total": total,
                         "total_raw": displayed_total, "gap": None if total is None else total - len(rows),
                         "top_level_reached_end": data.get("has_more") is False,
                         "top_level_has_more": data.get("has_more"), "incomplete_threads": incomplete,
                         "full_comment_section_claimed": False}}


def asset(task, provider, url, stem, kind):
    record_name = stem + ".json"
    record = task.get(record_name)
    if record:
        path = task.p(record["path"])
        ensure(path.is_file() and file_hash(path) == record["sha256"], "media_evidence_changed")
        return record
    base = task.p(stem)
    base.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A successful rename may precede its manifest write if the process was interrupted.
    extensions = (".mp4",) if kind == "video" else (".jpg", ".png", ".webp", ".avif", ".gif")
    recovered = [base.with_suffix(ext) for ext in extensions if base.with_suffix(ext).exists()]
    ensure(len(recovered) <= 1, "ambiguous_media_recovery")
    download = base.with_suffix(".download")
    if recovered:
        path = recovered[0]
        ensure(not path.is_symlink(), "symlink_not_allowed")
        info = provider.inspect(path, kind)
    else:
        ensure(not download.is_symlink(), "symlink_not_allowed")
        if not download.exists():
            provider.download(url, download)
        info = provider.inspect(download, kind)
        ensure(info.get("extension") in extensions, "unsupported_media_format")
        path = base.with_suffix(info["extension"])
        ensure(not path.exists() and not path.is_symlink(), "media_destination_exists")
        download.replace(path)
    record = {**info, "path": str(path.relative_to(task.root)), "sha256": file_hash(path),
              "bytes": path.stat().st_size, "kind": kind}
    task.save(record_name, record)
    return record


def verify(task):
    result = task.get("capture.json")
    ensure(result and result.get("task_hash") == task.fingerprint, "capture_not_complete_or_task_changed")
    unsigned = {k: v for k, v in result.items() if k != "manifest_hash"}
    ensure(result.get("manifest_hash") == digest(unsigned), "capture_manifest_changed")
    ensure(result.get("list_exhausted") is True, "selection_range_unresolved")
    ensure(len(result["selected"]) <= task.config["target_count"], "invalid_selected_count")
    ensure(len({w["id"] for w in result["selected"]}) == len(result["selected"]), "duplicate_selected_work")
    ensure(result["shortfall"] == task.config["target_count"] - len(result["selected"]), "shortfall_mismatch")
    for evidence in result["evidence"]:
        path = task.p(evidence["path"])
        ensure(path.is_file() and file_hash(path) == evidence["sha256"], "capture_evidence_changed")
    for item in result["selected"]:
        work = task.get(f"works/{item['id']}/work.json")
        ensure(work and work["id"] == item["id"] and work["type"] == item["type"]
               and work["type"] in ("video", "image")
               and work["author_id"] == task.config["account_id"], "work_manifest_invalid")
        if work["type"] == "video":
            duration = work["video"]["duration_seconds"]
            ensure(type(duration) in (int, float) and 0 < duration <= 1200, "video_duration_invalid")
        else:
            ensure(len(work["images"]) == work["expected_images"] > 0, "image_count_mismatch")
        for media in work["assets"]:
            path = task.p(media["path"])
            ensure(path.is_file() and file_hash(path) == media["sha256"], "media_evidence_changed")
    return {"collection_verified": True, "selected": len(result["selected"]), "videos": result["counts"]["video"],
            "images": result["counts"]["image"], "shortfall": result["shortfall"], "research_complete": False}


def collect(task, provider, *, resume=False):
    """Caller holds the task lock; resume never silently discards saved evidence."""
    state = task.get("state.json")
    ensure(not state or resume, "existing_task_requires_explicit_resume")
    if state:
        ensure(state.get("task_hash") == task.fingerprint, "task_changed_after_capture_started")
    if task.p("capture.json").exists():
        return verify(task)
    state = state or {"schema_version": 1, "task_hash": task.fingerprint, "phase": "listing",
                      "started_at": now(), "cursor": "", "list_exhausted": False,
                      "pages": [], "candidates": {}, "selected": [], "excluded": []}
    state.pop("stop_reason", None)
    try:
        task.save("state.json", state)
        profile = task.cached("raw/profile.json", lambda: provider.profile(task.config["account_id"]))
        ensure(profile.get("success") is True and isinstance(profile.get("data"), dict), "profile_missing")
        pages_this_run = 0
        while not state["list_exhausted"]:
            ensure(pages_this_run < task.config["limits"]["max_pages_per_run"], "list_page_budget_reached_resume_required")
            cursor = state["cursor"]
            response = task.cached(f"raw/list-{digest(cursor)[:24]}.json",
                                   lambda: provider.page(task.config["account_id"], cursor))
            data, notes = page_data(response, "notes")
            ensure(type(data.get("has_more")) is bool, "list_end_unknown")
            for note in notes:
                nid = note.get("note_id") or note.get("id")
                ensure(isinstance(nid, str) and ID.fullmatch(nid), "list_identity_missing")
                published = timestamp(note.get("time"))
                if nid in state["candidates"]:
                    ensure(state["candidates"][nid]["time"] == published, "list_changed_during_capture")
                state["candidates"][nid] = {"id": nid, "time": published,
                                             "xsec_token": note.get("xsec_token") or "", "type": note.get("type")}
            new_cursor = data.get("cursor")
            if data["has_more"]:
                ensure(isinstance(new_cursor, str) and new_cursor and new_cursor != cursor
                       and new_cursor not in [p["input_cursor"] for p in state["pages"]], "list_cursor_invalid")
            state["pages"].append({"input_cursor": cursor, "count": len(notes), "has_more": data["has_more"]})
            state["cursor"] = new_cursor if data["has_more"] else ""
            state["list_exhausted"] = not data["has_more"]
            pages_this_run += 1
            task.save("state.json", state)

        state["phase"] = "collecting"
        ordered = sorted(state["candidates"].values(), key=lambda n: (n["time"], n["id"]), reverse=True)
        done = {n["id"] for n in state["selected"] + state["excluded"]}
        for candidate in ordered:
            if len(state["selected"]) >= task.config["target_count"]:
                break
            nid = candidate["id"]
            if nid in done:
                continue
            raw = task.cached(f"raw/detail-{nid}.json", lambda: provider.detail(nid, candidate["xsec_token"]))
            work = provider.describe(raw, nid, task.config["account_id"])
            ensure(work["id"] == nid and work["author_id"] == task.config["account_id"], "detail_identity_mismatch")
            ensure(timestamp(work["published_ms"]) == candidate["time"], "detail_publication_time_mismatch")
            ensure(work["type"] in ("video", "image"), "unknown_work_type")
            prefix = f"works/{nid}/media"
            work["assets"] = []
            if work["type"] == "video":
                duration = work.get("duration_seconds")
                if type(duration) in (int, float) and math.isfinite(duration) and duration > 1200:
                    state["excluded"].append({"id": nid, "reason": "video_over_1200_seconds", "duration_seconds": duration,
                                              "duration_source": "api"})
                    task.save("state.json", state)
                    continue
                video = asset(task, provider, work["video_url"], prefix + "/video", "video")
                duration = video.get("duration_seconds")
                ensure(type(duration) in (int, float) and math.isfinite(duration) and duration > 0, "video_duration_unknown")
                if duration > 1200:
                    state["excluded"].append({"id": nid, "reason": "video_over_1200_seconds", "duration_seconds": duration,
                                              "duration_source": "local_probe"})
                    task.save("state.json", state)
                    continue
                cover = asset(task, provider, work["image_urls"][0], prefix + "/cover", "image")
                work.update(video=video, cover=cover, assets=[video, cover])
            else:
                urls = work["image_urls"]
                ensure(isinstance(urls, list) and urls, "image_list_empty")
                images = [asset(task, provider, url, prefix + f"/images/{i:02d}", "image")
                          for i, url in enumerate(urls, 1)]
                work.update(images=images, expected_images=len(urls), cover=images[0], assets=images)
            page = task.cached(f"raw/comments-{nid}.json", lambda: provider.comments(nid, candidate["xsec_token"]))
            _, roots = page_data(page, "comments")
            supplements = []
            budget = task.config["limits"]["reply_pages_per_work"]
            for root in roots:
                more, cursor, visited = root.get("sub_comment_has_more"), root.get("sub_comment_cursor"), set()
                while more is True and budget > 0:
                    ensure(isinstance(cursor, str) and cursor and cursor not in visited, "reply_cursor_invalid")
                    visited.add(cursor)
                    cache_key = digest([nid, root.get("id"), cursor])[:24]
                    extra = task.cached(f"raw/replies-{cache_key}.json",
                                        lambda: provider.replies(nid, root, cursor, candidate["xsec_token"]))
                    subdata, _ = page_data(extra, "comments")
                    supplements.append((root["id"], extra))
                    more, cursor = subdata.get("has_more"), subdata.get("cursor")
                    budget -= 1
            comments = normalize_comments(task, nid, page, supplements, work["metrics"].get("comment_count"))
            # Short-lived signed URLs stay only in raw evidence, never in the research manifest.
            work.pop("video_url", None)
            work.pop("image_urls", None)
            work.update(source_url=f"https://www.xiaohongshu.com/explore/{nid}", captured_at=now(),
                        semantic_review_status="not_reviewed", comments_file=f"works/{nid}/comments.json")
            task.save(f"works/{nid}/comments.json", comments)
            task.save(f"works/{nid}/work.json", work)
            state["selected"].append({"id": nid, "type": work["type"], "published_ms": work["published_ms"]})
            task.save("state.json", state)

        evidence = []
        if task.p("runtime.json").exists():
            evidence.append({"path": "runtime.json", "sha256": file_hash(task.p("runtime.json"))})
        for folder in ("raw", "works"):
            for path in sorted(task.p(folder).rglob("*.json")):
                ensure(not path.is_symlink(), "symlink_not_allowed")
                evidence.append({"path": str(path.relative_to(task.root)), "sha256": file_hash(path)})
        result = {"schema_version": 1, "task_hash": task.fingerprint, "started_at": state["started_at"],
                  "completed_at": now(), "list_exhausted": True, "candidate_count": len(ordered),
                  "selected": state["selected"], "excluded": state["excluded"], "evidence": evidence,
                  "requested": task.config["target_count"], "shortfall": task.config["target_count"] - len(state["selected"]),
                  "counts": {kind: sum(w["type"] == kind for w in state["selected"]) for kind in ("video", "image")},
                  "sampling_note": "按完整已遍历列表的发布时间排序去重，置顶不优先；这是采集时间窗口内的公开内容快照。",
                  "research_complete": False}
        result["manifest_hash"] = digest(result)
        task.save("capture.json", result)
        verified = verify(task)
        state["phase"] = "collected"
        task.save("state.json", state)
        return verified
    except (StopCapture, KeyboardInterrupt):
        import sys
        exc = sys.exc_info()[1]
        state["phase"] = "stopped"
        state["stop_reason"] = str(exc) if isinstance(exc, StopCapture) else "interrupted"
        task.save("state.json", state)
        raise
    except Exception:
        state["phase"] = "stopped"
        state["stop_reason"] = "collection_schema_or_runtime_error"
        task.save("state.json", state)
        raise StopCapture("collection_schema_or_runtime_error") from None
