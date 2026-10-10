"""A full run against a live hub, over HTTP, exactly as the CLI drives it."""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from duet import client
from duet.client import HubError
from duet.hub import Store, serve


class Live(unittest.TestCase):
    sends = 2
    conversations = 2

    def setUp(self):
        self.store = Store("test task", self.sends, self.conversations, None)
        self.httpd = serve(self.store, "127.0.0.1", 0)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def say(self, role, text):
        return client.request(self.url, "/api/say", {"role": role, "text": text})

    def test_full_run(self):
        seen = 0
        for conv in (1, 2):
            for send in (1, 2):
                res = self.say("master", "m%d.%d" % (conv, send))
                self.assertEqual(res["conversation"], conv)
                self.assertEqual(res["sends_used"], send)

                got = client.request(
                    self.url, "/api/wait?role=slave&after=%d&timeout=2" % seen)
                self.assertEqual(got["message"]["text"], "m%d.%d" % (conv, send))
                self.assertEqual(got["message"]["new_conversation"],
                                 conv == 2 and send == 1)
                seen = got["message"]["seq"]

                self.say("slave", "s%d.%d" % (conv, send))
                back = client.request(
                    self.url, "/api/wait?role=master&after=%d&timeout=2" % seen)
                self.assertEqual(back["message"]["text"], "s%d.%d" % (conv, send))
                seen = back["message"]["seq"]

        state = client.request(self.url, "/api/state")
        self.assertEqual(state["status"], "complete")
        self.assertEqual(len(state["convs"]), 2)
        self.assertEqual([len(c["messages"]) for c in state["convs"]], [4, 4])
        self.assertTrue(all(c["closed"] for c in state["convs"]))

    def test_master_cannot_speak_twice(self):
        self.say("master", "one")
        with self.assertRaises(HubError) as e:
            self.say("master", "two")
        self.assertIn("slave's turn", str(e.exception))

    def test_slave_cannot_open(self):
        with self.assertRaises(HubError):
            self.say("slave", "hello?")

    def test_nothing_after_complete(self):
        for _ in range(self.conversations):
            for _ in range(self.sends):
                self.say("master", "m")
                self.say("slave", "s")
        with self.assertRaises(HubError) as e:
            self.say("master", "one more")
        self.assertIn("complete", str(e.exception))

    def test_wait_returns_complete_not_a_hang(self):
        for _ in range(self.conversations):
            for _ in range(self.sends):
                self.say("master", "m")
                self.say("slave", "s")
        got = client.request(self.url, "/api/wait?role=slave&after=99&timeout=2")
        self.assertEqual(got["status"], "complete")
        self.assertIsNone(got["message"])

    def test_early_next_conversation(self):
        self.say("master", "m")
        self.say("slave", "s")
        state = client.request(self.url, "/api/next", {})
        self.assertEqual(state["conversation"], 2)
        self.assertEqual(state["sends_used"], 0)
        res = self.say("master", "fresh")
        self.assertEqual(res["conversation"], 2)

    def test_empty_message_refused(self):
        with self.assertRaises(HubError):
            self.say("master", "   ")

    def test_page_is_served(self):
        import urllib.request
        with urllib.request.urlopen(self.url + "/") as r:
            body = r.read().decode("utf-8")
        self.assertIn("<title>Duet</title>", body)
        self.assertIn("/api/state", body)

    def test_transcript_written(self):
        import json
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "t.json")
        self.store.transcript = path
        self.say("master", "hello")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["convs"][0]["messages"][0]["text"], "hello")


if __name__ == "__main__":
    unittest.main(verbosity=2)
