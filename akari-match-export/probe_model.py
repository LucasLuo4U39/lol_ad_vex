#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Probe LightGBM text models: feature usage, decision types, special lines."""
import re
import sys
from pathlib import Path

def probe(path: Path):
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    header = {}
    for ln in lines[:12]:
        if "=" in ln:
            k, v = ln.split("=", 1)
            header[k] = v
    trees = re.split(r"\nTree=\d+\n", text)[1:]
    feature_usage = {}
    decision_types = {}
    specials = {"default_left": 0, "cat_threshold": 0, "cat_boundaries": 0, "average_output": 0}
    num_trees = len(trees)
    max_leaves = 0
    for t in trees:
        m = re.search(r"num_leaves=(\d+)", t)
        if m:
            max_leaves = max(max_leaves, int(m.group(1)))
        mc = re.search(r"num_cat=(\d+)", t)
        if mc and int(mc.group(1)) > 0:
            specials["cat_boundaries"] += 1
        for key in ("default_left", "cat_threshold", "average_output"):
            if key + "=" in t:
                specials[key] += 1
        m = re.search(r"split_feature=(.*)", t)
        if m:
            for v in m.group(1).split():
                feature_usage[int(v)] = feature_usage.get(int(v), 0) + 1
        m = re.search(r"decision_type=(.*)", t)
        if m:
            for v in m.group(1).split():
                decision_types[int(v)] = decision_types.get(int(v), 0) + 1
    print(f"== {path.name} ==")
    print(f"  trees={num_trees} max_leaves={max_leaves} objective={header.get('objective')}")
    print(f"  num_class={header.get('num_class')} max_feature_idx={header.get('max_feature_idx')}")
    print(f"  decision_types: {decision_types}")
    print(f"  specials: {specials}")
    print(f"  features used: {dict(sorted(feature_usage.items()))}")
    print()

for p in sys.argv[1:]:
    probe(Path(p))
