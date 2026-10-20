"""Check issue #7 snapshot coverage, not implementation of its release gates."""

import re
import unittest
from pathlib import Path


class RoadmapSnapshotTests(unittest.TestCase):
    def test_existing_capabilities_and_non_rework_scope_are_preserved(self) -> None:
        roadmap = (Path(__file__).resolve().parents[1] / "docs/roadmap-v0.6.md").read_text(encoding="utf-8")
        inventory = re.search(r"(?ms)^## 既有能力與非重做範圍\n(.*?)(?=^## |\Z)", roadmap)
        self.assertIsNotNone(inventory, "The snapshot must distinguish existing capabilities from planned work")
        for contract in (
            "下載／轉錄／畫面分析／摘要與分層快取",
            "FTS5", "短中文字詞", "分類", "時間戳與來源引用",
            "Ask（`--ask`）由 MiniMax 回答", "OpenRouter 用於 embedding",
            "本機 BGE-M3", "rerank 尚未接入", "RSS 佇列／重試／dead-letter／single writer",
            "`--digest` 會呼叫 MiniMax", "本機 Digest 預覽只組裝既有 summary",
            "沿用既有能力，不重做",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, inventory.group(1))

    def test_storage_contract_preserves_legacy_sql_compatibility(self) -> None:
        roadmap = (Path(__file__).resolve().parents[1] / "docs/roadmap-v0.6.md").read_text(encoding="utf-8")
        storage = re.search(r"(?ms)^- \*\*F0\.1\b(.*?)(?=^- \*\*F0\.|^## |\Z)", roadmap)
        self.assertIsNotNone(storage)
        for contract in (
            "`operations.db` 保存永久操作帳", "`library.db` 是可重建的搜尋投影",
            "legacy `DROP`／`REPLACE`", "positional SQL", "顯式欄位清單",
            "不得把永久操作資料接到搜尋表", "舊資料升級／重建回歸", "保留有效向量",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, storage.group(1))

    def test_foundation_and_delivery_dependencies_are_preserved(self) -> None:
        roadmap = (Path(__file__).resolve().parents[1] / "docs/roadmap-v0.6.md").read_text(encoding="utf-8")
        self.assertRegex(roadmap, r"(?m)^## F0\b")
        for package in ("F0.1", "F0.2", "F0.3", "F0.4", "F0.5"):
            self.assertIn(package, roadmap)
        delivery = re.search(r"(?ms)^## [^\n]*依賴[^\n]*\n(.*?)(?=^## |\Z)", roadmap)
        self.assertIsNotNone(delivery, "The snapshot must include the delivery dependencies")
        for package in ("#4 C0–C3", "#2 N0–N5", "#4 C4–C8", "N6", "N8", "#3 S0–S7", "N7", "#5 A0–A10", "#6 W0", "W8", "W7"):
            with self.subTest(package=package):
                self.assertIn(package, delivery.group(1))

    def test_release_gates_preserve_work_packages_and_safety_conditions(self) -> None:
        roadmap = (Path(__file__).resolve().parents[1] / "docs/roadmap-v0.6.md").read_text(encoding="utf-8")
        gates = dict(re.findall(r"^\| (v0\.\d+a?) \|(.+)$", roadmap, re.MULTILINE))
        required = {
            "v0.3a": ("N0–N6", "N8", "preview 外部 API 呼叫=0", "unknown", "accepted", "delivered", "exactly-once"),
            "v0.3": ("C0–C8", "S0–S7", "N7", "hard", "sent reservation", "null", "cutoff", "shadow"),
            "v0.4": ("A0–A10", "hybrid", "引用", "向量", "CLI"),
            "v0.5": ("W0–W11", "W8", "W7", "DDL", "CSRF", "authentication", "unknown", "browser"),
        }
        self.assertEqual(set(gates), set(required), "The snapshot must preserve every pre-v0.6 release gate")
        for version, markers in required.items():
            for marker in markers:
                with self.subTest(version=version, marker=marker):
                    self.assertIn(marker, gates[version])
