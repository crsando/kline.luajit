#!/usr/bin/env bash
# 在 WSL 内运行所有 spec。Windows 端用法:
#   wsl bash /mnt/c/Users/qiumi/Documents/Alma/2026-07-23/save-k-line-historical-data/outputs/run_tests.sh
cd "$(dirname "$0")" || exit 1
fail=0
for f in spec/*_spec.lua; do
  echo ">>> $f"
  luajit "$f" || fail=1
done
if [ $fail -eq 0 ]; then
  echo "=== ALL PASS ==="
else
  echo "=== SOME FAILED ==="
  exit 1
fi
