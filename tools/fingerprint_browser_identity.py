#!/usr/bin/env python3
"""Identify only the explicitly supplied native binary; never download a browser."""
import argparse
import json
from pathlib import Path
from fingerprint_smoke import binary_identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    original = binary_identity(args.browser)
    report = dict(original)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=report['path'], headless=True, chromium_sandbox=True,
            args=['--fingerprint=off', '--no-first-run', '--disable-background-networking'])
        try:
            report['version'] = browser.version
        finally:
            browser.close()
    if binary_identity(args.browser) != original:
        raise ValueError('browser executable changed during identification')
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)


if __name__ == '__main__':
    main()
