"""向量嵌入后端（L2 混合检索的新基础设施——产品口径，2026-10-10 所有者裁决）。

口径边界（docs/next-cycle-roadmap.md §3.1.1 / §5 决策点 1）：
- 赛题四纪律（docs/afac-track4.md §0.1）禁止 embedding 参与正式答题——
  本模块只服务**产品模式**（个人文档库问答）；赛题口径评测线
  （isolate/baseline/submit）不得 import 本模块，对外引用赛题纪律时
  必须注明产品形态的差异；
- 数据驻留口径与 D1 OCR 一致：默认后端是**本机 CPU 推理**（fastembed，
  ONNX，文本不出本机）；百炼 API 后端是显式选择的兜底，其 embedding
  token 用量如实累计（成本如实铁律），不与 Qwen 问答台账混账。

接口约定：``embed_documents`` / ``embed_queries`` 返回与输入同序的向量
（两侧分开是 bge 系模型的查询指令前缀要求）；后端不可用（未安装可选
依赖 / 模型下载失败 / 网络/密钥错误）抛 :class:`EmbeddingUnavailable`，
由上层显式决定回退 BM25，绝不静默降级。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from functools import lru_cache
from itertools import chain
from pathlib import Path
from typing import Protocol, runtime_checkable

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN_WORD = re.compile(r"[A-Za-z0-9]+")


class EmbeddingUnavailable(RuntimeError):
    """嵌入后端不可用：未安装可选依赖 / 模型下载失败 / 网络 or 密钥错误。"""


@runtime_checkable
class Embedder(Protocol):
    name: str  # 缓存文件名与归档指纹的一部分（模型名即指纹）

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_queries(self, queries: list[str]) -> list[list[float]]: ...


def _bigram_features(text: str) -> list[str]:
    """字符二元组袋：CJK 相邻二元组 + 拉丁词内二元组（跨词不组）。

    与检索层 tokenize 的 CJK 二元组同构，但对拉丁词也降到子词粒度——
    这正是词法 word-token 对不上、而子词重叠仍然存在的形态。
    """
    feats: list[str] = []
    for word in _LATIN_WORD.findall(text.lower()):
        feats.extend(a + b for a, b in zip(word, word[1:], strict=False))
    chars = [c for c in text if _CJK.match(c)]
    feats.extend(a + b for a, b in zip(chars, chars[1:], strict=False))
    return feats


class HashEmbedder:
    """确定性哈希嵌入（二元组袋 → 哈希桶 → L2 归一）。

    **没有真实语义**，只保证「子词重叠多 → 余弦高」：用于离线测试融合
    机制与缓存/回退路径（CI 无网络、无可选依赖），质量结论一律以
    FinanceBench 真模型消融为准。
    """

    def __init__(self, dims: int = 256) -> None:
        self.dims = dims
        self.name = f"hash-bigram-{dims}"

    def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dims
            for feat in _bigram_features(text):
                digest = hashlib.sha256(feat.encode("utf-8")).digest()
                vec[int.from_bytes(digest[:8], "big") % self.dims] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([round(x / norm, 6) for x in vec])
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        return self._embed(queries)


class FastEmbedder:
    """本机 ONNX 推理（fastembed，可选依赖 extra=embed；CPU、文本不出本机）。

    bge 系模型查询侧需要指令前缀，查询走 ``query_embed``（旧版 API 只收
    单条时逐条退化）；文档侧 ``embed``。模型输入截断至模型上限
    （bge 系 512 token）——长块只有前部进向量臂，BM25F 仍覆盖全文，
    消融报告里如实注明。

    模型下载源：本机产品环境实测 huggingface.co 直连不可达（2026-10-10，
    hf-mirror 可达），且新版 hub 的 Xet 传输协议不走镜像（CAS 401）——
    默认切 hf-mirror + 禁 Xet；用户已显式设置 ``HF_ENDPOINT`` 时不覆盖
    （直连/自建镜像环境自治）。
    """

    def __init__(
        self, model_name: str = "BAAI/bge-small-en-v1.5", threads: int | None = None
    ) -> None:
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise EmbeddingUnavailable(
                "fastembed 未安装：uv sync --extra embed（本机 CPU 推理，文本不出本机）"
            ) from exc
        # ORT 默认线程数 = 全部核心；30+ 核机器上小模型推理反而被线程争用拖垮
        # （2026-10-10 实测：32 核 25 分钟未完成 1.3 万块，限 8 线程后数分钟）。
        threads = threads if threads is not None else min(8, os.cpu_count() or 4)
        try:
            self._model = TextEmbedding(model_name=model_name, threads=threads)
        except Exception as exc:  # noqa: BLE001 — 模型下载/加载失败统一转不可用
            raise EmbeddingUnavailable(
                f"fastembed 模型不可用：{model_name}（{exc}）"
            ) from exc
        self.name = model_name

    @staticmethod
    def _floats(stream) -> list[list[float]]:  # noqa: ANN001 — fastembed 返回类型随版本
        return [[round(float(x), 6) for x in vec] for vec in stream]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._floats(self._model.embed(texts))

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        query_embed = getattr(self._model, "query_embed", None)
        if query_embed is None:
            return self.embed_documents(queries)
        try:
            return self._floats(query_embed(queries))
        except TypeError:
            # 旧版 query_embed 只收单条字符串
            return self._floats(chain.from_iterable(query_embed(q) for q in queries))


class DashScopeEmbedder:
    """百炼 embeddings API 兜底（显式选择；文本发往云端，token 用量如实累计）。

    与 QwenClient 同一密钥与 compatible-mode 入口，但 embedding 调用**不进
    问答 token 台账**（那是 Qwen 答题账）——用量单独累计在
    ``usage_prompt_tokens``，报告里单列。
    """

    def __init__(
        self,
        model: str = "text-embedding-v4",
        api_key: str | None = None,
        base_url: str | None = None,
        batch_size: int = 10,
        client=None,  # noqa: ANN001 — 测试注入假客户端；生产走 openai SDK
    ) -> None:
        from findata.config import settings

        key = api_key if api_key is not None else settings.dashscope_api_key
        if not key:
            raise EmbeddingUnavailable(
                "缺少百炼 API key：配置 FINDATA_DASHSCOPE_API_KEY 或注入 api_key"
            )
        self.name = model
        self._batch = batch_size
        self.usage_prompt_tokens = 0
        if client is not None:
            self._client = client
        else:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=key,
                base_url=base_url or settings.dashscope_base_url,
                timeout=settings.dashscope_timeout_seconds,
            )

    def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            batch = texts[i : i + self._batch]
            for attempt in range(3):
                try:
                    resp = self._client.embeddings.create(model=self.name, input=batch)
                    break
                except Exception as exc:  # noqa: BLE001 — 网络/限流统一转不可用
                    if attempt == 2:
                        raise EmbeddingUnavailable(
                            f"百炼 embeddings 调用失败：{exc}"
                        ) from exc
                    time.sleep(2**attempt)
            usage = getattr(resp, "usage", None)
            if usage is not None:
                self.usage_prompt_tokens += usage.prompt_tokens or 0
            # 按 index 排序：接口不承诺 data 与 input 同序
            out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        return self._embed(queries)


def _cache_key(kind: str, text: str) -> str:
    return hashlib.sha256(f"{kind}\x00{text}".encode()).hexdigest()


class CachedEmbedder:
    """内容寻址缓存（sha256(kind+text) → 向量；JSONL 追加式，可重建）。

    向量臂建索引要嵌入全语料，而个人文档库每次提问都重建索引——没有
    缓存，同一批文档会被反复嵌入（本机分钟级浪费、云端真金白银）。
    缓存键含 kind（doc/query）：bge 系查询侧带指令前缀，两侧向量不可
    混用。向量按 6 位小数落盘：排序结论不受影响，且两次运行逐位一致。
    """

    def __init__(self, inner: Embedder, cache_dir: Path | str) -> None:
        self.inner = inner
        self.name = inner.name
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", inner.name)
        self._path = Path(cache_dir) / f"{safe}.jsonl"
        self._cache: dict[str, list[float]] = {}
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                self._cache[row["k"]] = row["v"]
        self.embed_calls = 0  # 实际落到 inner 的条数（成本观测：0 = 全缓存命中）

    def _embed(self, kind, texts, fn):  # noqa: ANN001, ANN202 — 协议内部胶水
        keys = [_cache_key(kind, t) for t in texts]
        missing = [i for i, key in enumerate(keys) if key not in self._cache]
        if missing:
            self.embed_calls += len(missing)
            vecs = fn([texts[i] for i in missing])
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                for i, vec in zip(missing, vecs, strict=True):
                    self._cache[keys[i]] = vec
                    fh.write(json.dumps({"k": keys[i], "v": vec}) + "\n")
        return [self._cache[key] for key in keys]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed("doc", texts, self.inner.embed_documents)

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        return self._embed("query", queries, self.inner.embed_queries)


@lru_cache(maxsize=4)
def _build_cached(backend: str, model: str, cache_dir: str) -> CachedEmbedder:
    """单例工厂：fastembed 模型加载是秒级开销，多请求共享一个实例。"""
    if backend == "fastembed":
        inner: Embedder = FastEmbedder(model)
    elif backend == "dashscope":
        inner = DashScopeEmbedder(model)
    else:
        raise EmbeddingUnavailable(f"未知嵌入后端：{backend}（fastembed / dashscope）")
    return CachedEmbedder(inner, cache_dir)


def build_embedder(backend: str, model: str, cache_dir: Path | str) -> CachedEmbedder:
    """产品/评测共用的入口：不可用时抛 EmbeddingUnavailable（不静默）。"""
    return _build_cached(backend, model, str(cache_dir))
