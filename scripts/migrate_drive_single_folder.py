#!/usr/bin/env python3
"""把每支影片各自的 Drive 資料夾搬進同一個共用「Video Timeline」資料夾。

舊版排版：根目錄下每支影片一個資料夾「Video Timeline - <標題> - <job_id>」。
新版排版：根目錄下只有一個「Video Timeline」資料夾，分段、原片與 A 報告都在同一層，
檔名是「<標題> - <job_id> - <原檔名>」。

用法：
    python scripts/migrate_drive_single_folder.py                 # dry-run：只印計畫，零寫入
    python scripts/migrate_drive_single_folder.py --apply         # 真的搬移
    python scripts/migrate_drive_single_folder.py --apply --trash-job <job_id>

執行前先確認 MCP 服務沒有進行中的 Drive 上傳：正在上傳的 job 會把檔案寫回舊的
每 job 資料夾，與本腳本同時動作會互相覆蓋。腳本可重跑，中途失敗後再跑一次會收斂：
每個舊資料夾都是「先搬檔並逐檔核對 → 再改寫 manifest 與報告狀態檔 → 最後才丟資料夾」，
中斷後重跑會補上沒做完的那一步。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 直接執行本檔時讓 repo 根目錄的模組可用
    sys.path.insert(0, str(ROOT))

import analysis_report  # noqa: E402
import drive_storage  # noqa: E402
import pipeline  # noqa: E402

OLD_FOLDER = re.compile(r"^Video Timeline - (.+) - ([0-9a-f]{12})$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把每 job 的 Drive 資料夾搬進共用「Video Timeline」資料夾。")
    parser.add_argument("--jobs-root", default="mcp_jobs", help="本機工作根目錄（預設 mcp_jobs）")
    parser.add_argument("--trash-job", action="append", default=[], metavar="JOB_ID",
                        help="這個 job 的舊資料夾整個移入垃圾桶，不搬檔（可重複指定）")
    parser.add_argument("--apply", action="store_true", help="真的執行；預設只印計畫（零寫入）")
    return parser.parse_args(argv)


class Migration:
    """把舊的每 job 資料夾收斂成一個共用資料夾；預設只計畫。"""

    def __init__(self, jobs_root: Path, trash_jobs: set[str], apply: bool):
        self.jobs_root = jobs_root
        self.trash_jobs = trash_jobs
        self.apply = apply
        self.parent_id = drive_storage._env("GOOGLE_DRIVE_FOLDER_ID")
        self.shared_id = ""
        self.shared_url = ""
        self.problems = 0
        # job_id -> 本機 manifest 指著的舊資料夾；用來分辨孤兒資料夾與重複 job 的舊版資料夾。
        self.manifest_folders: dict[str, str] = {}
        self.manifest_prefixes: dict[str, str] = {}
        # 修復階段擋下的 job：資料夾階段整個資料夾都不動、不丟。
        self.repair_blocked: set[str] = set()
        # job_id -> 擋下的原因（repair_blocked / local_write_failed / manifest_verify_failed）。
        self.blocked_jobs: dict[str, str] = {}
        # 本機 manifest 存在但讀不到／壞掉的 job：不是孤兒，資料夾一律留著。
        self.invalid_manifests: set[str] = set()
        # 還被本機紀錄引用的 Drive file id：這些檔案不能被丟進垃圾桶。
        self.protected_file_ids: set[str] = set()

    # ---- 輸出與小工具 -------------------------------------------------
    def emit(self, step: str, **payload: Any) -> None:
        print(json.dumps({"step": step, **payload}, ensure_ascii=False), flush=True)

    def read_json(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def manifest_path(self, job_id: str) -> Path:
        return self.jobs_root / job_id / "drive-manifest.json"

    def manifest_paths(self) -> list[Path]:
        if not self.jobs_root.is_dir():
            return []
        return sorted(self.jobs_root.glob("*/drive-manifest.json"))

    def report_state_paths(self, job_id: str) -> list[Path]:
        reports = self.jobs_root / job_id / "reports"
        if not reports.is_dir():
            return []
        return sorted(reports.glob("*/drive-*.json"))

    def report_state_paths_all(self) -> list[Path]:
        """所有 job 的報告狀態檔；用來找出還被引用的 Drive file id。"""
        if not self.jobs_root.is_dir():
            return []
        return sorted(self.jobs_root.glob("*/reports/*/drive-*.json"))

    def read_manifest(self, job_id: str) -> tuple[str, Any]:
        """回傳 (狀態, 內容)：missing（不存在）／invalid（存在但讀不到或不是物件）／ok。"""
        path = self.manifest_path(job_id)
        if not path.exists():
            return "missing", None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "invalid", None
        if not isinstance(data, dict):
            return "invalid", None
        return "ok", data

    def block_job(self, job_id: str, reason: str) -> None:
        """把 job 標成擋下：資料夾階段整個資料夾都不動、不丟。"""
        if job_id:
            self.blocked_jobs.setdefault(job_id, reason)

    def blocked_reason(self, job_id: str, folder_id: str) -> str:
        """被擋下的 job 只有在 manifest 指向這個資料夾時才擋住它。"""
        reason = self.blocked_jobs.get(job_id, "")
        if not reason:
            return ""
        state, data = self.read_manifest(job_id)
        if state != "ok":
            return ""
        if str(data.get("folder_id") or "") != folder_id:
            return ""
        return reason

    def backup(self, path: Path, job_id: str = "") -> bool:
        """改寫本機 JSON 前留一份當日備份；已有備份不覆蓋。失敗回傳 False。"""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        target = path.with_name(path.name + ".bak-" + stamp)
        if target.exists():
            return True
        try:
            target.write_bytes(path.read_bytes())
        except OSError as exc:
            self.problems += 1
            self.block_job(job_id, "local_write_failed")
            self.emit("backup_failed", job_id=job_id, path=str(target), error=str(exc))
            return False
        self.emit("backup", path=str(target))
        return True

    def drive_name(self, name: str, prefix: str) -> str:
        """檔名帶上前綴；已經有前綴的不重複加。"""
        if not prefix:
            return name
        if name.startswith(prefix):
            return name
        return f"{prefix} - {name}"

    # ---- 本機 manifest ------------------------------------------------
    def load_manifests(self) -> None:
        """讀出每個 job 的 manifest 指向哪個資料夾；孤兒資料夾不在其中。"""
        for path in self.manifest_paths():
            job_id = path.parent.name
            state, data = self.read_manifest(job_id)
            if state == "invalid":
                # manifest 存在但讀不到：不是孤兒，資料夾一律留著，算問題。
                self.invalid_manifests.add(job_id)
                self.problems += 1
                self.emit("manifest_invalid", job_id=job_id, path=str(path))
                continue
            if state != "ok":
                continue
            folder_id = str(data.get("folder_id") or "")
            if folder_id:
                self.manifest_folders[job_id] = folder_id
            prefix = str(data.get("drive_prefix") or "").strip()
            if prefix:
                self.manifest_prefixes[job_id] = prefix

    def load_protected_file_ids(self) -> None:
        """收集還被本機紀錄引用的 Drive file id：這些檔案不能被丟進垃圾桶。"""
        ids: set[str] = set()
        for path in self.manifest_paths():
            data = self.read_json(path)
            if not isinstance(data, dict):
                continue
            for item in data.get("files") or []:
                if isinstance(item, dict):
                    self._collect_file_id(ids, item)
            source = data.get("source_file")
            if isinstance(source, dict):
                self._collect_file_id(ids, source)
        for path in self.report_state_paths_all():
            data = self.read_json(path)
            if isinstance(data, dict):
                self._collect_file_id(ids, data)
        self.protected_file_ids = ids

    @staticmethod
    def _collect_file_id(ids: set[str], item: dict[str, Any]) -> None:
        file_id = str(item.get("file_id") or "")
        if file_id:
            ids.add(file_id)

    def manifest_folder_id(self, job_id: str) -> str:
        data = self.read_json(self.manifest_path(job_id))
        if not isinstance(data, dict):
            return ""
        return str(data.get("folder_id") or "")

    def manifest_status(self, job_id: str) -> str:
        data = self.read_json(self.manifest_path(job_id))
        if not isinstance(data, dict):
            return ""
        return str(data.get("status") or "")

    # ---- 探索 ---------------------------------------------------------
    def preferred_folder_ids(self) -> list[str]:
        """共用資料夾的優先順序：drive-video-folder.json → 本機 manifest。"""
        ids: list[str] = []
        state = self.read_json(self.jobs_root / "drive-video-folder.json")
        if isinstance(state, dict):
            file_id = str(state.get("folder_id") or "")
            if file_id:
                ids.append(file_id)
        for folder_id in self.manifest_folders.values():
            if folder_id and folder_id not in ids:
                ids.append(folder_id)
        return ids

    def find_shared(self, token: str) -> dict[str, Any] | None:
        """共用資料夾沿用同一套選擇規則：名稱相同、未在垃圾桶、最早建立。"""
        return drive_storage._find_shared_video_folder(token, self.parent_id,
                                                       self.preferred_folder_ids())

    def shared_candidates(self, token: str) -> list[dict[str, Any]]:
        """Drive 上同名共用資料夾的候選；多於一個就是真的模稜兩可，算問題。"""
        candidates = drive_storage._shared_video_folder_candidates(token, self.parent_id)
        if len(candidates) > 1:
            self.problems += 1
            self.emit("multiple_shared_candidates",
                      folder_ids=[str(item.get("id") or "") for item in candidates],
                      created=[str(item.get("createdTime") or "") for item in candidates],
                      note="Drive 上有多個同名共用資料夾；已沿用既有紀錄指向的那一個")
        return candidates

    def stamp(self, folder: dict[str, Any]) -> str:
        raw = str(folder.get("createdTime") or "")
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return "00000000-0000"
        return moment.strftime("%Y%m%d-%H%M")

    def assign_prefixes(self, folders: list[dict[str, Any]]) -> dict[str, str]:
        """同一個 job 有多個舊資料夾時，只有一個用一般前綴，其餘標成舊版。"""
        by_job: dict[str, list[dict[str, Any]]] = {}
        for folder in folders:
            match = OLD_FOLDER.fullmatch(str(folder.get("name") or ""))
            if match:
                by_job.setdefault(match.group(2), []).append(folder)
        prefixes: dict[str, str] = {}
        for job_id, group in by_job.items():
            referenced = self.manifest_folder_id(job_id)
            ordered = sorted(group, key=lambda f: (str(f.get("createdTime") or ""), str(f.get("id") or "")))
            plain = next((f for f in group if str(f.get("id")) == referenced), None) or ordered[-1]
            for folder in group:
                # 每個舊資料夾用自己的標題；舊版資料夾的標題不一定跟被 manifest 指著的一樣。
                title = OLD_FOLDER.fullmatch(str(folder.get("name") or "")).group(1)
                base = f"{title} - {job_id}"
                if str(folder.get("id")) == str(plain.get("id")):
                    prefixes[str(folder.get("id"))] = base
                else:
                    prefixes[str(folder.get("id"))] = f"{base} - 舊版 {self.stamp(folder)}"
            self.emit("prefix", job_id=job_id, prefix=prefixes[str(plain.get("id"))], folders=[
                {"id": f.get("id"), "name": f.get("name"), "prefix": prefixes[str(f.get("id"))],
                 "referenced_by_manifest": str(f.get("id")) == referenced} for f in group])
        return prefixes

    # ---- 共用資料夾 ---------------------------------------------------
    def shared_folder(self, token: str, needed: bool) -> tuple[str, str]:
        """共用資料夾只有一個；真的有東西要搬時才建立。"""
        state_path = self.jobs_root / "drive-video-folder.json"
        self.shared_candidates(token)  # 同名候選超過一個就回報（dry-run 也要顯示）。
        if needed and self.apply:
            folder = drive_storage._video_folder(token, state_path,
                                                 self.preferred_folder_ids())
            self.shared_id = str(folder["folder_id"])
            self.shared_url = str(folder["folder_url"])
            self.emit("shared_folder", folder_id=self.shared_id, folder_url=self.shared_url,
                      created=True)
            return self.shared_id, self.shared_url
        found = self.find_shared(token)
        if found:
            self.shared_id = str(found.get("id") or "")
            self.shared_url = str(found.get("webViewLink") or "")
            self.emit("shared_folder", folder_id=self.shared_id, folder_url=self.shared_url,
                      created=False)
            return self.shared_id, self.shared_url
        self.emit("shared_folder", folder_id="", folder_url="", created=False,
                  note="--apply 時會建立共用資料夾")
        return "", ""

    # ---- 修復階段（中斷後重跑用） -------------------------------------
    def repair_phase(self, token: str) -> None:
        """以本機 manifest 為準，補上中斷的遷移：每次執行都跑。"""
        if not self.shared_id:
            self.emit("repair_skipped", reason="no_shared_folder")
            return
        for path in self.manifest_paths():
            job_id = path.parent.name
            if job_id in self.trash_jobs:
                continue  # 要整個丟掉的 job 不做修復，讓 --trash-job 決定它的下場
            try:
                self.repair_one(token, job_id, path)
            except OSError as exc:
                # 本機寫入失敗不能中斷整批：記下來，這個 job 留給下一次。
                self.problems += 1
                self.block_job(job_id, "local_write_failed")
                self.emit("repair_write_failed", job_id=job_id, path=str(path), error=str(exc))

    def repair_one(self, token: str, job_id: str, path: Path) -> None:
        """單一 job 的修復；寫入失敗只擋下這個 job。"""
        state, data = self.read_manifest(job_id)
        if state != "ok" or data.get("status") != "drive_ready":
            return
        folder_id = str(data.get("folder_id") or "")
        if not folder_id:
            return
        if folder_id != self.shared_id:
            repaired = self.repair_manifest(token, job_id, path, data, folder_id)
            if repaired is not None:
                data = repaired
        if str(data.get("folder_id") or "") == self.shared_id:
            self.repair_report_states(token, job_id, data)

    def repair_manifest(self, token: str, job_id: str, path: Path, data: dict,
                        folder_id: str) -> dict | None:
        """檔案都已在共用資料夾時，用 Drive 上的實際檔名還原 manifest。"""
        prefix = self.consistent_prefix(token, job_id, data, folder_id)
        if prefix is None:
            return None
        if not self.apply:
            self.emit("plan_manifest_repaired", job_id=job_id, path=str(path),
                      folder_id=self.shared_id, drive_prefix=prefix)
            return None
        issues = self.verify_manifest_files(token, job_id, data, prefix)
        if issues:
            # manifest 記錄的檔案跟共用資料夾對不上：不能改寫，資料夾也不能丟。
            self.problems += 1
            self.repair_blocked.add(job_id)
            self.block_job(job_id, "repair_blocked")
            self.emit("manifest_verify_failed", job_id=job_id, path=str(path), issues=issues)
            return None
        updated = self.rewrite_manifest(path, data, prefix, job_id)
        if updated is None:
            self.block_job(job_id, "local_write_failed")
            return None
        self.emit("manifest_repaired", job_id=job_id, path=str(path), folder_id=self.shared_id,
                  drive_prefix=prefix)
        return updated

    def consistent_prefix(self, token: str, job_id: str, data: dict, folder_id: str) -> str | None:
        """manifest 記錄的檔案是否都已在共用資料夾，且前綴一致。"""
        entries = [item for item in (data.get("files") or []) if isinstance(item, dict)]
        source = data.get("source_file")
        if isinstance(source, dict):
            entries.append(source)
        if not entries:
            self.emit("repair_skipped", job_id=job_id, reason="no_files")
            return None
        remotes: list[tuple[dict, dict]] = []
        for item in entries:
            file_id = str(item.get("file_id") or "")
            remote = drive_storage._get_file(token, file_id) if file_id else None
            if remote is None:
                self.note_repair_problem(job_id, file_id=file_id, reason="file_missing")
                return None
            if remote.get("trashed"):
                self.note_repair_problem(job_id, file_id=file_id, reason="file_trashed")
                return None
            parents = list(remote.get("parents") or [])
            if parents != [self.shared_id] and folder_id not in parents:
                self.note_repair_problem(job_id, file_id=file_id, reason="file_elsewhere")
                return None
            remotes.append((item, remote))
        if any(folder_id in list(remote.get("parents") or []) for _item, remote in remotes):
            # 還有檔案在舊資料夾：交給資料夾階段搬。
            self.emit("repair_deferred", job_id=job_id, reason="still_in_old_folder")
            return None
        prefixes: set[str] = set()
        for item, remote in remotes:
            file_name = str(item.get("file_name") or "")
            suffix = " - " + file_name
            name = str(remote.get("name") or "")
            expected_sha = str(item.get("sha256") or "")
            if expected_sha and remote.get("sha256Checksum") != expected_sha:
                self.note_repair_problem(job_id, file_id=str(item.get("file_id") or ""),
                                         reason="sha_mismatch")
                return None
            if not file_name or not name.endswith(suffix):
                self.note_repair_problem(job_id, file_id=str(item.get("file_id") or ""),
                                         reason="name_mismatch", name=name)
                return None
            prefixes.add(name[: -len(suffix)])
        if len(prefixes) != 1:
            self.note_repair_problem(job_id, reason="prefix_mismatch", prefixes=sorted(prefixes))
            return None
        return prefixes.pop()

    def note_repair_problem(self, job_id: str, **payload: Any) -> None:
        """修復階段擋下這個 job：資料夾階段整個資料夾都不能動。"""
        self.problems += 1
        self.repair_blocked.add(job_id)
        self.block_job(job_id, "repair_blocked")
        self.emit("repair_problem", job_id=job_id, **payload)

    def verify_manifest_files(self, token: str, job_id: str, data: dict,
                              prefix: str) -> list[dict[str, Any]]:
        """逐一驗證 manifest 記錄的檔案確實在共用資料夾、名字與 sha 相符。

        回傳問題清單；空清單代表可以安全改寫 manifest。
        """
        entries = [item for item in (data.get("files") or []) if isinstance(item, dict)]
        source = data.get("source_file")
        if isinstance(source, dict):
            entries.append(source)
        issues: list[dict[str, Any]] = []
        for item in entries:
            file_id = str(item.get("file_id") or "")
            file_name = str(item.get("file_name") or "")
            if not file_id or not file_name:
                issues.append({"file_id": file_id, "reason": "manifest_entry_incomplete"})
                continue
            remote = drive_storage._get_file(token, file_id)
            if remote is None:
                issues.append({"file_id": file_id, "reason": "file_missing"})
                continue
            if remote.get("trashed"):
                issues.append({"file_id": file_id, "reason": "file_trashed"})
                continue
            if list(remote.get("parents") or []) != [self.shared_id]:
                issues.append({"file_id": file_id, "reason": "file_elsewhere",
                               "parents": list(remote.get("parents") or [])})
                continue
            expected_sha = str(item.get("sha256") or "")
            if expected_sha and remote.get("sha256Checksum") != expected_sha:
                issues.append({"file_id": file_id, "reason": "sha_mismatch"})
                continue
            expected_name = f"{prefix} - {file_name}"
            if str(remote.get("name") or "") != expected_name:
                issues.append({"file_id": file_id, "reason": "name_mismatch",
                               "name": str(remote.get("name") or "")})
        return issues

    def rewrite_manifest(self, path: Path, data: dict, prefix: str,
                         job_id: str = "") -> dict | None:
        """manifest 改指向共用資料夾；其餘欄位不動。失敗回傳 None。"""
        if not self.backup(path, job_id):
            return None
        updated = dict(data)
        updated["folder_id"] = self.shared_id
        updated["folder_url"] = self.shared_url
        updated["parent_id"] = self.parent_id
        updated["drive_prefix"] = prefix
        updated["files"] = [{**item, "drive_name": f"{prefix} - {item.get('file_name')}"}
                            for item in (data.get("files") or []) if isinstance(item, dict)]
        source = data.get("source_file")
        if isinstance(source, dict):
            updated["source_file"] = {**source, "drive_name": f"{prefix} - {source.get('file_name')}"}
        try:
            pipeline.write_json(path, updated)
        except OSError as exc:
            self.problems += 1
            self.emit("manifest_rewrite_failed", job_id=job_id, path=str(path), error=str(exc))
            return None
        return updated

    def repair_report_states(self, token: str, job_id: str, data: dict) -> None:
        """manifest 已指向共用資料夾時，補寫還缺的新 key 報告狀態檔。"""
        prefix = str(data.get("drive_prefix") or "").strip()
        if not prefix:
            return
        for path in self.report_state_paths(job_id):
            state = self.read_json(path)
            if not isinstance(state, dict):
                continue
            if str(state.get("folder_id") or "") == self.shared_id:
                continue
            self.write_report_state(token, job_id, path, state, prefix, strict=False)

    # ---- 每個舊資料夾 -------------------------------------------------
    def process_folder(self, token: str, folder: dict[str, Any], prefix: str, job_id: str) -> None:
        name = str(folder.get("name") or "")
        folder_id = str(folder.get("id") or "")
        if job_id in self.trash_jobs:
            self.trash_folder(token, folder, job_id)
            return
        blocked = self.blocked_reason(job_id, folder_id)
        if blocked:
            # 修復階段擋下或本機寫入失敗：整個資料夾不搬、不改寫、不丟。
            self.emit("folder_kept", folder=name, job_id=job_id, reason=blocked)
            return
        if job_id in self.invalid_manifests:
            # manifest 壞掉：不能當成孤兒處理，資料夾原封不動。
            self.emit("folder_kept", folder=name, job_id=job_id, reason="manifest_invalid")
            return
        children = drive_storage._list_children(token, folder_id)
        subfolders = [c for c in children if c.get("mimeType") == drive_storage.FOLDER_MIME]
        files = [c for c in children if c.get("mimeType") != drive_storage.FOLDER_MIME]
        if subfolders:
            self.problems += 1
            self.emit("skipped_subfolders", folder=name, job_id=job_id,
                      subfolders=[c.get("name") for c in subfolders])
            return
        shared_children = drive_storage._list_children(token, self.shared_id) if self.shared_id else []
        plan = self.plan_files(files, prefix, shared_children, folder_id)
        duplicate_ids = {item["id"] for item in plan["duplicates"]}
        if plan["conflicts"]:
            # 先對整個資料夾做衝突預檢：有任何一個衝突就一個檔都不搬。
            self.problems += len(plan["conflicts"])
            for conflict in plan["conflicts"]:
                self.emit("name_conflict", folder=name, job_id=job_id, file=conflict["file"],
                          new_name=conflict["new_name"], existing=conflict["existing"],
                          reason=conflict.get("reason", "name_conflict"))
            self.emit("folder_kept", folder=name, job_id=job_id, reason="name_conflict")
            return
        for item in files:
            file_id = str(item.get("id") or "")
            if file_id in duplicate_ids:
                continue  # 內容完全相同：丟垃圾桶視同已處理，不搬。
            new_name = self.drive_name(str(item.get("name") or ""), prefix)
            if not self.apply:
                self.emit("plan_move", folder=name, job_id=job_id, file_id=file_id,
                          file=item.get("name"), new_name=new_name, prefix=prefix)
                continue
            drive_storage._move_file(token, file_id, new_name, self.shared_id, folder_id)
            remote = drive_storage._get_file(token, file_id)
            verified = bool(remote and (remote.get("parents") or []) == [self.shared_id]
                            and str(remote.get("name") or "") == new_name
                            and remote.get("sha256Checksum") == item.get("sha256Checksum"))
            if not verified:
                self.problems += 1
                self.emit("move_failed", folder=name, job_id=job_id, file_id=file_id, new_name=new_name)
                self.emit("folder_kept", folder=name, job_id=job_id, reason="move_failed")
                return
            self.emit("moved", folder=name, job_id=job_id, file_id=file_id, new_name=new_name,
                      prefix=prefix)
        for duplicate in plan["duplicates"]:
            if not self.apply:
                self.emit("plan_trash_identical_duplicate", folder=name, job_id=job_id,
                          file_id=duplicate["id"], file=duplicate["name"])
                continue
            drive_storage._trash_file(token, duplicate["id"])
            self.emit("trashed_identical_duplicate", folder=name, job_id=job_id,
                      file_id=duplicate["id"], file=duplicate["name"])
        if not self.apply:
            self.emit("plan_trash_folder", folder=name, job_id=job_id, prefix=prefix)
            self.plan_local(job_id, folder, prefix)
            return
        # 搬完並核對過 → 改寫本機紀錄 → 最後才把清空的舊資料夾丟垃圾桶。
        status = self.rewrite_local(token, job_id, folder, prefix)
        if status == "failed":
            self.emit("folder_kept", folder=name, job_id=job_id, reason="manifest_rewrite_failed")
            return
        if status == "verify_failed":
            self.emit("folder_kept", folder=name, job_id=job_id, reason="manifest_verify_failed")
            return
        if status == "report_state_failed":
            self.emit("folder_kept", folder=name, job_id=job_id, reason="report_state_write_failed")
            return
        remaining = drive_storage._list_children(token, folder_id)
        if remaining:
            self.problems += 1
            self.emit("folder_kept", folder=name, job_id=job_id, reason="not_empty",
                      remaining=[c.get("name") for c in remaining])
            return
        drive_storage._trash_file(token, folder_id)
        self.emit("trashed_folder", folder=name, job_id=job_id, prefix=prefix)

    def plan_files(self, files: list[dict[str, Any]], prefix: str,
                   shared_children: list[dict[str, Any]], folder_id: str) -> dict[str, list[dict]]:
        """衝突預檢：整個資料夾一起判定，不會搬一半。"""
        conflicts: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        referenced = folder_id in set(self.manifest_folders.values())
        for item in files:
            file_id = str(item.get("id") or "")
            new_name = self.drive_name(str(item.get("name") or ""), prefix)
            existing = [c for c in shared_children
                        if str(c.get("name") or "") == new_name and str(c.get("id") or "") != file_id]
            if not existing:
                continue
            if new_name.endswith(analysis_report.NAME):
                # 同一個 job 的多份報告版本本來就同名，照常搬入。
                continue
            digest = str(item.get("sha256Checksum") or "")
            if (not referenced and digest
                    and any(str(c.get("sha256Checksum") or "") == digest for c in existing)):
                if file_id in self.protected_file_ids:
                    # 這個檔案還被本機 manifest 或報告狀態檔引用：不能丟垃圾桶，
                    # 整個資料夾照衝突處理，一個檔都不動。
                    conflicts.append({"file": item.get("name"), "new_name": new_name,
                                      "existing": [c.get("id") for c in existing],
                                      "reason": "protected_file_id"})
                    continue
                # 內容完全相同的重複檔，且沒有 manifest 指著這個舊資料夾：丟垃圾桶視同已處理。
                duplicates.append({"id": file_id, "name": item.get("name"), "new_name": new_name})
                continue
            conflicts.append({"file": item.get("name"), "new_name": new_name,
                              "existing": [c.get("id") for c in existing]})
        return {"conflicts": conflicts, "duplicates": duplicates}

    def protected_in_folder(self, token: str, folder_id: str) -> list[str]:
        """這個資料夾裡還被本機紀錄引用的檔案 id。"""
        if not self.protected_file_ids or not folder_id:
            return []
        try:
            children = drive_storage._list_children(token, folder_id)
        except (RuntimeError, OSError):
            return []
        return sorted({str(child.get("id") or "") for child in children
                       if str(child.get("id") or "") in self.protected_file_ids})

    def trash_folder(self, token: str, folder: dict[str, Any], job_id: str) -> None:
        """整個舊資料夾移入垃圾桶，不搬檔。"""
        folder_id = str(folder.get("id") or "")
        name = str(folder.get("name") or "")
        protected = self.protected_in_folder(token, folder_id)
        if not self.apply:
            self.emit("plan_trash_job", folder=name, job_id=job_id, folder_id=folder_id)
            if protected:
                self.emit("trash_job_protected_files", folder=name, job_id=job_id,
                          file_ids=protected)
            self.plan_local(job_id, folder, "", trashed=True)
            return
        drive_storage._trash_file(token, folder_id)
        self.emit("trashed_job_folder", folder=name, job_id=job_id, folder_id=folder_id)
        if protected:
            # 使用者明確授權整個丟掉：被一起丟掉的受保護檔案要講清楚。
            self.emit("trash_job_protected_files", folder=name, job_id=job_id, file_ids=protected)
        self.mark_manifest_trashed(job_id, folder_id)

    # ---- 本機紀錄 -----------------------------------------------------
    def plan_local(self, job_id: str, folder: dict[str, Any], prefix: str, trashed: bool = False) -> None:
        state, data = self.read_manifest(job_id)
        if state == "invalid":
            self.emit("plan_local", job_id=job_id, manifest="invalid")
            return
        if state != "ok":
            self.emit("plan_local", job_id=job_id, manifest="missing")
            return
        old_folder_id = str(folder.get("id") or "")
        if str(data.get("folder_id") or "") != old_folder_id:
            self.emit("plan_local", job_id=job_id, manifest="other_folder")
            return
        self.emit("plan_manifest_trashed" if trashed else "plan_manifest_rewritten",
                  job_id=job_id, path=str(self.manifest_path(job_id)), mode="dry_run",
                  drive_prefix=prefix, folder_id=self.shared_id)
        for path in self.report_state_paths(job_id):
            state = self.read_json(path)
            if isinstance(state, dict) and str(state.get("folder_id") or "") == old_folder_id:
                self.emit("plan_report_state", job_id=job_id, path=str(path))

    def mark_manifest_trashed(self, job_id: str, folder_id: str) -> None:
        """垃圾桶化後本機 manifest 不再算有效備份。"""
        path = self.manifest_path(job_id)
        state, data = self.read_manifest(job_id)
        if state == "missing":
            self.emit("manifest_missing", job_id=job_id)
            return
        if state == "invalid":
            self.emit("manifest_invalid", job_id=job_id, path=str(path))
            self.block_job(job_id, "local_write_failed")
            return
        if str(data.get("folder_id") or "") != folder_id:
            self.emit("manifest_other_folder", job_id=job_id,
                      manifest_folder=str(data.get("folder_id") or ""))
            return
        if not self.backup(path, job_id):
            self.block_job(job_id, "local_write_failed")
            return
        try:
            pipeline.write_json(path, {**data, "status": "drive_trashed"})
        except OSError as exc:
            self.problems += 1
            self.block_job(job_id, "local_write_failed")
            self.emit("manifest_rewrite_failed", job_id=job_id, path=str(path), error=str(exc))
            return
        self.emit("manifest_trashed", job_id=job_id, path=str(path), status="drive_trashed")

    def rewrite_local(self, token: str, job_id: str, folder: dict[str, Any], prefix: str) -> str:
        """manifest 與報告狀態檔改指向共用資料夾。

        回傳 "rewritten"／"not_referenced"／"missing"／"failed"／"verify_failed"／
        "report_state_failed"；只有後三種算問題，資料夾一律留著。
        """
        old_folder_id = str(folder.get("id") or "")
        path = self.manifest_path(job_id)
        state, data = self.read_manifest(job_id)
        if state == "missing":
            self.emit("manifest_missing", job_id=job_id)
            return "missing"
        if state == "invalid":
            self.emit("manifest_invalid", job_id=job_id, path=str(path))
            return "failed"
        if str(data.get("folder_id") or "") != old_folder_id:
            self.emit("manifest_other_folder", job_id=job_id,
                      manifest_folder=str(data.get("folder_id") or ""))
            return "not_referenced"
        issues = self.verify_manifest_files(token, job_id, data, prefix)
        if issues:
            # manifest 記錄的檔案跟共用資料夾對不上：不能改寫，資料夾也不能丟。
            self.problems += 1
            self.block_job(job_id, "manifest_verify_failed")
            self.emit("manifest_verify_failed", job_id=job_id, path=str(path), issues=issues)
            return "verify_failed"
        try:
            updated = self.rewrite_manifest(path, data, prefix, job_id)
        except OSError as exc:
            self.problems += 1
            self.block_job(job_id, "local_write_failed")
            self.emit("manifest_rewrite_failed", job_id=job_id, path=str(path), error=str(exc))
            return "failed"
        if updated is None:
            self.block_job(job_id, "local_write_failed")
            return "failed"
        self.emit("manifest_rewritten", job_id=job_id, path=str(path), folder_id=self.shared_id,
                  drive_prefix=prefix)
        if self.rewrite_report_states(token, job_id, old_folder_id, prefix) == "failed":
            return "report_state_failed"
        return "rewritten"

    def rewrite_report_states(self, token: str, job_id: str, old_folder_id: str,
                              prefix: str) -> str:
        """報告狀態檔的 key 含 folder_id：依新資料夾重算一份，舊的保留。"""
        result = "ok"
        for path in self.report_state_paths(job_id):
            state = self.read_json(path)
            if not isinstance(state, dict) or str(state.get("folder_id") or "") != old_folder_id:
                continue
            if self.write_report_state(token, job_id, path, state, prefix, strict=True) == "failed":
                result = "failed"
        return result

    def write_report_state(self, token: str, job_id: str, path: Path, state: dict, prefix: str,
                           strict: bool) -> str:
        """寫出指向共用資料夾的新 key 報告狀態檔；回傳處置結果。"""
        report_name = f"{prefix} - {analysis_report.NAME}"
        if not all(state.get(k) for k in ("job_id", "analysis_sha256", "report_sha256")):
            self.emit("report_state_incomplete", job_id=job_id, path=str(path),
                      reason="incomplete_state")
            return "incomplete"
        if state.get("status") != "report_ready" or state.get("verified") is not True:
            self.emit("report_state_incomplete", job_id=job_id, path=str(path),
                      reason="not_uploaded")
            return "incomplete"
        remote = drive_storage._get_file(token, str(state.get("file_id") or ""))
        verified = bool(remote and not remote.get("trashed")
                        and (remote.get("parents") or []) == [self.shared_id]
                        and str(remote.get("name") or "") == report_name
                        and remote.get("sha256Checksum") == str(state.get("report_sha256") or ""))
        if not verified:
            if strict:
                self.problems += 1
                self.emit("report_state_unverified", job_id=job_id, path=str(path))
                return "unverified"
            self.emit("report_state_skipped", job_id=job_id, path=str(path), reason="not_in_shared_folder")
            return "skipped"
        key = hashlib.sha256(json.dumps(
            {k: state[k] for k in ("job_id", "analysis_sha256", "report_sha256")},
            sort_keys=True).encode() + self.shared_id.encode()).hexdigest()
        new_state = {**state, "folder_id": self.shared_id, "file_name": report_name}
        new_path = path.parent / ("drive-" + key + ".json")
        if new_path.exists():
            if self.read_json(new_path) == new_state:
                self.emit("report_state_unchanged", job_id=job_id, path=str(new_path))
                return "unchanged"
            self.problems += 1
            self.emit("report_state_conflict", job_id=job_id, path=str(new_path))
            return "conflict"
        if not self.apply:
            self.emit("plan_report_state", job_id=job_id, path=str(new_path), file_name=report_name)
            return "planned"
        try:
            pipeline.write_json(new_path, new_state)
        except OSError as exc:
            # 本機寫入失敗不能中斷整批：記下來，這個 job 的資料夾留著。
            self.problems += 1
            self.block_job(job_id, "local_write_failed")
            self.emit("report_state_write_failed", job_id=job_id, path=str(new_path),
                      error=str(exc))
            return "failed"
        self.emit("report_state_written", job_id=job_id, path=str(new_path), file_name=report_name)
        return "written"

    # ---- 主流程 -------------------------------------------------------
    def final_report_states(self, token: str) -> None:
        """資料夾階段結束後再補一次報告狀態檔。

        媒體已搬、報告還留在舊資料夾的中斷窗口，一次 --apply 就要補完。
        """
        for path in self.manifest_paths():
            job_id = path.parent.name
            if job_id in self.trash_jobs:
                continue
            state, data = self.read_manifest(job_id)
            if state != "ok":
                continue
            if str(data.get("folder_id") or "") != self.shared_id:
                continue
            self.repair_report_states(token, job_id, data)

    def run(self) -> int:
        token = drive_storage._access_token()
        self.load_manifests()
        self.load_protected_file_ids()
        children = drive_storage._list_children(token, self.parent_id)
        old = [c for c in children
               if c.get("mimeType") == drive_storage.FOLDER_MIME
               and OLD_FOLDER.fullmatch(str(c.get("name") or ""))]
        self.emit("scan", mode="apply" if self.apply else "dry_run", parent_id=self.parent_id,
                  old_folders=[{"id": c.get("id"), "name": c.get("name"),
                                "job_id": OLD_FOLDER.fullmatch(str(c.get("name") or "")).group(2),
                                "created_time": c.get("createdTime")} for c in old])
        job_folders: dict[str, list[dict[str, Any]]] = {}
        for folder in old:
            job_id = OLD_FOLDER.fullmatch(str(folder.get("name") or "")).group(2)
            job_folders.setdefault(job_id, []).append(folder)
        # --trash-job 找不到要在「沒有舊資料夾就提早結束」之前就回報。
        for job_id in sorted(self.trash_jobs):
            if job_id in job_folders:
                continue
            if self.manifest_status(job_id) == "drive_trashed":
                self.emit("trash_job_already_trashed", job_id=job_id)
                continue
            self.problems += 1
            self.emit("trash_job_not_found", job_id=job_id)
        self.shared_id, self.shared_url = self.shared_folder(token, needed=bool(old))
        self.repair_phase(token)
        if not old:
            self.final_report_states(token)
            self.emit("done", old_folders=0, problems=self.problems)
            return 1 if self.problems else 0
        prefixes = self.assign_prefixes(old)
        for folder in sorted(old, key=lambda c: (str(c.get("name") or ""), str(c.get("id") or ""))):
            match = OLD_FOLDER.fullmatch(str(folder.get("name") or ""))
            job_id = match.group(2)
            try:
                self.process_folder(token, folder, prefixes[str(folder.get("id"))], job_id)
            except OSError as exc:
                # 本機寫入失敗不能中斷整批：這個資料夾留著，其他 job 繼續。
                self.problems += 1
                self.block_job(job_id, "local_write_failed")
                self.emit("folder_kept", folder=str(folder.get("name") or ""), job_id=job_id,
                          reason="local_write_failed", error=str(exc))
        self.final_report_states(token)
        self.emit("done", old_folders=len(old), problems=self.problems)
        return 1 if self.problems else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pipeline.load_dotenv(Path(".env"))
    migration = Migration(Path(args.jobs_root), {str(job) for job in args.trash_job}, bool(args.apply))
    return migration.run()


if __name__ == "__main__":
    raise SystemExit(main())
