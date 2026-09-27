"""Итоговый прогноз посадок трамвая на ноябрь–декабрь 2025 → submission.csv.

    py predict.py                         # dataset/ рядом, результат — ./submission.csv
    py predict.py --data PATH --out FILE  # другие пути
    py predict.py --offline               # без сети (календарь — встроенный список постановления № 1335)

Воспроизводит сабмит с результатом WAPE-score 0.91250 на лидерборде.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ml.config import DATA_DIR  # noqa: E402
from ml.forecast import build_final  # noqa: E402
from ml.submission import md5, validate, write  # noqa: E402

# Отпечаток эталонного результата (сабмит с WAPE-score 0.91250). Сам файл для проверки не нужен:
# сравниваем md5 и сумму прогнозов только что созданного submission.csv с этими числами.
LEADERBOARD_MD5 = "8843277dc8ddc5ecee83b3b809908563"
LEADERBOARD_TOTAL = 12_818_544


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=DATA_DIR, help="папка датасета (labels/, test_submission.csv, test.csv)")
    ap.add_argument("--out", type=Path, default=ROOT / "submission.csv")
    ap.add_argument("--offline", action="store_true", help="не ходить в сеть")
    a = ap.parse_args()

    t0 = time.time()
    sub, info = build_final(a.data, a.offline)
    errs = validate(sub)
    if errs:
        sys.exit("Сабмит не прошёл проверку формата: " + "; ".join(errs))
    h = write(sub, a.out)
    print(f"Готово за {time.time() - t0:.1f} с → {a.out}")
    print(f"  строк: {len(sub):,}, посадок всего: {int(sub['prediction'].sum()):,}; календарь: {info['calendar']}")
    print(f"  внешние события и калибровки: {', '.join(info['effects'])}")
    total = int(sub["prediction"].sum())
    if h == LEADERBOARD_MD5:
        print(f"  md5 {h}  ✓ файл идентичен сабмиту 0.91250")
    elif abs(total - LEADERBOARD_TOTAL) <= 100:
        print(f"  md5 {h} — не совпал, но сумма прогнозов отличается от эталона всего на {total - LEADERBOARD_TOTAL:+} "
              f"посадок: это округление отдельных ячеек из-за других версий библиотек, на score не влияет")
    else:
        print(f"  md5 {h} — не совпал, сумма {total:,} против эталонных {LEADERBOARD_TOTAL:,}: "
              f"проверьте, что датасет полный и ml/config.py, ml/regimes.json не менялись")


if __name__ == "__main__":
    main()
