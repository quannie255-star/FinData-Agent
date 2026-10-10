"""D1 扫描件 OCR 兜底：图片型 PDF 的文字提取（可选依赖 extra=ocr）。

设计纪律：
- **可选依赖不硬绑**：rapidocr-onnxruntime 只在 ``[ocr]`` extra 里，未安装时
  ``ocr_available()`` 返回 False，转换路径给出**明确报错行**而不是静默空文本
  （用户拿着一份「转换成功」的空文档提问，得到的全是「未提及」，那才是最差
  的失败形态）；
- 本地推理（onnxruntime CPU），不把页面图片发任何外部服务——与「文档数据
  不出本机（提问片段除外）」的隐私口径一致；
- 失败降级：单页 OCR 抛错不拖垮整份转换，该页标注错误行。

启用：``uv sync --extra ocr``（Windows CPU 可跑；模型随包分发，Apache-2.0）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅为类型注解；运行时绝不硬 import
    import pymupdf

_OCR = None
_OCR_TRIED = False


def ocr_available() -> bool:
    """rapidocr 是否已安装（懒探测，一次进程一次）。"""
    global _OCR, _OCR_TRIED
    if _OCR_TRIED:
        return _OCR is not None
    _OCR_TRIED = True
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return False
    try:
        _OCR = RapidOCR()
    except Exception:  # noqa: BLE001 — 模型文件损坏等极端情况按未安装处理
        _OCR = None
    return _OCR is not None


OCR_MISSING_HINT = (
    "[本页为扫描图片，未安装 OCR 组件无法提取文字：uv sync --extra ocr 后重试]"
)


def ocr_page_text(page: pymupdf.Page, dpi: int = 200) -> str:
    """单页 OCR：渲染位图 → RapidOCR → 按行拼文本。

    未安装 OCR 返回提示行（调用方照常入库，用户看得到原因）；
    推理异常返回错误标注行，不抛出（单页失败不拖垮整份转换）。
    """
    if not ocr_available():
        return OCR_MISSING_HINT
    import numpy as np

    try:
        pix = page.get_pixmap(dpi=dpi)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )
        if pix.n == 4:
            img = img[:, :, :3]
        result, _elapse = _OCR(img[:, :, ::-1])  # RGB → BGR（cv2 通道序）
        if not result:
            return ""
        return "\n".join(str(line[1]) for line in result if line and line[1])
    except Exception as exc:  # noqa: BLE001 — OCR 失败降级为标注行
        return f"[本页 OCR 失败：{str(exc)[:80]}]"
