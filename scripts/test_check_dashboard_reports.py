#!/usr/bin/env python3
"""Self-test for check_dashboard_reports.py using synthetic Quartus 17 summaries.

These fixtures follow the documented report layout; they are not real reports.
Add the first real baseline's summaries here once a build host produces them.
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(HERE, "check_dashboard_reports.py")
BUDGET = os.path.join(HERE, "..", "docs", "dashboard-resource-budget.json")

FIT = """Fitter Status : {status} - Thu Sep 25 12:00:00 2026
Quartus Prime Version : 17.0.2 Build 602 07/19/2017 SJ Lite Edition
Revision Name : {rev}
Top-level Entity Name : sys_top
Family : Cyclone V
Device : {device}
Timing Models : Final
Logic utilization (in ALMs) : {alm:,} / 41,910 ( {pct} % )
Total registers : {regs}
Total pins : 180 / 314 ( 57 % )
Total virtual pins : 0
Total block memory bits : {bits:,} / 5,662,720 ( 40 % )
Total RAM Blocks : {ram} / 553 ( 55 % )
Total DSP Blocks : {dsp} / 112 ( 11 % )
Total HSSI RX PCSs : 0
Total PLLs : {pll} / 6 ( 50 % )
Total DLLs : 0 / 4 ( 0 % )
"""

STA = """------------------------------------------------------------
TimeQuest Timing Analyzer Summary
------------------------------------------------------------

Type  : Slow 1100mV 85C Model Setup 'emu|pll|pll_inst|altera_pll_i|outclk_wire[1]'
Slack : {setup}
TNS   : 0.000

Type  : Slow 1100mV 85C Model Hold 'emu|pll|pll_inst|altera_pll_i|outclk_wire[1]'
Slack : {hold}
TNS   : 0.000

Type  : Slow 1100mV 85C Model Recovery 'FPGA_CLK1_50'
Slack : 12.001
TNS   : 0.000
"""

FLOW = """Flow Status : {status} - Thu Sep 25 12:00:00 2026
Revision Name : {rev}
"""


def write(d, rev, status="Successful", device="5CSEBA6U23I7", alm=30000, regs=40000, bits=2200000,
          ram=300, dsp=12, pll=3, setup="0.212", hold="0.180", sta=True, flow=True):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, rev + ".fit.summary"), "w") as f:
        f.write(FIT.format(status=status, rev=rev, device=device, alm=alm, pct=alm * 100 // 41910,
                           regs=regs, bits=bits, ram=ram, dsp=dsp, pll=pll))
    if sta:
        with open(os.path.join(d, rev + ".sta.summary"), "w") as f:
            f.write(STA.format(setup=setup, hold=hold))
    if flow:
        with open(os.path.join(d, rev + ".flow.rpt"), "w") as f:
            f.write(FLOW.format(status=status, rev=rev))


class T(unittest.TestCase):
    def run_check(self, **cand):
        with tempfile.TemporaryDirectory() as t:
            write(os.path.join(t, "base"), "MegaCD")
            write(os.path.join(t, "cand"), "MegaCD_Dashboard", **cand)
            r = subprocess.run([sys.executable, CHECK, "--baseline", os.path.join(t, "base"),
                                "--candidate", os.path.join(t, "cand"), "--revision", "MegaCD_Dashboard",
                                "--budget", BUDGET], capture_output=True, text=True)
            return r.returncode, r.stdout

    def test_pass(self):
        rc, out = self.run_check(alm=31000, ram=303)
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS", out)

    def test_alm_budget(self):
        rc, out = self.run_check(alm=31600)
        self.assertEqual(rc, 1)
        self.assertIn("alm grew by 1600", out)

    def test_ram_budget(self):
        rc, out = self.run_check(ram=305)
        self.assertEqual(rc, 1)

    def test_new_dsp_or_pll(self):
        self.assertEqual(self.run_check(dsp=13)[0], 1)
        self.assertEqual(self.run_check(pll=4)[0], 1)

    def test_negative_setup_and_hold(self):
        rc, out = self.run_check(setup="-0.051")
        self.assertEqual(rc, 1)
        self.assertIn("negative slack", out)
        self.assertEqual(self.run_check(hold="-0.001")[0], 1)

    def test_free_alm_floor(self):
        rc, out = self.run_check(alm=40000)
        self.assertEqual(rc, 1)

    def test_failed_fit_and_missing_reports(self):
        self.assertEqual(self.run_check(status="Failed")[0], 1)
        self.assertEqual(self.run_check(sta=False)[0], 1)
        self.assertEqual(self.run_check(flow=False)[0], 1)

    def test_wrong_device(self):
        rc, out = self.run_check(device="5CSEMA5F31C6")
        self.assertEqual(rc, 1)
        self.assertIn("device", out)


if __name__ == "__main__":
    unittest.main()
