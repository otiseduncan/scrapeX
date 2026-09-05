from __future__ import annotations
import hashlib, json, re
from pathlib import Path
from urllib.parse import urlsplit

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
    return h.hexdigest()

def canonical_alldata_url(raw: str) -> str:
    try: p=urlsplit(str(raw or "").strip())
    except ValueError: return ""
    host=(p.hostname or "").casefold()
    if not host.endswith("alldata.com"): return ""
    netloc=host+(f":{p.port}" if p.port else "")
    path=p.path or "/"
    if path != "/": path=path.rstrip("/")
    if p.fragment:
        if not path.endswith("/"): path += "/"
        return f"{p.scheme.casefold()}://{netloc}{path}#{p.fragment.rstrip('/')}"
    return f"{p.scheme.casefold()}://{netloc}{path}"

def article_id(url: str):
    m=re.search(r"(?:^|/)(?:article|guid)/([^/?#]+)",canonical_alldata_url(url),re.I)
    return re.sub(r"[^A-Za-z0-9_-]","",m.group(1))[:100] if m else None

def safe_name(value: str, fallback="ADAS procedure"):
    text=re.sub(r'[<>:"/\\|?*\x00-\x1f]+'," ",str(value or ""))
    text=re.sub(r"\s+"," ",text).strip(" .")
    return text[:150] or fallback

class DedupeIndex:
    def __init__(self,root: Path):
        self.root=root; self.urls={}; self.hashes={}; self._scan()
    def _scan(self):
        if not self.root.exists(): return
        for s in self.root.rglob("*.source.json"):
            try: data=json.loads(s.read_text(encoding="utf-8"))
            except Exception: continue
            u=canonical_alldata_url(data.get("canonical_source_url") or data.get("source_url") or "")
            if u: self.urls[u]=s
            d=str(data.get("saved_pdf_sha256") or "").casefold()
            if re.fullmatch(r"[0-9a-f]{64}",d): self.hashes[d]=s
        for p in self.root.rglob("*.pdf"):
            try: d=sha256_file(p)
            except OSError: continue
            self.hashes.setdefault(d,p)
    def has_url(self,url): return self.urls.get(canonical_alldata_url(url))
    def has_hash(self,digest): return self.hashes.get(str(digest).casefold())
    def add(self,url,digest,path):
        u=canonical_alldata_url(url)
        if u: self.urls[u]=path
        self.hashes[digest]=path
