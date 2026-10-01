#!/usr/bin/env python3
"""Set or clear bootcore= in a MiSTer.ini read from stdin; writes the result to stdout.

    ini_bootcore.py set "Desert Bus (control).mgl"   # bootcore=<mgl>, bootcore_timeout commented
    ini_bootcore.py unset                            # comments out an active bootcore= line

Only the [MiSTer] section is changed. Line endings (CRLF in the stock file) are kept.
"""
import re
import sys

TAG = " ; MegaCD dashboard package"


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("set", "unset") or (sys.argv[1] == "set" and len(sys.argv) != 3):
        sys.exit(__doc__)
    data = sys.stdin.buffer.read().decode("latin-1")
    nl = "\r\n" if "\r\n" in data else "\n"
    lines = data.split(nl)
    section, start, done, anchor = None, None, False, None
    for i, line in enumerate(lines):
        m = re.match(r"\s*\[([^\]]+)\]", line)
        if m:
            section = m.group(1).strip().lower()
            if section == "mister" and start is None:
                start = i
            continue
        if section != "mister":
            continue
        if re.match(r"\s*;\s*bootcore\s*=", line) and anchor is None:
            anchor = i                      # the stock commented example line
        if re.match(r"\s*bootcore\s*=", line):
            if sys.argv[1] == "set" and not done:
                lines[i] = f"bootcore={sys.argv[2]}{TAG}"
                done = True
            else:
                lines[i] = ";" + line
        elif sys.argv[1] == "set" and re.match(r"\s*bootcore_timeout\s*=", line):
            lines[i] = ";" + line
    if sys.argv[1] == "set" and not done:
        if start is None:
            sys.exit("no [MiSTer] section in MiSTer.ini")
        lines.insert((anchor if anchor is not None else start) + 1, f"bootcore={sys.argv[2]}{TAG}")
    sys.stdout.buffer.write(nl.join(lines).encode("latin-1"))


if __name__ == "__main__":
    main()
