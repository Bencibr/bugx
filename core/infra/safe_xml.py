#!/usr/bin/env python3
"""安全 XML 解析：拒绝 DTD/实体声明，并施加体积、深度与元素数上限 (SEC-07)。

背景
----
被审计项目的产物（coverage 报告、``pom.xml``、Android 布局）是不可信输入，
而仓库此前在三个位置直接调用 ``xml.etree.ElementTree.fromstring`` / ``parse``：

* ``core/coverage_intelligence.py``（coverage 报告）
* ``core/sca_scanner.py``（``pom.xml``；其 ``clean_xml`` 只剥 xmlns）
* ``core/mobile_ui_inspector.py``（Android 布局）

``xml.etree`` 不解析外部实体，因此 XXE 文件读取风险很低；但**内部实体膨胀
（billion laughs / 二次膨胀）会耗尽 CPU 与内存**。实测结论：

* 本机托管 Python 3.13.12（libexpat 2.6.3）**拒绝**经典 billion-laughs
  载荷——libexpat ≥ 2.4 默认开启 amplification 限制；
* 系统 Python 3.9.6（libexpat 2.2.8）在**同一个载荷上被直接杀死**（SIGTERM /
  OOM）。

也就是说，防护完全依赖运行时 libexpat 版本这一隐式前提。仓库声明支持更老的
Python，因此必须显式防护，不能把安全性交给「碰巧装了新版 expat」。

防护策略（全部只依赖标准库，仓库零第三方依赖）
--------------------------------------------
1. **拒绝 DTD**：没有 ``<!DOCTYPE`` 就没有 ``<!ENTITY``，expat 便不会做任何
   用户定义实体展开。扫描前先剔除注释与 CDATA 段，避免把合法内容误判。
2. **体积上限**：解析前拒绝超限输入。
3. **深度与元素数上限**：解析后按树结构计数，拦住深层嵌套/超宽文档造成的
   内存放大（这类膨胀不依赖 DTD，第 1 条拦不住）。

失败一律抛 :class:`XmlSecurityError`（``ValueError`` 子类），调用方可以把它
降级为「该报告不可信/跳过」而不是崩溃。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

__all__ = [
    "MAX_XML_BYTES",
    "MAX_XML_DEPTH",
    "MAX_XML_ELEMENTS",
    "XmlSecurityError",
    "parse_xml",
    "parse_xml_file",
]

#: 输入体积上限：正常 coverage/pom/布局文件远小于此值。
MAX_XML_BYTES = 8 * 1024 * 1024
#: 元素嵌套深度上限。
MAX_XML_DEPTH = 64
#: 元素总数上限。
MAX_XML_ELEMENTS = 200_000

#: DTD 与实体声明。XML 规范要求大写，此处大小写不敏感以放宽检出（安全方向）。
_DTD_MARKERS = ("<!DOCTYPE", "<!ENTITY")
#: 注释与 CDATA：内容不是标记，扫描前剔除以免误判。
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_CDATA_RE = re.compile(r"<!\[CDATA\[.*?\]\]>", re.DOTALL)
_BOMS = (
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


class XmlSecurityError(ValueError):
    """XML 输入因安全策略被拒绝。"""


def _sniff_utf16(data: bytes) -> str | None:
    """无 BOM 的 UTF-16 嗅探。

    这不是可选项：``expat`` 会从 NUL 交错模式自行识别 UTF-16，因此
    「按 UTF-8 解码成带 NUL 的字符串」既不会报错，也**会让 DTD 标记扫描
    完全失效**（实测 ``ET.fromstring`` 在这种字符串上仍能成功解析并展开实体）。
    """
    sample = data[:64]
    if len(sample) < 2:
        return None
    even_nuls = sum(1 for index in range(0, len(sample), 2) if sample[index] == 0)
    odd_nuls = sum(1 for index in range(1, len(sample), 2) if sample[index] == 0)
    half = max(1, len(sample) // 2)
    if odd_nuls >= half and even_nuls == 0:
        return "utf-16-le"
    if even_nuls >= half and odd_nuls == 0:
        return "utf-16-be"
    return None


def _decode(data: bytes) -> str:
    """把字节流解码为文本，供标记扫描使用。

    ``xml.etree`` 支持多种编码，纯字节扫描会漏掉 DTD 标记，因此这里显式归一化
    到 ``str`` 再扫描，并把解码后的文本交给解析器，避免编码歧义。
    """
    encoding = None
    for bom, bom_encoding in _BOMS:
        if data.startswith(bom):
            encoding = bom_encoding
            break
    if encoding is None:
        encoding = _sniff_utf16(data)
    if encoding is not None:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError as exc:
            raise XmlSecurityError(f"XML 字节流无法解码（{encoding}）: {exc}") from exc
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise XmlSecurityError(
                f"XML 字节流既非 UTF-8 也无 BOM，拒绝解析以避免绕过 DTD 扫描: {exc}"
            ) from exc
    # NUL 在 XML 1.0 中根本不是合法字符；出现即说明编码判定不可靠（例如
    # UTF-32 或被刻意构造的字节流），而 expat 仍可能按 UTF-16 解析它，
    # 从而绕过字符串层面的 DTD 扫描。fail-closed 拒绝。
    if "\x00" in text:
        raise XmlSecurityError("XML 字节流含 NUL，无法可靠判定编码，拒绝解析")
    return text.lstrip("\ufeff")


def _normalize(source: str | bytes) -> str:
    if isinstance(source, str):
        text = source
        size = len(source.encode("utf-8", "surrogatepass"))
    elif isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
        size = len(raw)
        text = _decode(raw)
    else:
        raise XmlSecurityError(f"XML 输入必须是 str 或 bytes，收到 {type(source).__name__}")
    if size > MAX_XML_BYTES:
        raise XmlSecurityError(f"XML 超过 {MAX_XML_BYTES} 字节上限（{size}）")
    return text


def _assert_no_dtd(text: str) -> None:
    """拒绝 DTD / 实体声明（剔除注释与 CDATA 后扫描）。"""
    scannable = _CDATA_RE.sub("", _COMMENT_RE.sub("", text)).upper()
    for marker in _DTD_MARKERS:
        if marker in scannable:
            raise XmlSecurityError(
                f"XML 包含 {marker} 声明；DTD 与用户定义实体一律拒绝"
                "（实体膨胀 DoS，SEC-07）"
            )


def _assert_within_limits(
    root: ET.Element, max_depth: int, max_elements: int,
) -> None:
    """按树结构校验深度与元素总数（不依赖 DTD 的膨胀路径）。"""
    total = 0
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        total += 1
        if total > max_elements:
            raise XmlSecurityError(f"XML 元素数超过 {max_elements} 上限")
        if depth > max_depth:
            raise XmlSecurityError(f"XML 嵌套深度超过 {max_depth} 上限")
        for child in element:
            stack.append((child, depth + 1))


def parse_xml(
    source: str | bytes,
    *,
    max_bytes: int | None = None,
    max_depth: int = MAX_XML_DEPTH,
    max_elements: int = MAX_XML_ELEMENTS,
) -> ET.Element:
    """安全解析 XML 文本/字节，返回根元素。

    抛 :class:`XmlSecurityError`（策略拒绝或格式非法）。调用方应把它当作
    「该产物不可信」处理，而不是程序缺陷。
    """
    text = _normalize(source)
    if max_bytes is not None and len(text.encode("utf-8", "surrogatepass")) > max_bytes:
        raise XmlSecurityError(f"XML 超过 {max_bytes} 字节上限")
    _assert_no_dtd(text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise XmlSecurityError(f"XML 解析失败: {exc}") from exc
    _assert_within_limits(root, max_depth, max_elements)
    return root


def parse_xml_file(
    path: str | Path,
    *,
    max_bytes: int = MAX_XML_BYTES,
    max_depth: int = MAX_XML_DEPTH,
    max_elements: int = MAX_XML_ELEMENTS,
) -> ET.Element:
    """安全读取并解析项目内的 XML 文件。

    先按体积上限拒绝，再解码扫描 DTD，最后解析；不使用 ``ET.parse``，
    因为它在读取阶段就已经开始解析，无法在 DTD 出现前介入。
    """
    candidate = Path(path)
    if candidate.is_symlink():
        raise XmlSecurityError(f"XML 文件不能是符号链接: {candidate}")
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise XmlSecurityError(f"XML 文件不可读: {candidate}") from exc
    if size > max_bytes:
        raise XmlSecurityError(f"XML 超过 {max_bytes} 字节上限（{size}）: {candidate}")
    try:
        data = candidate.read_bytes()
    except OSError as exc:
        raise XmlSecurityError(f"XML 文件不可读: {candidate}") from exc
    return parse_xml(
        data, max_bytes=max_bytes, max_depth=max_depth, max_elements=max_elements,
    )
