import json
from pathlib import Path
from scrapex.dedupe import canonical_alldata_url,DedupeIndex,sha256_file

def test_canonical():
    assert canonical_alldata_url("https://my.alldata.com/repair/?x=1#/article/123/guid/abc/")=="https://my.alldata.com/repair/#/article/123/guid/abc"

def test_dedupe(tmp_path:Path):
    root=tmp_path/"ADAS SI";root.mkdir();pdf=root/"x.pdf";pdf.write_bytes(b"%PDF-test")
    side=root/"x.source.json";side.write_text(json.dumps({"canonical_source_url":"https://my.alldata.com/repair/#/article/123","saved_pdf_sha256":sha256_file(pdf)}))
    idx=DedupeIndex(root)
    assert idx.has_url("https://my.alldata.com/repair/?x=1#/article/123")==side
    assert idx.has_hash(sha256_file(pdf)) is not None
