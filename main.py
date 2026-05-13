"""
main.py
"""

import sys

from orchestrator.loop import start_experiment


def main(argv: list[str] | None = None):
    start_experiment()


if __name__ == "__main__":
    main()