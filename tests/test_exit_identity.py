import unittest
from unittest import mock

from seller_auto.exit_identity import (
    clear_exit_identity_cache,
    resolve_exit_identity,
    _from_payload,
)


class ExitIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_exit_identity_cache()

    def tearDown(self) -> None:
        clear_exit_identity_cache()

    def test_singapore_exit_maps_to_singapore_identity(self) -> None:
        identity = _from_payload(
            {
                "ip": "172.236.154.135",
                "city": "Singapore",
                "country": "SG",
                "loc": "1.2897,103.8501",
                "timezone": "Asia/Singapore",
            },
            "https://ipinfo.io/json",
        )
        self.assertEqual(identity.country, "SG")
        self.assertEqual(identity.locale, "en-SG")
        self.assertEqual(identity.timezone_id, "Asia/Singapore")
        self.assertAlmostEqual(identity.geolocation["latitude"], 1.2897)
        self.assertAlmostEqual(identity.geolocation["longitude"], 103.8501)
        self.assertIn("es", identity.accept_language)

    def test_argentina_exit_keeps_buenos_aires(self) -> None:
        identity = _from_payload(
            {"ip": "1.2.3.4", "city": "Buenos Aires", "country": "AR", "loc": "-34.6,-58.4"},
            "https://ipinfo.io/json",
        )
        self.assertEqual(identity.locale, "es-AR")
        self.assertEqual(identity.timezone_id, "America/Argentina/Buenos_Aires")

    def test_fallback_uses_argentina(self) -> None:
        identity = _from_payload({}, "fallback")
        self.assertEqual(identity.country, "AR")
        self.assertEqual(identity.locale, "es-AR")

    def test_resolve_caches_lookup(self) -> None:
        payload = {
            "ip": "172.236.154.135",
            "city": "Singapore",
            "country": "SG",
            "loc": "1.2897,103.8501",
            "timezone": "Asia/Singapore",
        }
        with mock.patch("seller_auto.exit_identity._lookup_exit", return_value=(payload, "test")) as lookup:
            first = resolve_exit_identity()
            second = resolve_exit_identity()
        self.assertIs(first, second)
        lookup.assert_called_once()
        self.assertEqual(first.timezone_id, "Asia/Singapore")


if __name__ == "__main__":
    unittest.main()
