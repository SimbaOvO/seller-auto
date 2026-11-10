import unittest

from seller_auto.exit_identity import china_identity, resolve_exit_identity


class ExitIdentityTests(unittest.TestCase):
    def test_identity_is_china(self) -> None:
        identity = china_identity()
        self.assertEqual(identity.country, "CN")
        self.assertEqual(identity.city, "Shanghai")
        self.assertEqual(identity.locale, "zh-CN")
        self.assertEqual(identity.accept_language, "zh-CN,zh;q=0.9,en;q=0.8")
        self.assertEqual(identity.timezone_id, "Asia/Shanghai")
        self.assertAlmostEqual(identity.geolocation["latitude"], 31.2304)
        self.assertAlmostEqual(identity.geolocation["longitude"], 121.4737)

    def test_resolve_does_not_follow_exit_country(self) -> None:
        identity = resolve_exit_identity()
        self.assertEqual(identity.locale, "zh-CN")
        self.assertEqual(identity.timezone_id, "Asia/Shanghai")
        self.assertEqual(identity.source, "china")


if __name__ == "__main__":
    unittest.main()
