"""Deterministic auth/storage boundaries; no provider or database calls."""
import os
import io
import logging
import time
import unittest
import uuid
from unittest.mock import MagicMock, Mock, patch

from flask import Flask, g

from sceneit.auth import (
    ISSUER, SESSION_COOKIE, _safe_return_to, _session_from_cookie,
    _verified_claims, auth_bp, current_user, require_csrf, require_owner,
)
from sceneit.private_storage import (
    _parts, _range, cancel_upload_session, decrypt_upload_session,
    delete_object, download_object, encrypt_upload_session, object_info,
    open_private, reserve_upload,
)


class AuthBoundaryTests(unittest.TestCase):
    def test_return_path_cannot_escape_origin(self):
        self.assertEqual(_safe_return_to("/imports/1?tab=file"), "/imports/1?tab=file")
        for value in (
            "https://evil.invalid/", "//evil.invalid/x", r"/\evil", "",
            "/%5cevil", "/%255cevil", "/%2f%2fevil", "/ok%0d%0aLocation:x",
            "/bad%zz",
        ):
            with self.subTest(value=value):
                self.assertEqual(_safe_return_to(value), "/")

    def test_anonymous_user_does_not_need_database(self):
        app = Flask(__name__)
        app.register_blueprint(auth_bp)
        with app.test_request_context("/api/auth/user"):
            g.auth_session = None
            response = current_user()
            self.assertEqual(response.get_json(), {
                "user": None, "csrfToken": None, "pilotAdmitted": False,
            })

    def test_owner_and_csrf_come_only_from_verified_session(self):
        app = Flask(__name__)
        with app.test_request_context(
            "/api/import", method="POST",
            headers={"X-CSRF-Token": "csrf-value"},
        ):
            g.auth_session = {
                "user_id": "owner-1", "csrf_token": "csrf-value"
            }
            self.assertEqual(require_owner(), "owner-1")
            self.assertIsNone(require_csrf())

    def test_client_owner_header_does_not_authenticate(self):
        app = Flask(__name__)
        with app.test_request_context(
            "/api/import", headers={"X-Owner-ID": "attacker"}
        ):
            g.auth_session = None
            with self.assertRaises(Exception) as caught:
                require_owner()
            self.assertEqual(caught.exception.code, 401)

    def test_malformed_session_cookie_is_anonymous_without_database(self):
        app = Flask(__name__)
        for value in ("é" * 40, "x" * 129, "not valid spaces"):
            with app.test_request_context(
                "/", headers={"Cookie": f"{SESSION_COOKIE}={value}"}
            ), patch("sceneit.auth.connection") as database:
                self.assertIsNone(_session_from_cookie())
                database.assert_not_called()

    def test_signed_id_token_validates_all_oidc_boundaries(self):
        from authlib.jose import JsonWebKey, JsonWebToken

        key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
        public_set = {"keys": [key.as_dict(is_private=False)]}
        now = int(time.time())

        def token(**changes):
            claims = {
                "iss": ISSUER, "aud": "sceneit-client", "sub": "owner-1",
                "nonce": "nonce-1", "iat": now, "exp": now + 300,
            }
            claims.update(changes)
            return JsonWebToken(["RS256"]).encode(
                {"alg": "RS256", "kid": key.kid}, claims, key
            )

        response = Mock()
        response.json.return_value = public_set
        response.raise_for_status.return_value = None
        metadata = {"issuer": ISSUER, "jwks_uri": "https://issuer.invalid/jwks"}
        environment = {"REPL_ID": "sceneit-client"}
        with patch.dict(os.environ, environment), \
                patch("sceneit.auth._discovery", return_value=metadata), \
                patch("sceneit.auth.httpx.get", return_value=response):
            self.assertEqual(
                _verified_claims(token(), "nonce-1")["sub"], "owner-1"
            )
            metadata["issuer"] = ISSUER + "/"
            self.assertEqual(_verified_claims(token(iss=ISSUER + "/"), "nonce-1")["sub"], "owner-1")
            with self.assertRaises(Exception):
                _verified_claims(token(), "nonce-1")
            metadata["issuer"] = ISSUER
            invalid = (
                token(iss="https://wrong.invalid"),
                token(aud="wrong-client"),
                token(nonce="wrong-nonce"),
                token(exp=now - 300),
                token(aud=["sceneit-client", "other"], azp="other"),
                token(sub=""),
            )
            for encoded in invalid:
                with self.subTest(encoded=encoded[:10]), self.assertRaises(Exception):
                    _verified_claims(encoded, "nonce-1")

            other_key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
            forged = JsonWebToken(["RS256"]).encode(
                {"alg": "RS256", "kid": key.kid},
                {
                    "iss": ISSUER, "aud": "sceneit-client", "sub": "owner-1",
                    "nonce": "nonce-1", "iat": now, "exp": now + 300,
                },
                other_key,
            )
            with self.assertRaises(Exception):
                _verified_claims(forged, "nonce-1")


class StorageBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ, {"PRIVATE_OBJECT_DIR": "/private-bucket/sceneit"}
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def test_paths_are_confined_to_private_prefix(self):
        self.assertEqual(
            _parts("/private-bucket/sceneit/owners/u/video.mp4"),
            ("private-bucket", "sceneit/owners/u/video.mp4"),
        )
        for path in (
            "/public-bucket/video.mp4",
            "/private-bucket/sceneit/../video.mp4",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                _parts(path)

    def test_direct_upload_is_exact_origin_bound_and_create_only(self):
        app = Flask(__name__)
        app.config["SESSION_SECRET"] = "s" * 48
        blob = unittest.mock.Mock()
        blob.create_resumable_upload_session.return_value = (
            "https://storage.invalid/resumable-secret"
        )
        with app.test_request_context(
            "/api/imports/upload",
            base_url="https://sceneit.example",
            headers={"Origin": "https://sceneit.example"},
        ), patch("sceneit.private_storage._blob", return_value=blob):
            result = reserve_upload(
                "/private-bucket/sceneit/u/file.mp4", 1234
            )
            self.assertEqual(result["method"], "PUT")
            self.assertEqual(result["headers"]["Content-Type"], "video/mp4")
            self.assertEqual(
                result["headers"]["Content-Range"], "bytes 0-1233/1234"
            )
            self.assertEqual(
                decrypt_upload_session(result["sessionReference"]),
                result["uploadURL"],
            )
        blob.create_resumable_upload_session.assert_called_once_with(
            content_type="video/mp4", size=1234,
            origin="https://sceneit.example", if_generation_match=0,
            timeout=30,
        )

    def test_upload_reservation_rejects_invalid_size_and_origin(self):
        app = Flask(__name__)
        app.config["SESSION_SECRET"] = "s" * 48
        for size in (0, 200_000_001, True):
            with app.test_request_context(
                "/", base_url="https://sceneit.example"
            ), self.subTest(size=size), self.assertRaises(ValueError):
                reserve_upload("/private-bucket/sceneit/u/file.mp4", size)
        with app.test_request_context(
            "/", base_url="https://sceneit.example",
            headers={"Origin": "https://evil.example"},
        ), self.assertRaises(ValueError):
            reserve_upload("/private-bucket/sceneit/u/file.mp4", 1234)

    def test_session_reference_is_encrypted_and_cancel_has_no_url_in_error(self):
        app = Flask(__name__)
        app.config["SESSION_SECRET"] = "s" * 48
        url = "https://storage.invalid/private-bearer"
        with app.test_request_context("/"):
            encrypted = encrypt_upload_session(url)
            self.assertNotIn("private-bearer", encrypted)
            self.assertEqual(decrypt_upload_session(encrypted), url)
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.delete.return_value.status_code = 500
        with patch("sceneit.private_storage.httpx.Client", return_value=client):
            with self.assertRaises(RuntimeError) as caught:
                cancel_upload_session(url)
        self.assertNotIn(url, str(caught.exception))

    def test_private_http_logging_never_emits_resumable_bearer_url(self):
        import httpx

        url = (
            "https://storage.invalid/upload/resumable?"
            "upload_id=do-not-log-this-bearer"
        )
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(204)
        )
        real_client = httpx.Client(transport=transport, timeout=30)
        factory = Mock()
        context = MagicMock()
        context.__enter__.return_value = real_client
        context.__exit__.side_effect = (
            lambda *_args: real_client.close()
        )
        factory.return_value = context
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        root = logging.getLogger()
        old_level = root.level
        capture = Capture()
        root.setLevel(logging.INFO)
        root.addHandler(capture)
        try:
            with patch("sceneit.private_storage.httpx.Client", factory):
                self.assertTrue(cancel_upload_session(url))
        finally:
            root.removeHandler(capture)
            root.setLevel(old_level)
            real_client.close()
        output = "\n".join(records)
        self.assertNotIn(url, output)
        self.assertNotIn("do-not-log-this-bearer", output)

    def test_single_ranges_include_suffix_and_reject_unsatisfied(self):
        self.assertEqual(_range("bytes=0-9", 100), (0, 9))
        self.assertEqual(_range("bytes=-10", 100), (90, 99))
        self.assertEqual(_range("bytes=90-", 100), (90, 99))
        self.assertFalse(_range("bytes=100-101", 100))
        self.assertFalse(_range("bytes=0-1,4-5", 100))

    def test_open_private_streams_bounded_generation_range(self):
        app = Flask(__name__)
        handle = Mock()
        handle.read.side_effect = [b"2345"]
        blob = Mock()
        blob.open.return_value = handle
        info = {
            "size": 10, "generation": 7, "contentType": "video/mp4",
            "etag": "etag",
        }
        with app.test_request_context("/"), \
                patch("sceneit.private_storage.object_info", return_value=info), \
                patch("sceneit.private_storage._blob", return_value=blob):
            response = open_private(
                "/private-bucket/sceneit/u/file.mp4",
                range_header="bytes=2-5", generation=7,
            )
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.get_data(), b"2345")
            self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
        blob.open.assert_called_once_with(
            "rb", chunk_size=1024 * 1024, if_generation_match=7, timeout=120,
        )
        handle.seek.assert_called_once_with(2)
        handle.close.assert_called_once()


@unittest.skipUnless(
    os.environ.get("SCENEIT_LIVE_STORAGE_TEST") == "1",
    "set SCENEIT_LIVE_STORAGE_TEST=1 for the tiny App Storage integration test",
)
class LivePrivateStorageTests(unittest.TestCase):
    """Opt-in, tiny App Storage verification; never emits bearer URLs."""

    def test_resumable_immutability_ranges_download_and_delete(self):
        root = os.environ["PRIVATE_OBJECT_DIR"].rstrip("/")
        object_path = f"{root}/imports/verification/{uuid.uuid4().hex}.mp4"
        oversized_path = f"{root}/imports/verification/{uuid.uuid4().hex}.mp4"
        payload = b"sceneit!"
        origin = "https://sceneit-storage-verification.invalid"
        app = Flask(__name__)
        app.config["SESSION_SECRET"] = os.environ["SESSION_SECRET"]
        session_urls = []
        generation = None
        try:
            with app.test_request_context(
                "/", base_url=origin, headers={"Origin": origin}
            ):
                reservation = reserve_upload(object_path, len(payload))
                session_urls.append(reservation["uploadURL"])
            uploaded = __import__("httpx").put(
                reservation["uploadURL"],
                content=payload,
                headers={**reservation["headers"], "Origin": origin},
                timeout=30,
            )
            if uploaded.status_code not in (200, 201):
                raise AssertionError(
                    f"correct_size_upload={uploaded.status_code}"
                )
            info = object_info(object_path)
            generation = info["generation"]
            self.assertEqual(info["size"], len(payload))

            replay = __import__("httpx").put(
                reservation["uploadURL"],
                content=b"modified",
                headers={**reservation["headers"], "Origin": origin},
                timeout=30,
            )
            # GCS may acknowledge a repeated final PUT idempotently; it must not
            # create a new generation or change the already committed bytes.
            self.assertIn(replay.status_code, (200, 201, 400, 404, 410))
            self.assertEqual(object_info(object_path)["generation"], generation)
            replay_check = io.BytesIO()
            download_object(
                object_path, replay_check, generation=generation
            )
            self.assertEqual(replay_check.getvalue(), payload)

            with app.test_request_context(
                "/", base_url=origin, headers={"Origin": origin}
            ):
                too_small = reserve_upload(oversized_path, len(payload))
                session_urls.append(too_small["uploadURL"])
            wrong = __import__("httpx").put(
                too_small["uploadURL"],
                content=payload + b"x",
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": (
                        f"bytes 0-{len(payload)}/{len(payload) + 1}"
                    ),
                    "Origin": origin,
                },
                timeout=30,
            )
            self.assertGreaterEqual(wrong.status_code, 400)

            destination = io.BytesIO()
            downloaded = download_object(
                object_path, destination, generation=generation
            )
            self.assertEqual(downloaded["size"], len(payload))
            self.assertEqual(destination.getvalue(), payload)
            with app.test_request_context("/"):
                ranged = open_private(
                    object_path, "bytes=2-5", generation=generation
                )
                self.assertEqual(ranged.status_code, 206)
                self.assertEqual(ranged.get_data(), payload[2:6])
                unsatisfied = open_private(
                    object_path, "bytes=99-100", generation=generation
                )
                self.assertEqual(unsatisfied.status_code, 416)
            delete_object(object_path, generation)
            generation = None
            with self.assertRaises(Exception):
                object_info(object_path)
        finally:
            for session_url in session_urls:
                try:
                    cancel_upload_session(session_url)
                except Exception:
                    pass
            if generation is not None:
                try:
                    delete_object(object_path, generation)
                except Exception:
                    pass
            try:
                extra = object_info(oversized_path)
                delete_object(oversized_path, extra["generation"])
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()