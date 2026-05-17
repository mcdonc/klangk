#!/usr/bin/env python3
"""Analyze a text file: line count, word count, character count, file size."""

import sys
import os


def analyze(path):
    if not os.path.exists(path):
        print(f"Error: {path} not found")
        sys.exit(1)

    size = os.path.getsize(path)
    with open(path, "r", errors="replace") as f:
        content = f.read()

    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    words = len(content.split())
    chars = len(content)

    print(f"File: {path}")
    print(f"Size: {size} bytes")
    print(f"Lines: {lines}")
    print(f"Words: {words}")
    print(f"Characters: {chars}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: word_count <file>")
        sys.exit(1)
    analyze(sys.argv[1])
