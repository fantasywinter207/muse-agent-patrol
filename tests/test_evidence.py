"""Evidence hashes describe the exact artifact bytes on every platform."""
import contextlib
import hashlib
import io
import json
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class Evidence(unittest.TestCase):
    def test_raw_output_bytes_match_report_without_newline_translation(self):
        runner=Path(__file__).resolve().parents[1]/"tools"/"run_checks.py"
        main=runpy.run_path(str(runner))["main"]
        def complete(command,**kwargs):
            if command[1:]==["rev-parse","HEAD"]:
                return subprocess.CompletedProcess(command,0,"fixture-head\n","")
            if command[1:]==["status","--porcelain"]:
                return subprocess.CompletedProcess(command,0,"","")
            return subprocess.CompletedProcess(command,0,"alpha\nbeta\n","warning\n")
        with tempfile.TemporaryDirectory() as folder:
            with patch("sys.argv",[str(runner),"--output",folder]), patch("subprocess.run",side_effect=complete), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(),0)
            report=json.loads((Path(folder)/"report.json").read_text(encoding="utf-8"))
            self.assertTrue(report["passed"])
            for command in report["commands"]:
                raw=(Path(folder)/command["raw_output"]).read_bytes()
                self.assertNotIn(b"\r\n",raw)
                self.assertIn(b"alpha\nbeta\n",raw)
                self.assertEqual(hashlib.sha256(raw).hexdigest(),command["sha256"])
