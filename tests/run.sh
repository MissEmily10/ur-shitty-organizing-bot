#!/usr/bin/env bash
# Все проверки бота разом: python tests/... через настоящие обработчики Telegram, без сети.
cd "$(dirname "$0")/.." || exit 1
fail=0
for t in tests/test_*.py; do
  if PYTHONPATH=.:tests python "$t" 2>/dev/null | grep -q " OK$"; then
    echo "✅ $t"
  else
    echo "❌ $t"; fail=1
  fi
done
exit $fail
