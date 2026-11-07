"""按需生图：给表情库补图。

走 OpenAI 兼容的 `/images/generations` 协议，覆盖 OpenAI 本身和大多数国内厂商
（它们基本都提供这个形状的接口）。不绑定具体厂商，只认 base_url + model。

三条原则：
  1. 生成过的图**入库存档**，同样的描述第二次直接用，不再花钱；
  2. 任何失败都只记日志，绝不影响正常回复；
  3. 提示词先过一遍本地过滤，明显不合适的直接拒绝，不发给厂商。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# 明显不该自动生成并outgoing的方向：真人、真人明星、暴力、色情、政治人物
_BLOCK_WORDS = (
    "真人", "照片", "自拍", "裸", "色情", "血腥", "暴力", "尸体", "枪",
    "身份证", "护照", "习近平", "政治", "选举", "领导人",
    "nude", "nsfw", "gore", "blood", "gun", "weapon",
)

MAX_PROMPT_CHARS = 200


@dataclass
class GenResult:
    ok: bool
    path: Path | None = None
    error: str = ""
    cached: bool = False


def _norm(prompt: str) -> str:
    return " ".join((prompt or "").split()).strip()


def check_prompt(prompt: str) -> tuple[bool, str]:
    p = _norm(prompt)
    if not p:
        return False, "空提示词"
    if len(p) > MAX_PROMPT_CHARS:
        return False, f"提示词过长（{len(p)} > {MAX_PROMPT_CHARS}）"
    low = p.lower()
    for w in _BLOCK_WORDS:
        if w.lower() in low:
            return False, f"含受限词：{w}"
    return True, ""


def _slug(prompt: str) -> str:
    """把提示词压成一个好念的名字，供 agent 之后按名字复用这张图。

    中文取 6 个字以内更容易被记住和复述（太长会变成
    「眼睛瞪得溜圆嘴巴张成」这种读起来费劲的名字）。
    """
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", _norm(prompt))
    return (s[:6] or "表情")


def _multipart(fields: dict[str, str], files: list[tuple[str, str, bytes, str]]):
    """手搓 multipart/form-data（标准库没有现成的）。"""
    import uuid

    boundary = "----dsh" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for k, v in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        )
    for name, filename, raw, ctype in files:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
        )
        chunks.append(raw)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class ImageGen:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        cache_dir: Path,
        size: str = "1024x1024",
        quality: str = "",
        style: str = "",
        timeout: float = 60.0,
        max_px: int = 512,
        reference_images: list[str | Path] | None = None,
        style_prefix: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.size = size
        self.quality = quality
        self.style = style
        self.timeout = timeout
        self.max_px = max_px
        self.style_prefix = (style_prefix or "").strip()
        # 参考图：给了就走 /images/edits（带图），没给就走 /images/generations
        self.reference_images = [Path(p) for p in (reference_images or [])]
        self.reference_images = [p for p in self.reference_images if p.exists()]
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.cache_dir / "index.json"
        self._index: dict[str, str] = self._load_index()

    # ---------- 缓存 ----------
    def _load_index(self) -> dict[str, str]:
        if not self.index_path.exists():
            return {}
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _save_index(self) -> None:
        try:
            self.index_path.write_text(
                json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _refs_sig(self) -> str:
        """参考图变了就必须重新生成，不能命中旧缓存。"""
        if not self.reference_images:
            return "noref"
        parts = []
        for p in self.reference_images:
            try:
                st = p.stat()
                parts.append(f"{p.name}:{st.st_size}:{int(st.st_mtime)}")
            except Exception:
                parts.append(p.name)
        return "ref:" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]

    def _key(self, prompt: str) -> str:
        # 把影响出图结果的参数一起算进去，换模型/尺寸/参考图都不会命中旧图
        return hashlib.sha1(
            f"{self.model}|{self.size}|{self.quality}|{self._refs_sig()}|"
            f"{_norm(prompt)}".encode("utf-8")
        ).hexdigest()[:16]

    def cached(self, prompt: str) -> Path | None:
        name = self._index.get(self._key(prompt))
        if not name:
            return None
        p = self.cache_dir / name
        return p if p.exists() else None

    # ---------- 生图 ----------
    def _downscale(self, raw: bytes) -> bytes:
        """缩到表情合适的大小。失败就用原图，不因为缩图把整张丢 掉。"""
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(raw)) as im:
                if im.width <= self.max_px and im.height <= self.max_px:
                    return raw
                im = im.convert("RGBA")
                im.thumbnail((self.max_px, self.max_px), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format="PNG", optimize=True)
                return buf.getvalue()
        except Exception:
            return raw

    def _post(self, prompt: str):
        """有参考图走 edits（带图），没有就走 generations。"""
        if self.reference_images:
            return self._post_edits(prompt)
        return self._post_generations(prompt)

    def _post_generations(self, prompt: str):
        payload = {"model": self.model, "prompt": prompt, "n": 1, "size": self.size}
        # 实测：gpt-image-* 不接受 response_format（会 400），默认就返回 b64_json
        if self.quality:
            payload["quality"] = self.quality
        req = urllib.request.Request(
            f"{self.base_url}/images/generations",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        return self._do(req)

    def _post_edits(self, prompt: str):
        fields = {"model": self.model, "prompt": prompt, "n": "1", "size": self.size}
        if self.quality:
            fields["quality"] = self.quality
        refs = self.reference_images
        files = []
        for p in refs:
            ctype = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
            # 单张用 image，多张用 image[]（OpenAI 的 multipart 约定）
            files.append(("image" if len(refs) == 1 else "image[]",
                          p.name, p.read_bytes(), ctype))
        body, ctype = _multipart(fields, files)
        req = urllib.request.Request(
            f"{self.base_url}/images/edits",
            data=body,
            headers={"Content-Type": ctype,
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        return self._do(req)

    def _do(self, req):
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            return GenResult(False, error=f"HTTP {exc.code} {detail}")
        except Exception as exc:
            return GenResult(False, error=f"{type(exc).__name__}: {exc}")

    def generate(self, prompt: str) -> GenResult:
        ok, why = check_prompt(prompt)
        if not ok:
            return GenResult(False, error=f"提示词被拒：{why}")

        hit = self.cached(prompt)
        if hit:
            return GenResult(True, path=hit, cached=True)

        full_prompt = _norm(prompt)
        if self.style_prefix:
            full_prompt = f"{self.style_prefix}, {full_prompt}"

        body = self._post(full_prompt)
        if isinstance(body, GenResult):
            return body

        try:
            item = body["data"][0]
        except Exception:
            return GenResult(False, error=f"响应格式不认识：{str(body)[:150]}")

        raw: bytes | None = None
        if item.get("b64_json"):
            try:
                raw = base64.b64decode(item["b64_json"])
            except Exception as exc:
                return GenResult(False, error=f"base64 解码失败：{exc}")
        elif item.get("url"):
            try:
                with urllib.request.urlopen(item["url"], timeout=self.timeout) as r:
                    raw = r.read()
            except Exception as exc:
                return GenResult(False, error=f"下载图片失败：{exc}")
        if not raw:
            return GenResult(False, error="响应里没有图片数据")

        # 1024x1024 的 PNG 有 1 MB 左右，发微信会被压得很难看，先缩到 512
        raw = self._downscale(raw)

        name = f"gen_{self._key(prompt)}_{int(time.time())}.png"
        path = self.cache_dir / name
        path.write_bytes(raw)
        self._index[self._key(prompt)] = name
        self._save_index()
        self._register(name, prompt)
        return GenResult(True, path=path)

    def _register(self, filename: str, prompt: str) -> None:
        """记进 manifest，让这张图成为表情库里可用名字引用的一员。

        之前生成的图只躺在 cache_dir 里、库扫不到，agent 眼里"现成表情"永远
        只有手工放的那几张，于是反复用同一张——这是要修的核心问题。
        """
        mpath = self.cache_dir / "manifest.json"
        try:
            data = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}

        base = _slug(prompt)
        taken = {v.get("name") for v in data.values() if isinstance(v, dict)}
        name = base
        i = 2
        while name in taken:
            name = f"{base}{i}"
            i += 1

        data[f"generated/{filename}"] = {
            "name": name,
            "prompt": _norm(prompt)[:200],
            "ts": int(time.time()),
        }
        try:
            mpath.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass
