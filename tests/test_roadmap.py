"""Check issue #7 snapshot coverage, not implementation of its release gates."""

import re
import unittest
from pathlib import Path


class RoadmapSnapshotTests(unittest.TestCase):
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
