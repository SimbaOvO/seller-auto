import unittest

from seller_auto.pacing import Cooldown, DelayError, DelaySettings


class DelaySettingsTests(unittest.TestCase):
    def test_defaults_keep_amazon_and_mercadolibre_apart(self) -> None:
        settings = DelaySettings()
        self.assertEqual((settings.amazon_min_delay, settings.amazon_max_delay), (8, 15))
        self.assertEqual((settings.ml_min_delay, settings.ml_max_delay), (12, 25))
        self.assertEqual((settings.ml_captcha_min, settings.ml_captcha_max), (30, 60))
        self.assertEqual(settings.similarity_threshold, 0.8)

    def test_inverted_range_is_rejected(self) -> None:
        settings = DelaySettings(ml_min_delay=25, ml_max_delay=12)
        with self.assertRaises(DelayError):
            settings.validate()

    def test_argv_overrides_are_separate(self) -> None:
        settings = DelaySettings.from_argv(
            ["--amazon-min-delay", "9", "--amazon-max-delay", "10", "--ml-min-delay", "20", "--ml-max-delay", "30"]
        )
        self.assertEqual(settings.amazon_min_delay, 9)
        self.assertEqual(settings.ml_min_delay, 20)


class CooldownTests(unittest.TestCase):
    def test_second_navigation_waits_until_the_interval_ends(self) -> None:
        clock = {"t": 0.0}
        slept: list[float] = []

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock["t"] += seconds

        samples = iter([12.0, 20.0, 15.0])
        cooldown = Cooldown(12, 25, sleep=sleep, now=lambda: clock["t"], rng=lambda _low, _high: next(samples))
        first = cooldown.acquire()
        second = cooldown.acquire()
        self.assertEqual(first, 12)
        self.assertEqual(second, 20)
        self.assertEqual(slept, [12, 20])

    def test_early_second_call_waits_only_the_remaining_time(self) -> None:
        clock = {"t": 50.0}
        slept: list[float] = []

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock["t"] += seconds

        cooldown = Cooldown(12, 12, sleep=sleep, now=lambda: clock["t"], rng=lambda low, _high: low)
        cooldown.ready_at = 62.0
        waited = cooldown.acquire()
        self.assertEqual(waited, 12)
        self.assertEqual(slept, [12])

    def test_captcha_penalty_is_added_on_top_of_the_pending_interval(self) -> None:
        clock = {"t": 10.0}
        slept: list[float] = []

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock["t"] += seconds

        cooldown = Cooldown(12, 12, sleep=sleep, now=lambda: clock["t"], rng=lambda low, _high: low)
        cooldown.ready_at = 22.0
        cooldown.penalize(30)
        cooldown.acquire()
        self.assertEqual(slept, [42])

    def test_two_cooldowns_do_not_share_a_clock(self) -> None:
        amazon = Cooldown(8, 8, sleep=lambda _seconds: None, now=lambda: 0, rng=lambda low, _high: low)
        mercadolibre = Cooldown(12, 12, sleep=lambda _seconds: None, now=lambda: 0, rng=lambda low, _high: low)
        amazon.acquire()
        self.assertIsNone(mercadolibre.ready_at)
        self.assertIsNotNone(amazon.ready_at)


if __name__ == "__main__":
    unittest.main()
