import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path


os.environ.setdefault("HERMES_REMOTE_URL", "http://127.0.0.1:9")
MODULE_PATH = Path(__file__).resolve().parents[1] / "backend.py"
SPEC = importlib.util.spec_from_file_location("hermes_web_backend", MODULE_PATH)
backend = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = backend
SPEC.loader.exec_module(backend)


class BackendUnitTests(unittest.TestCase):
    def test_conversation_ids_are_strict_uuids(self):
        self.assertTrue(backend._valid_id(str(uuid.uuid4())))
        self.assertFalse(backend._valid_id("../../config.yaml"))
        self.assertFalse(backend._valid_id("not-an-id"))

    def test_token_comparison(self):
        original = backend.AUTH_TOKEN
        try:
            backend.AUTH_TOKEN = "correct-horse"
            self.assertTrue(backend._token_ok("correct-horse"))
            self.assertFalse(backend._token_ok("wrong"))
        finally:
            backend.AUTH_TOKEN = original

    def test_media_roots_expand_and_resolve(self):
        original = os.environ.get("HERMES_MEDIA_ROOTS")
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            os.environ["HERMES_MEDIA_ROOTS"] = os.pathsep.join([first, second])
            self.assertEqual(backend._media_roots(), [Path(first).resolve(), Path(second).resolve()])
        if original is None:
            os.environ.pop("HERMES_MEDIA_ROOTS", None)
        else:
            os.environ["HERMES_MEDIA_ROOTS"] = original

    def test_pending_interaction_resolves_from_websocket_response(self):
        session = backend.LocalSession()
        session.send_from_worker = lambda *_args, **_kwargs: None
        result = []

        worker = threading.Thread(
            target=lambda: result.append(session.request_human("approval_required", {}, timeout=1))
        )
        worker.start()
        for _ in range(100):
            if session.pending:
                break
            time.sleep(0.005)
        request_id = next(iter(session.pending))
        self.assertTrue(session.resolve_human(request_id, "once"))
        worker.join(timeout=1)
        self.assertEqual(result, ["once"])


if __name__ == "__main__":
    unittest.main()
