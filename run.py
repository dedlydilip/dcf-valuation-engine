#!/usr/bin/env python
"""Entry point.

    python run.py value --ticker AAPL --use-offline
    python run.py scenarios --ticker AAPL --use-offline
    python run.py snapshot --ticker NVDA
"""

from src.cli import main

if __name__ == "__main__":
    main()
