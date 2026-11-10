import unittest

from seller_auto.exit_identity import argentina_identity, china_identity, resolve_exit_identity


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

    def test_main_browser_uses_argentina(self) -> None:
        identity = argentina_identity()
        self.assertEqual(identity.country, "AR")
        self.assertEqual(identity.city, "Buenos Aires")
        self.assertEqual(identity.locale, "es-AR")
        self.assertEqual(identity.timezone_id, "America/Argentina/Buenos_Aires")
        resolved = resolve_exit_identity()
        self.assertEqual(resolved.locale, "es-AR")
        self.assertEqual(resolved.timezone_id, "America/Argentina/Buenos_Aires")
        self.assertEqual(resolved.source, "argentina")


if __name__ == "__main__":
    unittest.main()
