import unittest
import time
from app.common import clear_time, safety


class TestFeatureBPhysicsClearTime(unittest.TestCase):

    def test_no_rain_monotonic_decrease_and_finite_time(self):
        """No rain: depth decreases monotonically and the time is finite."""
        h_init = 35.0
        h_target = 15.0
        k = 2.5
        runoff = 0.85
        hourly_rain = [0.0] * 24

        clear_min, reached, history = clear_time.simulate_water_balance(
            h_init=h_init,
            k=k,
            runoff_coeff=runoff,
            hourly_rain_mm=hourly_rain,
            h_target=h_target,
            buffer_min=15,
        )

        self.assertTrue(reached, "Water should reach target depth when there is no rain")
        self.assertIsNotNone(clear_min)
        self.assertGreater(clear_min, 0)
        self.assertLess(clear_min, 24 * 60, "Should clear within 24 hours")

        # Verify monotonic decrease until it reaches 0
        for i in range(len(history) - 1):
            if history[i] > 0:
                self.assertLessEqual(
                    history[i + 1],
                    history[i],
                    f"Depth must monotonically decrease: {history[i]} -> {history[i+1]} at step {i}"
                )

    def test_more_forecast_rain_gives_later_time(self):
        """More forecast rain gives a later time."""
        base_weather = {
            "past_24h_precip_mm": 0.0,
            "forecast_hourly_mm": [0.0] * 24,
            "forecast_available": True,
        }
        res_no_rain = clear_time.estimate_clear_time(
            lat=12.97, lng=77.59,
            depth_interval=(30.0, 30.0),
            vehicle="hatchback",
            mock_weather=base_weather,
            current_time=100000,
        )

        rainy_weather = {
            "past_24h_precip_mm": 0.0,
            "forecast_hourly_mm": [5.0] * 24,
            "forecast_available": True,
        }
        res_rainy = clear_time.estimate_clear_time(
            lat=12.97, lng=77.59,
            depth_interval=(30.0, 30.0),
            vehicle="hatchback",
            mock_weather=rainy_weather,
            current_time=100000,
        )

        min_no_rain = res_no_rain["likely_minutes"] or 0
        min_rainy = res_rainy["likely_minutes"] or 9999
        self.assertGreater(min_rainy, min_no_rain, "Rainy forecast must result in a later clearance time")

    def test_heavier_past_day_rain_gives_later_time(self):
        """Heavier past-day rain gives a later time (saturated drainage)."""
        dry_past = {
            "past_24h_precip_mm": 5.0,
            "forecast_hourly_mm": [0.0] * 24,
            "forecast_available": True,
        }
        res_dry_past = clear_time.estimate_clear_time(
            lat=12.97, lng=77.59,
            depth_interval=(30.0, 30.0),
            vehicle="hatchback",
            mock_weather=dry_past,
            current_time=100000,
        )

        soaked_past = {
            "past_24h_precip_mm": 45.0,  # exceeds heavy antecedent threshold (25mm)
            "forecast_hourly_mm": [0.0] * 24,
            "forecast_available": True,
        }
        res_soaked_past = clear_time.estimate_clear_time(
            lat=12.97, lng=77.59,
            depth_interval=(30.0, 30.0),
            vehicle="hatchback",
            mock_weather=soaked_past,
            current_time=100000,
        )

        min_dry = res_dry_past["likely_minutes"]
        min_soaked = res_soaked_past["likely_minutes"]
        self.assertGreater(min_soaked, min_dry, "Saturated past-day rain must reduce k and give a later time")

    def test_deeper_initial_depth_gives_later_time(self):
        """A deeper initial depth gives a later time."""
        weather = {
            "past_24h_precip_mm": 0.0,
            "forecast_hourly_mm": [0.0] * 24,
            "forecast_available": True,
        }
        res_shallow = clear_time.estimate_clear_time(
            lat=12.97, lng=77.59,
            depth_interval=(22.0, 24.0),
            vehicle="hatchback",
            mock_weather=weather,
            current_time=100000,
        )
        res_deep = clear_time.estimate_clear_time(
            lat=12.97, lng=77.59,
            depth_interval=(45.0, 50.0),
            vehicle="hatchback",
            mock_weather=weather,
            current_time=100000,
        )

        self.assertGreater(
            res_deep["likely_minutes"],
            res_shallow["likely_minutes"],
            "Deeper initial depth must take longer to clear"
        )

    def test_lower_vehicle_threshold_gives_later_time(self):
        """A lower vehicle threshold gives a later time."""
        weather = {
            "past_24h_precip_mm": 0.0,
            "forecast_hourly_mm": [0.0] * 24,
            "forecast_available": True,
        }
        # Pedestrian threshold is 12 cm, SUV threshold is 28 cm.
        # Initial depth 35 cm:
        res_ped = clear_time.estimate_clear_time(
            lat=12.97, lng=77.59,
            depth_interval=(35.0, 35.0),
            vehicle="pedestrian",
            mock_weather=weather,
            current_time=100000,
        )
        res_suv = clear_time.estimate_clear_time(
            lat=12.97, lng=77.59,
            depth_interval=(35.0, 35.0),
            vehicle="suv",
            mock_weather=weather,
            current_time=100000,
        )

        self.assertGreater(
            res_ped["likely_minutes"],
            res_suv["likely_minutes"],
            "Pedestrian (lower threshold) must clear later than SUV (higher threshold)"
        )

    def test_heavy_continuing_rain_returns_not_before_rain_eases(self):
        """Heavy continuing rain returns 'not before the rain eases'."""
        heavy_rain_weather = {
            "past_24h_precip_mm": 10.0,
            # Sustained heavy rain (12 mm/h) for first 4 hours, then eases
            "forecast_hourly_mm": [12.0, 12.0, 12.0, 10.0, 1.0, 0.0] + [0.0] * 18,
            "forecast_available": True,
        }
        res = clear_time.estimate_clear_time(
            lat=12.97, lng=77.59,
            depth_interval=(30.0, 35.0),
            vehicle="hatchback",
            mock_weather=heavy_rain_weather,
            current_time=100000,
        )

        self.assertIsNotNone(res["rain_note"])
        self.assertIn("not before the rain eases", res["rain_note"])

    def test_observed_drain_rate_overrides_default(self):
        """Observed drain rate overrides the default."""
        weather = {
            "past_24h_precip_mm": 0.0,
            "forecast_hourly_mm": [0.0] * 24,
            "forecast_available": True,
        }
        now_ts = int(time.time())
        # Two reports 1 hour apart showing falling depth from 30 cm to 20 cm
        reports = [
            {"captured_at": now_ts - 7200, "depth_lo": 28, "depth_hi": 32},
            {"captured_at": now_ts - 3600, "depth_lo": 18, "depth_hi": 22},
        ]
        res_observed = clear_time.estimate_clear_time(
            lat=12.97, lng=77.59,
            depth_interval=(25.0, 25.0),
            vehicle="hatchback",
            recent_reports=reports,
            mock_weather=weather,
            current_time=now_ts,
        )

        self.assertEqual(res_observed["basis"], "observed")
        self.assertEqual(res_observed["drain_rate_label"], "observed drain rate")

        # Without reports
        res_assumed = clear_time.estimate_clear_time(
            lat=12.97, lng=77.59,
            depth_interval=(25.0, 25.0),
            vehicle="hatchback",
            recent_reports=[],
            mock_weather=weather,
            current_time=now_ts,
        )
        self.assertEqual(res_assumed["basis"], "assumed")
        self.assertEqual(res_assumed["drain_rate_label"], "assumed drain rate")

    def test_weather_api_failure_gives_wider_range_and_forecast_unavailable(self):
        """Weather API failure gives a wider range and forecast_available=false."""
        failed_weather = {
            "past_24h_precip_mm": 0.0,
            "forecast_hourly_mm": [],
            "forecast_available": False,
        }
        res_fail = clear_time.estimate_clear_time(
            lat=12.97, lng=77.59,
            depth_interval=(25.0, 35.0),
            vehicle="hatchback",
            mock_weather=failed_weather,
            current_time=100000,
        )

        self.assertFalse(res_fail["forecast_available"])

        known_weather = {
            "past_24h_precip_mm": 0.0,
            "forecast_hourly_mm": [1.0] * 24,
            "forecast_available": True,
        }
        res_known = clear_time.estimate_clear_time(
            lat=12.97, lng=77.59,
            depth_interval=(25.0, 35.0),
            vehicle="hatchback",
            mock_weather=known_weather,
            current_time=100000,
        )

        range_fail = (res_fail["latest_minutes"] or 0) - (res_fail["earliest_minutes"] or 0)
        range_known = (res_known["latest_minutes"] or 0) - (res_known["earliest_minutes"] or 0)
        self.assertGreater(range_fail, range_known, "Failed weather forecast must produce a wider uncertainty range")


if __name__ == "__main__":
    unittest.main()
