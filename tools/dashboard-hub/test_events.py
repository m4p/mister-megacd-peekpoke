#!/usr/bin/env python3
"""Unit tests for the hub's event detector and bus stop maths (no hardware needed).

    python3 test_events.py
"""
import argparse
import sys
import unittest

import dashboard_hub as hub


def args(**kw):
    a = argparse.Namespace(stop_min_progress=30, stop_max_progress=75)
    a.__dict__.update(kw)
    return a


def tel(**kw):
    t = {"in_game": True, "game_state": 3, "speed_raw": 0x6000, "distance_raw": 10000, "leg_miles": 5.5,
         "lateral_raw": 0x6C00, "points": 0, "odometer_miles": 114.8, "return_leg": False,
         "splat_visible": False, "splat_x": 0x168,
         "stop_active": False, "stop_visible": False, "stop_progress": 0}
    t.update(kw)
    return t


class Detector(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.d = hub.EventDetector(lambda n, det: self.events.append((n, det)), args())

    def feed(self, *samples):
        for s in samples:
            self.d.update(s)

    def names(self):
        return [n for n, _ in self.events]

    def approach(self, progress):
        return tel(stop_active=True, stop_visible=True, stop_progress=progress)

    def test_stop_then_drive_off(self):
        self.feed(tel(), self.approach(20), self.approach(45),
                  tel(stop_active=True, stop_visible=True, stop_progress=50, speed_raw=0x200),
                  tel(stop_active=True, stop_visible=True, stop_progress=50, speed_raw=0),
                  tel(stop_active=True, stop_visible=True, stop_progress=50, speed_raw=0))
        self.assertEqual(self.names(), [], "no event while standing at the stop")
        self.feed(tel(stop_active=True, stop_visible=True, stop_progress=51, speed_raw=0x800),
                  self.approach(90), tel())
        self.assertEqual(self.names(), ["bus_stop"])

    def test_crash_at_stop_gives_no_bus_stop(self):
        self.feed(tel(), self.approach(45),
                  tel(stop_active=True, stop_visible=True, stop_progress=50, speed_raw=0),
                  tel(stop_active=True, stop_visible=True, stop_progress=50, speed_raw=0, game_state=1),
                  tel(stop_active=True, stop_visible=True, stop_progress=50, speed_raw=0, game_state=2),
                  tel(stop_active=True, stop_visible=True, stop_progress=50, speed_raw=0x2000, game_state=4),
                  tel(stop_active=True, stop_visible=True, stop_progress=50, speed_raw=0x2000, game_state=3),
                  tel())
        self.assertEqual(self.names(), ["crash"])

    def test_passing_is_missed(self):
        self.feed(tel(), self.approach(10), self.approach(50), self.approach(100), tel())
        self.assertEqual(self.names(), ["bus_stop_missed"])

    def test_stopping_too_early_does_not_count(self):
        self.feed(tel(), self.approach(10), tel(stop_active=True, stop_visible=True, stop_progress=12, speed_raw=0),
                  tel(stop_active=True, stop_visible=True, stop_progress=13, speed_raw=0x6000),
                  self.approach(80), tel())
        self.assertEqual(self.names(), ["bus_stop_missed"])

    def test_bus_stop_fires_once(self):
        stopped = tel(stop_active=True, stop_visible=True, stop_progress=50, speed_raw=0)
        moving = tel(stop_active=True, stop_visible=True, stop_progress=52, speed_raw=0x6000)
        self.feed(tel(), self.approach(40), stopped, moving, stopped, moving, tel())
        self.assertEqual(self.names(), ["bus_stop"])

    def test_crash(self):
        self.feed(tel(), tel(game_state=1, speed_raw=0x5000))
        self.assertEqual(self.names(), ["crash"])
        self.feed(tel(game_state=2), tel(game_state=4))
        self.assertEqual(self.names(), ["crash"], "tow states do not repeat the crash")

    def test_standing_still_too_long_is_a_crash(self):
        self.feed(tel(speed_raw=0), tel(speed_raw=0, game_state=4))
        self.assertEqual(self.names(), ["crash"])
        self.assertEqual(self.events[0][1]["cause"], "stood still too long")

    def test_point(self):
        self.feed(tel(distance_raw=hub.LEG_END - 3), tel(distance_raw=hub.LEG_END),
                  tel(distance_raw=hub.LEG_END, game_state=0))
        self.assertEqual(self.names(), ["point"])
        self.assertEqual(self.events[0][1]["points"], 1)

    def test_splat_only_while_driving(self):
        self.feed(tel(), tel(game_state=0), tel(game_state=0, splat_visible=True), tel(splat_visible=True))
        self.assertEqual(self.names(), [], "restored splat after an interlude is not a new event")
        self.feed(tel(splat_visible=False), tel(splat_visible=True, splat_x=0xA0))
        self.assertEqual(self.names(), ["bug_splat"])

    def test_not_in_game(self):
        self.feed(tel(in_game=False), tel(in_game=False, game_state=1))
        self.assertEqual(self.names(), [])


class CacheFreshness(unittest.TestCase):
    def test_freshest_block_wins(self):
        c = hub.Cache()
        c.put("bus", 0xFF6FEA, b"\x00\x00")                  # small block from a client miss
        c.put("bus", 0xFF6FDC, bytes(14) + b"\x60\x00")       # later poll of the whole chunk
        self.assertEqual(c.read("bus", 0xFF6FEA, 2), b"\x60\x00")
        self.assertEqual(list(c.blocks["bus"]), [0xFF6FDC], "covered block dropped")

    def test_newer_small_block_is_used_until_the_chunk_refreshes(self):
        c = hub.Cache()
        c.put("bus", 0xFF6FDC, bytes(16))
        c.put("bus", 0xFF6FEA, b"\x12\x34")
        self.assertEqual(c.read("bus", 0xFF6FEA, 2), b"\x12\x34")
        c.put("bus", 0xFF6FDC, bytes(14) + b"\x56\x78")
        self.assertEqual(c.read("bus", 0xFF6FEA, 2), b"\x56\x78")


class BusStops(unittest.TestCase):
    TABLE = b"".join(v.to_bytes(4, "big") for v in (0x640, 0x18380, 0x37140, 0x490C0, 0x66BC0, 0x81B00))
    STOCK = bytes.fromhex("43f900ff")

    def test_table(self):
        self.assertEqual(hub.next_bus_stop(0, self.TABLE, self.STOCK), (0x640, "table"))
        self.assertEqual(hub.next_bus_stop(0x640, self.TABLE, self.STOCK), (0x18380, "table"))
        self.assertEqual(hub.next_bus_stop(0x81B00, self.TABLE, self.STOCK), (None, "table"))

    def test_every_mile_patch(self):
        code = bytes.fromhex("80fc0708")
        self.assertEqual(hub.next_bus_stop(100, self.TABLE, code), (1800, "every 1 mi"))
        self.assertEqual(hub.next_bus_stop(1800, self.TABLE, code), (3600, "every 1 mi"))
        self.assertEqual(hub.next_bus_stop(hub.LEG_END - 10, self.TABLE, code)[0], None)

    def test_driver_name(self):
        self.assertEqual(hub.driver_name(bytes([0x0A, 0x0F, 0x03, 0x0B, 0x0F, 0, 0, 0])), "JOCKO")


if __name__ == "__main__":
    sys.exit(unittest.main())
