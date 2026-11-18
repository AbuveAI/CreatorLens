from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from creatorlens.collection import collect, normalize_comments, verify
from creatorlens.storage import StopCapture, read, write
from creatorlens.tasks import Task, create
from creatorlens.workspace import initialize
from creatorlens.spider import describe

ACCOUNT = "a" * 24


def nid(number):
    return f"{number:024x}"


def note(number, kind="normal", time=None, images=2, duration=10):
    return {"id": nid(number), "note_card": {"type": kind, "time": time or number * 1000,
            "user": {"user_id": ACCOUNT}, "title": "Synthetic fixture", "desc": "Authored test data",
            "interact_info": {"comment_count": "3"}, "image_list": [
                {"url_default": f"https://test.xhscdn.com/{number}-{i}"} for i in range(images)],
            "video": {"capa": {"duration": duration}}}}


def response(**data):
    return {"success": True, "data": data}


class FakeProvider:
    def __init__(self, notes, pages=None):
        self.notes = {n["id"]: n for n in notes}
        listed = [{"note_id": n["id"], "time": n["note_card"]["time"], "type": n["note_card"]["type"],
                   "xsec_token": "synthetic-test-token"} for n in notes]
        self.pages = pages or {"": response(notes=listed, has_more=False)}
        self.calls = []
        self.fail_download = None
        self.fail_detail = None
        self.actual_duration = {}

    def profile(self, account):
        self.calls.append(("profile", account))
        return response(basic_info={"nickname": "Synthetic account"})

    def page(self, account, cursor):
        self.calls.append(("page", cursor))
        return deepcopy(self.pages[cursor])

    def detail(self, note_id, token):
        self.calls.append(("detail", note_id))
        if self.fail_detail == note_id:
            raise StopCapture("synthetic_network_failure")
        return response(items=[deepcopy(self.notes[note_id])])

    def describe(self, raw, note_id, account):
        return describe(raw, note_id, account, lambda _: "https://test.xhscdn.com/video-" + note_id)

    def comments(self, note_id, token):
        self.calls.append(("comments", note_id))
        return response(comments=[{"id": "root", "content": "Question", "sub_comment_has_more": False,
                         "user_info": {"user_id": "viewer"}, "sub_comments": [
                             {"id": "reply", "content": "Reply", "user_info": {"user_id": ACCOUNT}}]}], has_more=True)

    def replies(self, *args):
        raise AssertionError("not expected")

    def download(self, url, path):
        self.calls.append(("download", url))
        if self.fail_download == url:
            raise StopCapture("synthetic_download_failure")
        path.write_text(url)

    def inspect(self, path, kind):
        if kind == "video":
            work_id = path.read_text().split("video-")[-1]
            duration = self.actual_duration.get(work_id, self.notes[work_id]["note_card"]["video"]["capa"]["duration"])
            return {"duration_seconds": duration, "extension": ".mp4"}
        return {"width": 2, "height": 2, "format": "PNG", "extension": ".png"}


class CollectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name).resolve() / "workspace"
        initialize(self.workspace)
        self.task = create(self.workspace, url=f"https://www.xiaohongshu.com/user/profile/{ACCOUNT}",
                           name="Synthetic", batch="test", count=3)

    def run_capture(self, provider, resume=False):
        with self.task.lock():
            return collect(self.task, provider, resume=resume)

    def test_mixed_latest_order_dedupe_and_all_images(self):
        provider = FakeProvider([note(1), note(4, "video"), note(3, images=4), note(2)])
        provider.pages[""]["data"]["notes"].append(deepcopy(provider.pages[""]["data"]["notes"][0]))
        result = self.run_capture(provider)
        self.assertEqual(result["selected"], 3)
        self.assertEqual(result["videos"], 1)
        self.assertEqual(result["images"], 2)
        manifest = self.task.get("capture.json")
        self.assertEqual([w["id"] for w in manifest["selected"]], [nid(4), nid(3), nid(2)])
        work = self.task.get(f"works/{nid(3)}/work.json")
        self.assertEqual(len(work["images"]), 4)
        self.assertEqual([Path(i["path"]).stem for i in work["images"]], ["01", "02", "03", "04"])
        self.assertEqual(work["semantic_review_status"], "not_reviewed")
        self.assertNotIn("synthetic-test-token", json.dumps(manifest))

    def test_twenty_minutes_kept_longer_video_replaced(self):
        provider = FakeProvider([note(5, "video", duration=1200.01), note(4, "video", duration=1200), note(3), note(2)])
        result = self.run_capture(provider)
        self.assertEqual(result["selected"], 3)
        self.assertEqual(self.task.get("capture.json")["excluded"][0]["id"], nid(5))
        self.assertNotIn(("download", "https://test.xhscdn.com/video-" + nid(5)), provider.calls)

    def test_measured_long_video_replaced(self):
        provider = FakeProvider([note(4, "video"), note(3), note(2), note(1)])
        provider.actual_duration[nid(4)] = 1201
        self.run_capture(provider)
        self.assertEqual(self.task.get("capture.json")["excluded"][0]["duration_source"], "local_probe")

    def test_confirmed_shortfall_and_empty_account_are_honest(self):
        provider = FakeProvider([note(1)])
        result = self.run_capture(provider)
        self.assertEqual(result["shortfall"], 2)
        second = create(self.workspace, url=self.task.config["profile_url"], name="Empty", batch="empty")
        with second.lock():
            empty = collect(second, FakeProvider([]))
        self.assertEqual(empty["selected"], 0)
        self.assertEqual(empty["shortfall"], 10)

    def test_page_budget_is_not_shortfall_and_resumes_cursor(self):
        pages = {"" if i == 0 else str(i): response(notes=[], has_more=True, cursor=str(i + 1)) for i in range(5)}
        pages["5"] = response(notes=[], has_more=False)
        provider = FakeProvider([], pages)
        with self.assertRaisesRegex(StopCapture, "list_page_budget"):
            self.run_capture(provider)
        self.assertFalse(self.task.p("capture.json").exists())
        self.assertFalse(self.task.status()["list_exhausted"])
        before = list(provider.calls)
        self.run_capture(provider, resume=True)
        self.assertEqual(provider.calls[len(before):], [("page", "5")])

    def test_missing_pagination_end_never_claims_complete(self):
        with self.assertRaisesRegex(StopCapture, "list_end_unknown"):
            self.run_capture(FakeProvider([], {"": response(notes=[])}))
        self.assertFalse(self.task.p("capture.json").exists())

    def test_later_page_newer_work_is_not_lost_to_pinned_first_page(self):
        provider = FakeProvider([note(1), note(2), note(3), note(4)])
        notes = provider.pages[""]["data"]["notes"]
        provider.pages = {"": response(notes=notes[:3], has_more=True, cursor="next"),
                          "next": response(notes=notes[3:], has_more=False)}
        self.run_capture(provider)
        self.assertEqual([x["id"] for x in self.task.get("capture.json")["selected"]], [nid(4), nid(3), nid(2)])

    def test_limited_reply_paging_keeps_unknown_coverage_and_deduplicates(self):
        provider = FakeProvider([note(1)])
        provider.comments = lambda *_: response(comments=[{
            "id": "root", "sub_comment_has_more": True, "sub_comment_cursor": "first", "content": "Q"}], has_more=False)
        requests = []
        def replies(note_id, root, cursor, token):
            requests.append(cursor)
            return response(comments=[{"id": "reply", "content": "A", "target_comment": {"id": "missing-parent"}}],
                            has_more=True, cursor=cursor + "-next")
        provider.replies = replies
        self.run_capture(provider)
        comments = self.task.get(f"works/{nid(1)}/comments.json")
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(comments["rows"]), 2)
        self.assertEqual(comments["rows"][1]["parent_id"], "missing-parent")
        self.assertEqual(len(comments["coverage"]["incomplete_threads"]), 1)

    def test_corrupt_raw_cache_stops_resume_before_reusing_it(self):
        provider = FakeProvider([note(1)])
        provider.fail_detail = nid(1)
        with self.assertRaises(StopCapture):
            self.run_capture(provider)
        raw = self.task.get("raw/profile.json")
        raw["payload"]["success"] = False
        self.task.save("raw/profile.json", raw)
        with self.assertRaisesRegex(StopCapture, "raw_evidence_changed"):
            self.run_capture(provider, resume=True)

    def test_resume_preserves_completed_images_without_redownload(self):
        provider = FakeProvider([note(3, images=3), note(2), note(1)])
        provider.fail_download = "https://test.xhscdn.com/3-1"
        with self.assertRaisesRegex(StopCapture, "download_failure"):
            self.run_capture(provider)
        self.assertFalse(self.task.p("capture.json").exists())
        provider.fail_download = None
        self.run_capture(provider, resume=True)
        self.assertEqual(provider.calls.count(("download", "https://test.xhscdn.com/3-0")), 1)
        self.assertEqual(provider.calls.count(("detail", nid(3))), 1)
        self.assertEqual(verify(self.task)["selected"], 3)

    def test_repeated_capture_requires_resume_and_complete_resume_is_local(self):
        provider = FakeProvider([note(1)])
        self.run_capture(provider)
        with self.assertRaisesRegex(StopCapture, "explicit_resume"):
            self.run_capture(provider)
        count = len(provider.calls)
        self.run_capture(provider, resume=True)
        self.assertEqual(len(provider.calls), count)

    def test_unknown_type_stops_instead_of_replacing(self):
        provider = FakeProvider([note(3, "unknown"), note(2), note(1)])
        with self.assertRaisesRegex(StopCapture, "unknown_work_type"):
            self.run_capture(provider)
        self.assertNotIn(("detail", nid(2)), provider.calls)

    def test_detail_timestamp_mismatch_stops_selection(self):
        provider = FakeProvider([note(1)])
        provider.notes[nid(1)]["note_card"]["time"] = 9000
        with self.assertRaisesRegex(StopCapture, "publication_time_mismatch"):
            self.run_capture(provider)

    def test_unknown_comment_total_and_parent_remain_unknown(self):
        provider = FakeProvider([note(1)])
        provider.notes[nid(1)]["note_card"]["interact_info"]["comment_count"] = "1.2万"
        self.run_capture(provider)
        comments = self.task.get(f"works/{nid(1)}/comments.json")
        self.assertIsNone(comments["coverage"]["total"])
        self.assertIsNone(comments["coverage"]["gap"])
        self.assertIsNone(comments["rows"][1]["parent_id"])
        self.assertEqual(comments["rows"][1]["root_id"], "root")
        self.assertFalse(comments["coverage"]["full_comment_section_claimed"])

    def test_tampered_media_and_raw_evidence_fail_validation(self):
        provider = FakeProvider([note(1)])
        self.run_capture(provider)
        work = self.task.get(f"works/{nid(1)}/work.json")
        image = self.task.p(work["images"][0]["path"])
        original = image.read_bytes()
        image.write_bytes(b"changed")
        with self.assertRaisesRegex(StopCapture, "media_evidence_changed"):
            verify(self.task)
        image.write_bytes(original)
        self.task.p("raw/profile.json").write_text("{}")
        with self.assertRaisesRegex(StopCapture, "capture_evidence_changed"):
            verify(self.task)

    def test_changed_task_rejects_resume(self):
        provider = FakeProvider([note(1)])
        provider.fail_detail = nid(1)
        with self.assertRaises(StopCapture):
            self.run_capture(provider)
        config = read(self.task.path)
        config["target_count"] = 2
        write(self.task.path, config)
        modified = Task(self.workspace, self.task.path)
        with modified.lock(), self.assertRaisesRegex(StopCapture, "task_changed"):
            collect(modified, provider, resume=True)

    def test_task_cannot_use_another_workspaces_state(self):
        other = self.workspace.parent / "other"
        initialize(other)
        with self.assertRaisesRegex(StopCapture, "outside_workspace"):
            Task(other, self.task.path)

    def test_same_task_has_one_writer(self):
        with self.task.lock():
            with self.assertRaisesRegex(StopCapture, "busy"):
                with self.task.lock():
                    pass


if __name__ == "__main__":
    unittest.main()
