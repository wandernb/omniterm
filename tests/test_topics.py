import importlib.util
import pathlib
import unittest


HELPERS = pathlib.Path(__file__).resolve().parents[1] / "helpers"
spec = importlib.util.spec_from_file_location("omni_topics", HELPERS / "omni_topics.py")
TOPICS = importlib.util.module_from_spec(spec)
spec.loader.exec_module(TOPICS)


def community(members, concepts=("file:a.py",)):
    return {"members": list(members), "concepts": list(concepts)}


class CarryOverTests(unittest.TestCase):
    def test_new_topics_get_ids_from_members(self):
        topics = TOPICS.carry_over([], [community(["b", "a"])])
        self.assertEqual(topics[0]["id"], TOPICS.topic_id(["a", "b"]))
        self.assertEqual(topics[0]["members"], ["a", "b"])
        self.assertIsNone(topics[0]["name"])

    def test_overlapping_topic_keeps_id_and_name(self):
        old = TOPICS.carry_over([], [community(["a", "b", "c", "d"])])
        TOPICS.apply_name(old, old[0]["id"], "Vector search", "Exact scan work.", now=1.0)
        new = TOPICS.carry_over(old, [community(["a", "b", "c", "e"])])
        self.assertEqual(new[0]["id"], old[0]["id"])
        self.assertEqual(new[0]["name"], "Vector search")
        self.assertEqual(new[0]["named_members"], ["a", "b", "c", "d"])

    def test_low_overlap_gets_a_new_id(self):
        old = TOPICS.carry_over([], [community(["a", "b", "c", "d"])])
        new = TOPICS.carry_over(old, [community(["a", "x", "y", "z"])])
        self.assertNotEqual(new[0]["id"], old[0]["id"])

    def test_one_old_topic_is_carried_by_its_best_match_only(self):
        old = TOPICS.carry_over([], [community(["a", "b", "c", "d"])])
        new = TOPICS.carry_over(old, [community(["a", "b", "c"]), community(["a", "b", "c", "d", "e"])])
        carried = [topic for topic in new if topic["id"] == old[0]["id"]]
        self.assertEqual(len(carried), 1)
        self.assertEqual(carried[0]["members"], ["a", "b", "c", "d", "e"])

    def test_order_of_current_communities_does_not_change_ids(self):
        old = TOPICS.carry_over([], [community(["a", "b"]), community(["c", "d"])])
        first = TOPICS.carry_over(old, [community(["a", "b"]), community(["c", "d"])])
        second = TOPICS.carry_over(old, [community(["c", "d"]), community(["a", "b"])])
        self.assertEqual(sorted(t["id"] for t in first), sorted(t["id"] for t in second))


class NamingTests(unittest.TestCase):
    def test_unnamed_and_drifted_topics_are_queued_largest_first(self):
        topics = TOPICS.carry_over([], [community(["a", "b"]), community(["c", "d", "e"])])
        queue = TOPICS.naming_queue(topics)
        self.assertEqual([t["members"] for t in queue], [["c", "d", "e"], ["a", "b"]])
        for topic in topics:
            TOPICS.apply_name(topics, topic["id"], "Name", "Summary")
        self.assertEqual(TOPICS.naming_queue(topics), [])
        topics[0]["members"] = ["a", "x", "y"]
        self.assertEqual([t["id"] for t in TOPICS.naming_queue(topics)], [topics[0]["id"]])

    def test_small_drift_does_not_rename(self):
        topics = TOPICS.carry_over([], [community(list("abcdefghij"))])
        TOPICS.apply_name(topics, topics[0]["id"], "Name", "Summary")
        topics[0]["members"] = list("abcdefghik")
        self.assertFalse(TOPICS.needs_name(topics[0]))

    def test_apply_name_validates_and_bounds(self):
        topics = TOPICS.carry_over([], [community(["a", "b"])])
        with self.assertRaises(ValueError):
            TOPICS.apply_name(topics, topics[0]["id"], " ", "x")
        with self.assertRaises(KeyError):
            TOPICS.apply_name(topics, "topic:missing", "Name", "x")
        topic = TOPICS.apply_name(topics, topics[0]["id"], "N" * 200, "  spaced   out ")
        self.assertEqual(len(topic["name"]), TOPICS.NAME_CHARACTERS)
        self.assertEqual(topic["summary"], "spaced out")

    def test_label_falls_back_to_concepts(self):
        topic = TOPICS.carry_over([], [community(["a", "b"], ("file:x.py", "package:numpy"))])[0]
        self.assertEqual(TOPICS.label(topic), "x.py, numpy")
        TOPICS.apply_name([topic], topic["id"], "Python tooling", "")
        self.assertEqual(TOPICS.label(topic), "Python tooling")


class MergeCandidateTests(unittest.TestCase):
    def test_pairs_above_threshold_best_first_without_decided(self):
        scores = {"a": [("b", 0.95), ("c", 0.86), ("d", 0.5)], "b": [("a", 0.95)], "c": [("a", 0.86)],
                  "d": [("a", 0.5)], "e": [("f", 0.99)]}
        found = TOPICS.merge_candidates(["a", "b", "c", "d"], scores.get, decided=set())
        self.assertEqual([item["pair"] for item in found], [["a", "b"], ["a", "c"]])
        decided = {TOPICS.pair_key("b", "a")}
        found = TOPICS.merge_candidates(["a", "b", "c", "d"], scores.get, decided=decided)
        self.assertEqual([item["pair"] for item in found], [["a", "c"]])

    def test_limit_and_self_pairs(self):
        scores = {"a": [("a", 1.0), ("b", 0.9), ("c", 0.9)], "b": [], "c": []}
        found = TOPICS.merge_candidates(["a", "b", "c"], scores.get, decided=set(), limit=1)
        self.assertEqual(found, [{"pair": ["a", "b"], "score": 0.9}])


if __name__ == "__main__":
    unittest.main()
