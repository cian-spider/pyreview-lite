#!/usr/bin/env python3
"""A file that is wrong in ten small ways, so pyreview-lite has something to find.

    python3 pyreview_lite.py demo.py

Every finding it prints here comes with the command that shows the behaviour, and the
command runs.
"""

import subprocess  # noqa: F401


def collect(items=[]):
    try:
        v = eval(input("expr: "))  # noqa: S307
    except:
        pass
    if v == None:
        return []
    assert isinstance(v, int), "must be int"
    subprocess.run("ls " + str(v), shell=True)
    if 0.1 + 0.2 == 0.3:
        print(f"no fields here")
    list = [1]
    if v is 5:
        return list
    return [v]


if __name__ == "__main__":
    print(collect())
