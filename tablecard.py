"""把表格渲染成分页长图，便于在微信里直接看。

设计取舍（都是踩过坑之后定的）：

1. **不是所有表第 1 行都是表头**。真实文件前面常有合并的大标题、副标题、
   一排 KPI 小格子。所以先找"列数最多且每格都不长"的那一行当表头。
2. **列多了不能硬挤**。14 列压在同一张图里，字号必然小到要放大。改成
   按列分组，每组重复关键列（优先级/导师），这样翻到哪组都认得出是谁。
3. **长句要换行，不能截断**。截断会把"当前状态"这种关键信息切掉，
   所以按列宽折行，行高跟着变。
4. 一页高度有限，超出就翻页，避免生成一张极长的图。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)

_MARGIN = 22
_FONT_SIZE = 20
_LINE_H = 28
_CELL_PAD_X = 10
_CELL_PAD_Y = 8
_HEADER_H = 46
_TITLE_H = 46
_MAX_PAGE_H = 1900          # 手机上一屏能看清的高度上限
_COL_CAP = 200              # 单列最大宽度
_COL_MIN = 84
_REPEAT_HINTS = ("优先级", "姓名", "导师", "推荐人", "序号", "编号", "名称", "项目")

_BG = (255, 255, 255)
_HEADER_BG = (236, 239, 245)
_ROW_ALT = (250, 250, 252)
_GRID = (222, 224, 230)
_TEXT = (32, 34, 40)
_DIM = (120, 124, 134)
_TITLE_COLOR = (18, 20, 26)


def _font(size: int = _FONT_SIZE):
    for path in _FONT_CANDIDATES:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------- 读表

def _load_sheets(path: Path) -> list[tuple[str, list[list[str]]]]:
    suffix = path.suffix.lower()
    if suffix in (".csv", ".txt"):
        import csv

        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = [[(c or "").strip() for c in r] for r in csv.reader(fh)]
        return [("", rows)]

    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        wb = load_workbook(path, data_only=True)
        out = []
        for name in wb.sheetnames:
            ws = wb[name]
            rows = []
            for row in ws.iter_rows(values_only=True):
                rows.append(["" if c is None else str(c) for c in row])
            out.append((name, rows))
        wb.close()
        return out

    raise ValueError(f"不支持的表格格式：{path.suffix}")


def _trim(rows: list[list[str]]) -> list[list[str]]:
    """去掉尾部空行和空列。"""
    rows = [list(r) for r in rows]
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()
    if not rows:
        return []
    width = 0
    for r in rows:
        for i, c in enumerate(r):
            if c.strip():
                width = max(width, i + 1)
    return [(r + [""] * width)[:width] for r in rows]


def _detect_header(rows: list[list[str]]) -> int:
    """找表头行：前 15 行里"非空格最多、且每格都短"的那一行。"""
    best, best_score = 0, -1
    for i, row in enumerate(rows[:15]):
        cells = [c.strip() for c in row if c.strip()]
        if len(cells) < 3:
            continue
        if max(len(c) for c in cells) > 30:
            continue          # 太长，是正文不是表头
        score = len(cells)
        if score > best_score:
            best, best_score = i, score
    return best


def _preamble(rows: list[list[str]], header_idx: int) -> list[str]:
    """表头之前的标题/KPI，压成几行文字放在图顶部。

    KPI 常见的排版是"标签在第 3 行、数值在第 4 行"，而且中间夹着空列
    （因为合并单元格）。所以按**列位置**配对：同一列上下的两行组成
    "标签 数值"，而不是先把空单元格过滤掉再两两配。
    """
    lines: list[str] = []
    used: set[int] = set()

    for i in range(header_idx):
        if i in used:
            continue
        row = rows[i]
        cells = [c.strip() for c in row]
        nonempty_at = [j for j, c in enumerate(cells) if c]
        if not nonempty_at:
            continue

        # 尝试和下一行配对（同一列有值）
        nxt = rows[i + 1] if i + 1 < header_idx else None
        if nxt is not None:
            nxt_cells = [c.strip() for c in nxt]
            paired = []
            for j in nonempty_at:
                if j < len(nxt_cells) and nxt_cells[j]:
                    paired.append(f"{cells[j]} {nxt_cells[j]}")
            if paired and len(paired) == len(nonempty_at):
                lines.append(" · ".join(paired))
                used.add(i + 1)
                continue

        if len(nonempty_at) >= 2:
            lines.append("  ".join(cells[j] for j in nonempty_at))
        else:
            lines.append(cells[nonempty_at[0]])

    return lines[:4]


# ---------------------------------------------------------------- 折行

def _wrap(text: str, font, width: int, max_lines: int = 6) -> list[str]:
    """按列宽折行。

    英文按**单词**折，中文按**字**折——否则会出现 "Degh at" "Uni versity"
    这种把词劈开的排版，读起来很别扭。
    """
    text = (text or "").replace("\r", "")
    if not text.strip():
        return [""]

    out: list[str] = []
    for para in text.split("\n"):
        # 把段落切成"中文单字"和"英文单词/数字"两类 token
        tokens: list[str] = []
        buf = ""
        for ch in para:
            if ch.isascii() and (ch.isalnum() or ch in "._-/:%#@+&'()"):
                buf += ch
            else:
                if buf:
                    tokens.append(buf)
                    buf = ""
                tokens.append(ch)
        if buf:
            tokens.append(buf)

        line = ""
        for tok in tokens:
            if font.getlength(line + tok) <= width:
                line += tok
                continue
            if line:
                out.append(line.rstrip())
                line = ""
            if len(out) >= max_lines:
                break
            # 单个 token 就超宽：只能按字符硬切
            if font.getlength(tok) > width:
                piece = ""
                for ch in tok:
                    if font.getlength(piece + ch) <= width:
                        piece += ch
                    else:
                        out.append(piece)
                        piece = ch
                        if len(out) >= max_lines:
                            break
                line = piece
            else:
                line = tok
        if line:
            out.append(line.rstrip())
        if len(out) >= max_lines:
            break

    out = out[:max_lines]
    if out and font.getlength(out[-1] + "…") <= width:
        out[-1] = out[-1] + "…" if len(out) == max_lines else out[-1]
    return out or [""]


# ---------------------------------------------------------------- 渲染

def _pick_key_cols(header: list[str], width: int) -> list[int]:
    keys = []
    for i, name in enumerate(header):
        if any(h in name for h in _REPEAT_HINTS):
            keys.append(i)
        if len(keys) >= 2:
            break
    return keys or [0]


def _column_groups(header: list[str], max_cols: int) -> list[list[int]]:
    n = len(header)
    if n <= max_cols:
        return [list(range(n))]
    keys = [i for i in _pick_key_cols(header, n) if i < n]
    rest = [i for i in range(n) if i not in keys]
    per = max(1, max_cols - len(keys))
    return [keys + rest[i:i + per] for i in range(0, len(rest), per)]


def render(path: str | Path, out_dir: str | Path | None = None,
           max_cols: int = 6, max_rows: int | None = None,
           sheets: list[str] | None = None,
           columns: list[str] | None = None) -> list[Path]:
    """渲染成分页 PNG。返回所有页面的路径。"""
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"找不到表格：{src}")

    out = Path(out_dir) if out_dir else src.with_name(src.stem + "_pages")
    out.mkdir(parents=True, exist_ok=True)

    font = _font()
    title_font = _font(int(_FONT_SIZE * 1.1))
    paths: list[Path] = []

    for sheet_name, raw in _load_sheets(src):
        rows = _trim(raw)
        if not rows:
            continue
        if sheets and sheet_name not in sheets:
            continue
        hdr_idx = _detect_header(rows)
        header = rows[hdr_idx]
        if not any(c.strip() for c in header):
            continue
        body = [r for r in rows[hdr_idx + 1:] if any(c.strip() for c in r)]
        if max_rows:
            body = body[:max_rows]
        if not body:
            continue

        pre = _preamble(rows, hdr_idx)

        # 只渲染指定列（按表头关键字匹配）。14 列全塞进手机必然要放大，
        # 所以支持"只看关键列"，页数能少一个数量级。
        if columns:
            keep = []
            for want in columns:
                for i, name in enumerate(header):
                    if want and want in name and i not in keep:
                        keep.append(i)
                        break
            if keep:
                header = [header[i] for i in keep]
                body = [[r[i] if i < len(r) else "" for i in keep] for r in body]

        groups = _column_groups(header, max_cols)

        for gi, cols in enumerate(groups, 1):
            widths = []
            for ci in cols:
                w = font.getlength(header[ci])
                for r in body[:200]:
                    w = max(w, font.getlength(r[ci] if ci < len(r) else ""))
                widths.append(int(min(max(w + _CELL_PAD_X * 2, _COL_MIN), _COL_CAP)))
            page_w = _MARGIN * 2 + sum(widths) + len(cols) - 1

            # 按像素高度切页
            pages: list[list[list[str]]] = []
            cur: list[list[str]] = []
            cur_h = 0.0
            for r in body:
                wrapped = [_wrap(r[ci] if ci < len(r) else "", font,
                                 widths[k] - _CELL_PAD_X * 2)
                           for k, ci in enumerate(cols)]
                row_h = max(len(w) for w in wrapped) * _LINE_H + _CELL_PAD_Y * 2
                if cur and cur_h + row_h > _MAX_PAGE_H:
                    pages.append(cur)
                    cur, cur_h = [], 0.0
                cur.append(wrapped)
                cur_h += row_h
            if cur:
                pages.append(cur)

            for pi, chunk in enumerate(pages, 1):
                heights = [max(len(w) for w in r) * _LINE_H + _CELL_PAD_Y * 2
                           for r in chunk]
                top = _MARGIN + _TITLE_H + len(pre) * _LINE_H + _HEADER_H
                page_h = int(top + sum(heights) + _MARGIN)
                img = Image.new("RGB", (page_w, page_h), _BG)
                draw = ImageDraw.Draw(img)

                title = sheet_name or src.stem
                draw.text((_MARGIN, _MARGIN), title[:40], fill=_TITLE_COLOR,
                          font=title_font)
                tags = []
                if len(groups) > 1:
                    tags.append(f"第 {gi}/{len(groups)} 组列")
                if len(pages) > 1:
                    tags.append(f"{pi}/{len(pages)} 页")
                tags.append(f"共 {len(body)} 条")
                tag = " · ".join(tags)
                draw.text((page_w - _MARGIN - font.getlength(tag), _MARGIN + 4),
                          tag, fill=_DIM, font=font)

                y = _MARGIN + _TITLE_H
                for line in pre:
                    for piece in _wrap(line, font, page_w - _MARGIN * 2, 2):
                        draw.text((_MARGIN, y), piece, fill=_DIM, font=font)
                        y += _LINE_H

                draw.rectangle([_MARGIN - 6, y, page_w - _MARGIN + 6, y + _HEADER_H],
                               fill=_HEADER_BG)
                x = _MARGIN
                for k, ci in enumerate(cols):
                    draw.text((x + _CELL_PAD_X, y + 12),
                              _wrap(header[ci], font, widths[k] - _CELL_PAD_X * 2, 2)[0],
                              fill=_TITLE_COLOR, font=font)
                    x += widths[k] + 1
                y += _HEADER_H

                for ri, row_cells in enumerate(chunk):
                    row_h = heights[ri]
                    if ri % 2:
                        draw.rectangle([_MARGIN - 6, y, page_w - _MARGIN + 6, y + row_h],
                                       fill=_ROW_ALT)
                    x = _MARGIN
                    for k, lines in enumerate(row_cells):
                        ly = y + _CELL_PAD_Y
                        for line in lines:
                            draw.text((x + _CELL_PAD_X, ly), line, fill=_TEXT, font=font)
                            ly += _LINE_H
                        x += widths[k] + 1
                    draw.line([_MARGIN - 6, y + row_h, page_w - _MARGIN + 6, y + row_h],
                              fill=_GRID, width=1)
                    y += row_h

                suffix = f"_{sheet_name}" if sheet_name else ""
                if len(groups) > 1:
                    suffix += f"_g{gi}"
                target = out / f"{src.stem}{suffix}_p{pi}.png"
                img.save(target, "PNG", optimize=True)
                paths.append(target)

    if not paths:
        raise ValueError("这份表里没有找到可渲染的表格内容")
    return paths
