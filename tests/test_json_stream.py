import importlib.util
import json
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "omni_json_stream_tests", Path(__file__).parents[1] / "helpers/omni_json_stream.py")
STREAM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STREAM)


def leaves(value, path=()):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            result.update(leaves(item, path + (key,)))
        return result
    if isinstance(value, list):
        result = {}
        for index, item in enumerate(value):
            result.update(leaves(item, path + (index,)))
        return result
    return {path: value}


def event_leaves(events):
    result = {}
    for event in events:
        path = tuple(event["path"])
        if event["kind"] == "string_start":
            result[path] = ""
        elif event["kind"] == "string_chunk":
            result[path] += event["value"]
        elif event["kind"] == "scalar":
            result[path] = event["value"]
    return result


class StreamingJSONTests(unittest.TestCase):
    def parse(self, encoded, chunk_size=7, checkpoint=False, **options):
        parser = STREAM.IncrementalJSONParser(**options)
        events = []
        for offset in range(0, len(encoded), chunk_size):
            events.extend(parser.feed(encoded[offset:offset + chunk_size]))
            self.assertFalse(parser.complete)
            if checkpoint:
                parser = STREAM.IncrementalJSONParser.restore(json.loads(json.dumps(parser.snapshot())))
        events.extend(parser.feed(final=True))
        self.assertTrue(parser.complete)
        self.assertEqual(sum(event["kind"] == "complete" for event in events), 1)
        return parser, events

    def test_fragmented_events_match_standard_json_leaves_and_never_emit_keys(self):
        value = {"message": {"role": "assistant", "content": [
            {"type": "text", "text": "Hello"}, {"type": "tool_result", "content": "Output"}]},
            "count": -12.25e2, "flags": [True, False, None, 0, -9], "empty": "", "objects": [{}, []]}
        encoded = json.dumps(value).encode()
        _, events = self.parse(encoded, chunk_size=1, checkpoint=True)
        self.assertEqual(event_leaves(events), leaves(json.loads(encoded)))
        starts = [tuple(event["path"]) for event in events if event["kind"] == "string_start"]
        self.assertNotIn(("message",), starts)
        self.assertIn(("message", "content", 1, "content"), starts)

    def test_giant_output_has_bounded_fragments_and_checkpoint_without_text_history(self):
        parser = STREAM.IncrementalJSONParser(fragment_size=1024)
        parser.feed(b'{"output":"')
        received = 0
        largest_checkpoint = 0
        for _ in range(160):
            events = parser.feed(b"x" * STREAM.MAX_FEED_BYTES)
            self.assertTrue(all(len(event.get("value", "")) <= 1024 for event in events))
            received += sum(len(event["value"]) for event in events if event["kind"] == "string_chunk")
            state = json.loads(json.dumps(parser.snapshot()))
            largest_checkpoint = max(largest_checkpoint, len(json.dumps(state)))
            parser = STREAM.IncrementalJSONParser.restore(state)
        parser.feed(b'"}', final=True)
        self.assertEqual(received, 160 * STREAM.MAX_FEED_BYTES)
        self.assertLess(largest_checkpoint, 2000)
        self.assertTrue(parser.complete)

    def test_utf8_and_escaped_surrogate_pairs_resume_at_every_byte_boundary(self):
        value = {"clé🐈": ["héllo 🌍", "\n\t\b\f\r/\\\"", "𝄞"]}
        for ascii_only in (False, True):
            with self.subTest(ascii_only=ascii_only):
                encoded = json.dumps(value, ensure_ascii=ascii_only).encode()
                _, events = self.parse(encoded, chunk_size=1, checkpoint=True, fragment_size=1)
                self.assertEqual(event_leaves(events), leaves(value))

    def test_partial_record_and_closed_root_are_not_complete_before_final_validation(self):
        parser = STREAM.IncrementalJSONParser()
        provisional = parser.feed(b'{"text":"evidence"}')
        self.assertFalse(parser.complete)
        self.assertFalse(any(event["kind"] == "complete" for event in provisional))
        with self.assertRaises(STREAM.JSONStreamError):
            parser.feed(b' {"another":true}', final=True)
        self.assertFalse(parser.complete)
        with self.assertRaises(STREAM.JSONStreamError):
            parser.snapshot()

    def test_container_events_have_paths_for_nested_arrays_and_objects(self):
        _, events = self.parse(b'{"items":[{"nested":[[]]},{}]}')
        starts = [(event["kind"], tuple(event["path"])) for event in events if event["kind"].startswith("start_")]
        ends = [(event["kind"], tuple(event["path"])) for event in events if event["kind"].startswith("end_")]
        self.assertIn(("start_array", ("items", 0, "nested", 0)), starts)
        self.assertIn(("end_array", ("items", 0, "nested", 0)), ends)
        self.assertEqual(ends[-1], ("end_object", ()))

    def test_invalid_json_never_emits_completion(self):
        inputs = [b'[]', b'null', b'{"a":}', b'{"a":1,}', b'{"a":[1,]}',
                  b'{"a" 1}', b'{"a":truefalse}', b'{"a":01}', b'{"a":-.1}',
                  b'{"a":1.}', b'{"a":1e+}', b'{"a":NaN}', b'{"a":Infinity}',
                  b'{"a":"bad\\q"}', b'{"a":"line\nline"}', b'{"a":1}{}', b'{"a":[}',
                  b'{"a":"\\ud800"}', b'{"a":"\\udc00"}', b'{"a":"\\ud800\\u0041"}',
                  b'{"a":"\xff"}', b'{"a":"\xf0\x9f"}', b'{"a":"unfinished', b'{"a":tru}']
        for encoded in inputs:
            with self.subTest(encoded=encoded):
                parser = STREAM.IncrementalJSONParser()
                with self.assertRaises(STREAM.JSONStreamError):
                    for offset in range(0, len(encoded), 3):
                        parser.feed(encoded[offset:offset + 3])
                    parser.feed(final=True)
                self.assertFalse(parser.complete)

    def test_depth_keys_scalar_and_path_limits_fail_explicitly(self):
        cases = [(b'{"a":[[]]}', {"max_depth": 2}),
                 (b'{"long":true}', {"max_scalar_chars": 3}),
                 (b'{"n":12345}', {"max_scalar_chars": 4}),
                 (b'{"ab":{"cd":true}}', {"max_path_chars": 3}),
                 (b'{"n":1e999}', {})]
        for encoded, options in cases:
            with self.subTest(encoded=encoded, options=options), self.assertRaises(STREAM.JSONStreamError):
                STREAM.IncrementalJSONParser(**options).feed(encoded, final=True)

    def test_restored_number_and_literal_finish_with_original_paths(self):
        for encoded in (b'{"n":-123.456e+2}', b'{"n":false}', b'{"n":null}'):
            with self.subTest(encoded=encoded):
                _, events = self.parse(encoded, chunk_size=1, checkpoint=True)
                self.assertEqual(event_leaves(events), leaves(json.loads(encoded)))

    def test_text_feeds_work_and_byte_text_mixing_cannot_bypass_utf8_validation(self):
        _, events = self.parse('{"é":"🍞"}', chunk_size=2, checkpoint=True)
        self.assertEqual(event_leaves(events), {("é",): "🍞"})
        parser = STREAM.IncrementalJSONParser()
        parser.feed(b'{"a":"\xf0')
        with self.assertRaises(STREAM.JSONStreamError):
            parser.feed('"}', final=True)

    def test_feed_limit_is_byte_based_and_parser_reuse_cannot_add_another_record(self):
        for payload in (b" " * (STREAM.MAX_FEED_BYTES + 1), "🌍" * (STREAM.MAX_FEED_BYTES // 4 + 1)):
            with self.subTest(kind=type(payload)), self.assertRaises(STREAM.JSONStreamError):
                STREAM.IncrementalJSONParser().feed(payload)
        parser = STREAM.IncrementalJSONParser()
        parser.feed(b"{}", final=True)
        self.assertEqual(parser.feed(final=True), [])
        with self.assertRaises(STREAM.JSONStreamError):
            parser.feed(b"{}")

    def test_checkpoint_is_independent_and_invalid_checkpoint_is_rejected(self):
        parser = STREAM.IncrementalJSONParser()
        parser.feed(b'{"a":["part')
        state = parser.snapshot()
        altered = json.loads(json.dumps(state))
        altered["stack"][0]["state"] = "invented"
        with self.assertRaises(STREAM.JSONStreamError):
            STREAM.IncrementalJSONParser.restore(altered)
        self.assertEqual(parser.snapshot(), state)
        state["decoder_bytes"] = "00" * 4
        with self.assertRaises(STREAM.JSONStreamError):
            STREAM.IncrementalJSONParser.restore(state)

    def test_checkpoint_cannot_forge_pending_ascii_or_oversized_value_fragment(self):
        parser = STREAM.IncrementalJSONParser(fragment_size=1)
        parser.feed(b'{"a":"part')
        state = parser.snapshot()
        for alter in (lambda value: value.update(decoder_bytes="61"),
                      lambda value: value["token"].update(buffer="oversized"),
                      lambda value: value.update(root="done"),
                      lambda value: value["token"].pop("high"),
                      lambda value: value["stack"][0].pop("label")):
            changed = json.loads(json.dumps(state))
            alter(changed)
            with self.subTest(checkpoint=changed), self.assertRaises(STREAM.JSONStreamError):
                STREAM.IncrementalJSONParser.restore(changed)


if __name__ == "__main__":
    unittest.main()
