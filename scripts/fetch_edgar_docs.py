"""从 SEC EDGAR 取 FinanceBench 文档原文（GitHub 不可达时的独立数据通路）。

映射规则（确定性，可复核）：
- ``COMPANY_YYYY_10K`` → 该公司 form=10-K、reportDate 落在
  [YYYY-07-01, YYYY+1-06-30] 的卷宗，取 reportDate 最早的一个
  （财年截止不统一：Amcor 六月财年、BestBuy 二月财年均落窗内）；
- ``COMPANY_YYYY_10Q`` → form=10-Q、reportDate 落在 [YYYY-MM 之后的 100 天]
  的第一个（季度报告唯一性强）；
- **覆盖率自检环**：取回后若该文档全部题目的最高金证据覆盖率 < 0.5，
  判定映射可疑，自动换窗口内下一个候选重试；仍失败则标记
  ``mapping_unresolved`` 并从校准分母剔除（如实报告，不硬凑）。

HTML 处理：剥标签 + 实体解码 + 每 ~2400 字符插一个 `## Section N` 锚点
（10-K 无条款号，锚点供检索层分块；前言排除逻辑因此只作用于文件头）。
"""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

import requests

SEC_UA = "FinData-Agent calibration research contact@example.com"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")

# SEC 政策：≤10 req/s，超频封禁 ~10 分钟。三件套：磁盘缓存（重跑零重复请求）、
# 全局限速、增量跳过已有文档。
_CACHE_DIR = Path("data/raw/afac/edgar_cache")
_LAST_REQUEST = [0.0]
_MIN_INTERVAL = 0.3


def _polite_get(url: str, timeout: int = 120) -> requests.Response:
    import time

    cache_key = _CACHE_DIR / (re.sub(r"[^A-Za-z0-9]", "_", url)[-120:] + ".resp")
    if cache_key.exists():
        return _CachedResponse(cache_key)
    gap = time.time() - _LAST_REQUEST[0]
    if gap < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - gap)
    _LAST_REQUEST[0] = time.time()
    r = requests.get(url, headers={"User-Agent": SEC_UA}, timeout=timeout)
    r.raise_for_status()
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_key.write_bytes(r.content)
    return r


class _CachedResponse:
    """磁盘缓存的最小 Response 替身（.content / .raise_for_status / .text）。"""

    def __init__(self, path: Path) -> None:
        self.content = path.read_bytes()
        self.text = self.content.decode("utf-8", errors="replace")

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return json.loads(self.text)


def _strip_html(raw: str) -> str:
    text = html.unescape(_TAG.sub(" ", raw))
    text = _WS.sub(" ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _get_json(url: str) -> dict:
    return _polite_get(url, timeout=60).json()


def _norm_name(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _cik_for_company(company: str, tickers_path: Path) -> int | None:
    """公司名 → CIK：规范化后按 EDGAR 标题前缀匹配。

    匹配不到返回 None（退市公司如 ATVI 不在现列表里）——由调用方跳过并
    如实记录，不中断整批。
    """
    table = json.loads(tickers_path.read_text(encoding="utf-8"))
    want = _norm_name(company)
    for item in table.values():
        if _norm_name(item["title"]).startswith(want):
            return int(item["cik_str"])
    return None


def _submissions_with_history(cik: int) -> dict:
    """submissions 的 recent 只含最近 ~1000 份申报；申报大户的历史卷宗在
    filings.files 分页里——合并后老年份 10-K 才可见。"""
    data = _get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    filings = data["filings"]
    recent = filings["recent"]
    merged = {key: list(recent[key]) for key in recent}
    for extra in filings.get("files", []):
        older = _get_json(f"https://data.sec.gov/submissions/{extra['name']}")
        for key in merged:
            merged[key].extend(older["filings"][key] if "filings" in older
                               else older[key])
    return {"filings": {"recent": merged}}


def _candidates(submissions: dict, form: str, window: tuple[str, str]) -> list[dict]:
    # doc_name 用无连字符写法（10K/10Q），EDGAR form 字段带连字符（10-K/10-Q）
    edgar_form = {"10K": "10-K", "10Q": "10-Q"}[form]
    recent = submissions["filings"]["recent"]
    out = []
    for i, f in enumerate(recent["form"]):
        if f != edgar_form:
            continue
        rd = recent["reportDate"][i]
        if window[0] <= rd <= window[1]:
            out.append(
                {
                    "reportDate": rd,
                    "accession": recent["accessionNumber"][i].replace("-", ""),
                    "primary": recent["primaryDocument"][i],
                }
            )
    out.sort(key=lambda x: x["reportDate"])
    return out


def _fetch_text(cik: int, cand: dict) -> str:
    url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{cand['accession']}/"
        f"{cand['primary']}"
    )
    return _strip_html(_polite_get(url).text)


def _sectioned_doc(doc_name: str, text: str) -> str:
    parts = [text[i : i + 2400] for i in range(0, len(text), 2400)]
    body = "\n\n".join(
        f"## Section {i}\n\n{part}" for i, part in enumerate(parts, start=1)
    )
    return f"# {doc_name}\n\n{body}"


def parse_doc_name(doc_name: str) -> tuple[str, int, str] | None:
    """``AMD_2022_10K`` → (AMD, 2022, 10K)；财报新闻稿等返回 None（本适配器不处理）。"""
    m = re.match(r"^([A-Z0-9]+)_(\d{4})_(10K|10Q)$", doc_name)
    if not m:
        return None
    return m.group(1), int(m.group(2)), m.group(3)


def window_for(year: int, form: str) -> tuple[str, str]:
    if form == "10K":
        return f"{year}-07-01", f"{year + 1}-06-30"
    # 10Q：label 年内该季度的报告期窗口放宽到全年（自检环兜底）
    return f"{year}-01-01", f"{year + 1}-03-31"


def fetch_doc(
    doc_name: str,
    questions: list[dict],
    tickers_path: Path,
    out_dir: Path,
    cover_check,
    threshold: float = 0.5,
) -> dict:
    """取一个文档：候选卷宗按序尝试，覆盖率自检通过即落盘。"""
    parsed = parse_doc_name(doc_name)
    if parsed is None:
        return {"doc_name": doc_name, "status": "unsupported_form"}
    company, year, form = parsed
    cik = _cik_for_company(company, tickers_path)
    if cik is None:
        return {"doc_name": doc_name, "status": "unknown_company"}
    subs = _submissions_with_history(cik)
    cands = _candidates(subs, form, window_for(year, form))
    attempts = []
    for cand in cands:
        try:
            text = _fetch_text(cik, cand)
        except requests.RequestException as exc:
            attempts.append({"accession": cand["accession"], "error": str(exc)[:120]})
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{doc_name}.md").write_text(
            _sectioned_doc(doc_name, text), encoding="utf-8"
        )
        best = cover_check(doc_name)
        attempts.append(
            {
                "accession": cand["accession"],
                "reportDate": cand["reportDate"],
                "chars": len(text),
                "best_coverage": best,
            }
        )
        if best >= threshold:
            return {
                "doc_name": doc_name, "status": "ok", "cik": cik,
                "attempts": attempts,
            }
    return {"doc_name": doc_name, "status": "mapping_unresolved", "attempts": attempts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, help="financebench_open_source.jsonl")
    parser.add_argument("--tickers", required=True, help="EDGAR company_tickers.json")
    parser.add_argument("--docs", required=True, help="文档输出目录")
    args = parser.parse_args()
    questions = [
        json.loads(line)
        for line in Path(args.data).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_doc: dict[str, list[dict]] = {}
    for q in questions:
        by_doc.setdefault(q["doc_name"], []).append(q)

    # 覆盖率自检：金证据词级覆盖率（评测脚本的同一定义，importlib 路径加载——
    # scripts/ 不是包，直接执行本脚本时无法包导入）
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "eval_financebench_recall",
        PROJECT_ROOT / "scripts" / "eval_financebench_recall.py",
    )
    eval_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eval_mod)
    from findata.finqa.baseline import MAX_CHUNK_CHARS
    from findata.finqa.retrieval import BM25Index, chunk_docs

    def cover_check(doc_name: str) -> float:
        docs = {
            p.stem: p.read_text(encoding="utf-8")
            for p in Path(args.docs).glob("*.md")
        }
        index = BM25Index(chunk_docs(docs, max_chunk_chars=MAX_CHUNK_CHARS))
        best = 0.0
        for q in by_doc.get(doc_name, []):
            # FinanceBench 证据为嵌套列表 evidence: [{evidence_text: ...}]
            evidence = "\n".join(
                e.get("evidence_text", "") for e in q.get("evidence", [])
            )
            if not evidence:
                continue
            pool = [i for i, c in enumerate(index.chunks) if c.doc_id == doc_name]
            scores = index._scores(q["question"])
            ranked = sorted(pool, key=lambda i: scores[i], reverse=True)[:8]
            concat = "\n".join(index.chunks[i].text for i in ranked)
            best = max(best, eval_mod.coverage(evidence, concat))
        return best

    tickers_path = Path(args.tickers)
    out_dir = Path(args.docs)
    out_dir.mkdir(parents=True, exist_ok=True)
    statuses = []
    for doc_name in sorted(by_doc):
        existing = out_dir / f"{doc_name}.md"
        if existing.exists() and existing.stat().st_size > 10_000:
            statuses.append({"doc_name": doc_name, "status": "already_fetched"})
            print(f"{doc_name}: already_fetched")
            continue
        try:
            status = fetch_doc(
                doc_name, by_doc[doc_name], tickers_path, out_dir,
                cover_check=cover_check,
            )
        except Exception as exc:  # noqa: BLE001 — 单文档失败不拖垮整批，状态如实记录
            status = {"doc_name": doc_name, "status": "error", "error": str(exc)[:150]}
        statuses.append(status)
        print(f"{status['doc_name']}: {status['status']}")
    (out_dir / "_edgar_fetch_log.json").write_text(
        json.dumps(statuses, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
