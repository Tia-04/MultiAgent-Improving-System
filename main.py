"""
main.py
"""

import sys

from orchestrator.loop import run_normal_mode


def main(argv: list[str] | None = None):
    run_normal_mode()


if __name__ == "__main__":
    main()