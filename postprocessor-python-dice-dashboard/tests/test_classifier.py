"""
Unit tests for dice classification, NMS, label parsing, and detection parsing.

Covers all §5.2 edge cases from the spec, plus D1 (always-3-dice invariant).
Run with:  pytest tests/
"""
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock

# ── Stub nxai_communication_utils so the main module imports without C lib ──────
sys.modules.setdefault("nxai_communication_utils", MagicMock())

# ── Load the main module (file name contains hyphens so importlib is required) ──
_here    = os.path.dirname(__file__)
_mod_path = os.path.join(_here, "..", "postprocessor-python-dice-dashboard.py")
spec = importlib.util.spec_from_file_location("dice_dashboard", _mod_path)
mod  = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

classify            = mod.classify
_label_to_value     = mod._label_to_value
_iou                = mod._iou
_nms                = mod._nms
parse_dice_detections = mod.parse_dice_detections

FIXTURES = json.load(
    open(os.path.join(_here, "fixtures", "sample_detections.json"))
)


# ── classify ────────────────────────────────────────────────────────────────────

class TestClassify:

    # Boundary totals
    def test_total_3_is_small(self):
        cat, triple = classify([1, 1, 1])
        assert cat == "Triple"    # [1,1,1] hits Triple first
        assert triple is True

    def test_total_3_non_triple(self):
        # total=3 with non-identical values is impossible (min is [1,1,1])
        # but verify classify handles gracefully if somehow given e.g. [1,1,1]
        cat, triple = classify([1, 1, 1])
        assert cat == "Triple"

    def test_min_non_triple_small(self):
        # [1, 1, 2] → total=4, Small
        cat, triple = classify([1, 1, 2])
        assert cat == "Small"
        assert triple is False

    def test_total_9_is_small(self):
        cat, triple = classify([3, 3, 3])
        assert cat == "Triple"    # all same → Triple

    def test_total_9_not_triple(self):
        # [2, 3, 4] → total=9, Small
        cat, triple = classify([2, 3, 4])
        assert cat == "Small"
        assert triple is False

    def test_total_10_is_big(self):
        # [2, 4, 4] → total=10, Big
        cat, triple = classify([2, 4, 4])
        assert cat == "Big"
        assert triple is False

    def test_total_18_is_big(self):
        cat, triple = classify([6, 6, 6])
        assert cat == "Triple"    # all same → Triple

    def test_total_18_not_triple(self):
        # [5, 6, 7] — but 7 is invalid; use [4, 6, 8] — invalid.
        # Max non-triple Big: [5, 6, 5] = 16, or [4, 6, 6] = 16
        # [5, 6, 6] → total=17, Big (not triple: 5 ≠ 6)
        cat, triple = classify([5, 6, 6])
        assert cat == "Big"
        assert triple is False

    # Triple detection
    def test_triple_ones(self):
        cat, triple = classify([1, 1, 1])
        assert cat == "Triple"
        assert triple is True

    def test_triple_sixes(self):
        cat, triple = classify([6, 6, 6])
        assert cat == "Triple"
        assert triple is True

    def test_triple_fours(self):
        cat, triple = classify([4, 4, 4])
        assert cat == "Triple"
        assert triple is True

    def test_not_triple_two_same(self):
        # [2, 2, 5] → total=9, Small — NOT triple because 5 ≠ 2
        cat, triple = classify([2, 2, 5])
        assert cat == "Small"
        assert triple is False

    def test_not_triple_all_different(self):
        cat, triple = classify([1, 3, 5])
        assert cat == "Small"    # total=9
        assert triple is False

    # D1: wrong dice count
    def test_zero_dice(self):
        cat, triple = classify([])
        assert cat == "Unknown"
        assert triple is False

    def test_one_die(self):
        cat, triple = classify([4])
        assert cat == "Unknown"
        assert triple is False

    def test_two_dice(self):
        cat, triple = classify([3, 6])
        assert cat == "Unknown"
        assert triple is False

    def test_four_dice(self):
        cat, triple = classify([1, 2, 3, 4])
        assert cat == "Unknown"
        assert triple is False

    def test_five_dice(self):
        cat, triple = classify([1, 2, 3, 4, 5])
        assert cat == "Unknown"
        assert triple is False

    # All Small values
    def test_all_small_totals(self):
        small_combos = [
            [1, 1, 1], [1, 1, 2], [1, 2, 3], [2, 2, 3], [2, 3, 4], [3, 3, 3],
        ]
        for vals in small_combos:
            cat, _ = classify(vals)
            if len(set(vals)) == 1:
                assert cat == "Triple", f"Expected Triple for {vals}"
            else:
                assert cat == "Small",  f"Expected Small for {vals}, got {cat}"

    # All Big values
    def test_all_big_totals(self):
        big_combos = [
            [4, 3, 3], [5, 3, 2], [4, 4, 2], [6, 2, 2], [5, 5, 1],
            [6, 3, 1], [5, 4, 1], [6, 4, 1], [5, 4, 2], [6, 5, 1],
            [6, 5, 2], [6, 5, 3], [6, 5, 4], [6, 5, 5], [6, 6, 1],
            [6, 6, 2], [6, 6, 3], [6, 6, 4], [6, 6, 5],
        ]
        for vals in big_combos:
            cat, triple = classify(vals)
            if len(set(vals)) == 1:
                assert cat == "Triple"
            else:
                assert cat == "Big",  f"Expected Big for {vals} (sum={sum(vals)}), got {cat}"


# ── _label_to_value ─────────────────────────────────────────────────────────────

class TestLabelToValue:

    def test_plain_integers(self):
        for i in range(1, 7):
            assert _label_to_value(str(i)) == i

    def test_dice_prefix(self):
        for i in range(1, 7):
            assert _label_to_value(f"dice_{i}") == i

    def test_die_prefix(self):
        assert _label_to_value("die_3") == 3

    def test_pip_prefix(self):
        assert _label_to_value("pip_6") == 6

    def test_face_prefix(self):
        assert _label_to_value("face_2") == 2

    def test_case_insensitive(self):
        assert _label_to_value("DICE_4") == 4

    def test_out_of_range(self):
        assert _label_to_value("0")  is None
        assert _label_to_value("7")  is None
        assert _label_to_value("-1") is None

    def test_non_numeric(self):
        assert _label_to_value("person") is None
        assert _label_to_value("car")    is None

    def test_integer_input(self):
        assert _label_to_value(4) == 4


# ── _iou ────────────────────────────────────────────────────────────────────────

class TestIoU:

    def test_identical_boxes(self):
        assert _iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0

    def test_no_overlap(self):
        assert _iou([0, 0, 5, 5], [10, 10, 15, 15]) == 0.0

    def test_partial_overlap(self):
        # Box A: [0,0,10,10] area=100; Box B: [5,5,15,15] area=100
        # Intersection: [5,5,10,10] area=25; Union = 100+100-25=175
        result = _iou([0, 0, 10, 10], [5, 5, 15, 15])
        assert abs(result - 25/175) < 1e-6

    def test_one_inside_other(self):
        # A completely inside B
        result = _iou([2, 2, 8, 8], [0, 0, 10, 10])
        # Intersection=36, A_area=36, B_area=100 → union=100
        assert abs(result - 36/100) < 1e-6


# ── _nms ────────────────────────────────────────────────────────────────────────

class TestNMS:

    def _det(self, conf, bbox):
        return {"confidence": conf, "bbox": bbox, "value": 1}

    def test_empty(self):
        assert _nms([], 0.5) == []

    def test_single_box(self):
        d = self._det(0.9, [0, 0, 10, 10])
        assert _nms([d], 0.5) == [d]

    def test_keeps_best_overlapping(self):
        high = self._det(0.9, [0, 0, 10, 10])
        low  = self._det(0.4, [1, 1, 11, 11])
        kept = _nms([low, high], 0.5)
        assert len(kept) == 1
        assert kept[0]["confidence"] == 0.9

    def test_keeps_non_overlapping(self):
        a = self._det(0.9, [0,   0,  10, 10])
        b = self._det(0.8, [50, 50,  60, 60])
        kept = _nms([a, b], 0.5)
        assert len(kept) == 2

    def test_sample_fixture_nms(self):
        msg = FIXTURES["overlapping_boxes_nms"]
        dets = parse_dice_detections(msg, conf_threshold=0.5, nms_iou=0.5)
        assert len(dets) == 1
        assert dets[0]["value"] == 6
        assert dets[0]["confidence"] == 0.92


# ── parse_dice_detections ───────────────────────────────────────────────────────

class TestParseDiceDetections:

    def test_three_dice_small(self):
        msg  = FIXTURES["three_dice_small"]
        dets = parse_dice_detections(msg, 0.5, 0.5)
        assert len(dets) == 3
        values = [d["value"] for d in dets]
        assert sorted(values) == [1, 3, 5]

    def test_three_dice_big(self):
        msg  = FIXTURES["three_dice_big"]
        dets = parse_dice_detections(msg, 0.5, 0.5)
        assert len(dets) == 3
        assert sorted(d["value"] for d in dets) == [4, 5, 6]

    def test_low_confidence_filtered(self):
        msg  = FIXTURES["low_confidence"]
        dets = parse_dice_detections(msg, conf_threshold=0.5, nms_iou=0.5)
        assert len(dets) == 0

    def test_low_confidence_passes_lower_threshold(self):
        msg  = FIXTURES["low_confidence"]
        dets = parse_dice_detections(msg, conf_threshold=0.2, nms_iou=0.5)
        assert len(dets) == 3

    def test_empty_bboxes(self):
        dets = parse_dice_detections({}, 0.5, 0.5)
        assert dets == []

    def test_sorted_left_to_right(self):
        msg  = FIXTURES["three_dice_small"]
        dets = parse_dice_detections(msg, 0.5, 0.5)
        x1s  = [d["bbox"][0] for d in dets]
        assert x1s == sorted(x1s)

    def test_two_dice_returns_two(self):
        msg  = FIXTURES["two_dice_only"]
        dets = parse_dice_detections(msg, 0.5, 0.5)
        assert len(dets) == 2
        cat, triple = classify([d["value"] for d in dets])
        assert cat == "Unknown"
        assert triple is False

    def test_triple_fixture(self):
        msg  = FIXTURES["triple_threes"]
        dets = parse_dice_detections(msg, 0.5, 0.5)
        assert len(dets) == 3
        values = [d["value"] for d in dets]
        cat, triple = classify(values)
        assert cat == "Triple"
        assert triple is True

    def test_no_confidence_falls_back_to_1(self):
        msg = {
            "BBoxes_xyxy": {"4": [0.0, 0.0, 60.0, 60.0]},
            "ObjectsMetaData": {},
        }
        dets = parse_dice_detections(msg, conf_threshold=0.5, nms_iou=0.5)
        assert len(dets) == 1
        assert dets[0]["confidence"] == 1.0


# ── Integration: full classify flow via fixtures ────────────────────────────────

class TestIntegration:

    def _run(self, fixture_name, conf=0.5, nms=0.5):
        msg  = FIXTURES[fixture_name]
        dets = parse_dice_detections(msg, conf, nms)
        vals = [d["value"] for d in dets]
        return classify(vals)

    def test_fixture_small(self):
        cat, triple = self._run("three_dice_small")
        assert cat == "Small"
        assert triple is False

    def test_fixture_big(self):
        cat, triple = self._run("three_dice_big")
        assert cat == "Big"
        assert triple is False

    def test_fixture_triple(self):
        cat, triple = self._run("triple_threes")
        assert cat == "Triple"
        assert triple is True

    def test_fixture_wrong_count(self):
        cat, triple = self._run("two_dice_only")
        assert cat == "Unknown"
        assert triple is False
