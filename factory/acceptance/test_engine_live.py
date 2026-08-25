#!/usr/bin/env python3
"""Opt-in live proof for the shared Capacity Pool provider adapters."""

from __future__ import annotations

import os
import unittest

from factory.acceptance.e2e_doctor import Doctor

LIVE = os.environ.get("FACTORY_ENGINE_LIVE") == "1"


@unittest.skipUnless(LIVE, "live capacity proof; run deliberately with FACTORY_ENGINE_LIVE=1")
class TestConfiguredCapacityAdaptersWork(unittest.TestCase):
    def test_every_configured_capacity_answers_its_independent_probe(self):
        doctor = Doctor("maheshhbhat/ai-software-factory", 1, commitment=1,
                        target="unused", environ=os.environ)
        doctor.worker_engine_start()
        probes = [row for row in doctor.checks if row.name.startswith("capacity probe")]
        self.assertTrue(probes, "no configured capacity was discovered")
        failures = [f"{row.name}: {row.detail}" for row in probes if not row.passed]
        self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()
